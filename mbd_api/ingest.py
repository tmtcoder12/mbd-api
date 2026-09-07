"""Repeatable, staged knowledge ingestion for Supabase pgvector."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urljoin, urlparse

from dotenv import load_dotenv
from openai import OpenAI
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .config import Settings
from .repository import SupabaseStore

EMBEDDING_MODEL = "text-embedding-3-small"
EMBEDDING_DIMENSIONS = 1536
DETERMINISTIC_NAMESPACE = uuid.UUID("9fbd06b4-56bc-4e42-8cef-8fba4c6c58a0")


class ManifestChunk(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_key: str = Field(min_length=1, max_length=120)
    external_id: str = Field(min_length=1, max_length=160)
    text: str = Field(min_length=1, max_length=20_000)
    type: str | None = Field(default=None, max_length=80)
    title: str | None = Field(default=None, max_length=240)
    source_url: str | None = None
    page_path: str | None = Field(default=None, max_length=500)
    meta_description: str | None = Field(default=None, max_length=500)
    image_path: str | None = None
    image_url: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("source_key", "external_id", "text")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be blank")
        return stripped

    @model_validator(mode="after")
    def one_image_reference(self) -> ManifestChunk:
        if self.image_path and self.image_url:
            raise ValueError("use image_path or image_url, not both")
        return self


class IngestionManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    restaurant_id: uuid.UUID
    source_name: str = Field(min_length=1, max_length=160)
    chunks: list[ManifestChunk] = Field(min_length=1, max_length=5000)

    @model_validator(mode="after")
    def unique_chunk_keys(self) -> IngestionManifest:
        keys = [(chunk.source_key, chunk.external_id) for chunk in self.chunks]
        if len(keys) != len(set(keys)):
            raise ValueError("source_key/external_id pairs must be unique")
        return self


def load_manifest(path: Path) -> IngestionManifest:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"unable to read manifest: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"manifest is not valid JSON: {exc}") from exc
    try:
        return IngestionManifest.model_validate(data)
    except ValidationError as exc:
        raise ValueError(f"manifest validation failed: {exc}") from exc


def deterministic_chunk_id(restaurant_id: uuid.UUID, source_key: str, external_id: str) -> str:
    identity = f"{restaurant_id}:{source_key}:{external_id}"
    return str(uuid.uuid5(DETERMINISTIC_NAMESPACE, identity))


def content_hash(text: str) -> str:
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


def resolve_image_url(chunk: ManifestChunk, asset_base_url: str | None) -> str | None:
    if chunk.image_url:
        parsed = urlparse(chunk.image_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError(f"invalid image_url for {chunk.external_id}")
        return chunk.image_url
    if not chunk.image_path:
        return None
    if not asset_base_url:
        raise ValueError(f"--asset-base-url is required for image_path on {chunk.external_id}")
    parsed_base = urlparse(asset_base_url)
    if parsed_base.scheme not in {"http", "https"} or not parsed_base.netloc:
        raise ValueError("--asset-base-url must be an absolute http(s) URL")
    return urljoin(asset_base_url.rstrip("/") + "/", chunk.image_path.lstrip("/"))


def _embed_with_retry(client: Any, texts: list[str], attempts: int = 3) -> list[list[float]]:
    delay = 0.5
    for attempt in range(attempts):
        try:
            response = client.embeddings.create(model=EMBEDDING_MODEL, input=texts)
            vectors = [list(item.embedding) for item in response.data]
            if len(vectors) != len(texts):
                raise RuntimeError("embedding response count did not match input count")
            if any(len(vector) != EMBEDDING_DIMENSIONS for vector in vectors):
                raise RuntimeError(f"embedding vectors must have {EMBEDDING_DIMENSIONS} dimensions")
            return vectors
        except Exception:
            if attempt + 1 >= attempts:
                raise
            time.sleep(delay)
            delay *= 2
    raise RuntimeError("embedding retry loop ended unexpectedly")


def prepare_rows(
    manifest: IngestionManifest,
    existing: dict[tuple[str, str], tuple[str, str]],
    client: Any,
    asset_base_url: str | None,
    batch_size: int,
) -> tuple[list[dict[str, Any]], int, int]:
    prepared: list[dict[str, Any]] = []
    changed_indexes: list[int] = []
    rid = str(manifest.restaurant_id)
    for chunk in manifest.chunks:
        digest = content_hash(chunk.text)
        unchanged = existing.get((chunk.source_key, chunk.external_id)) == (digest, EMBEDDING_MODEL)
        row = {
            "id": deterministic_chunk_id(manifest.restaurant_id, chunk.source_key, chunk.external_id),
            "restaurant_id": rid,
            "source_key": chunk.source_key,
            "external_id": chunk.external_id,
            "text": chunk.text,
            "type": chunk.type,
            "source_url": chunk.source_url,
            "page_path": chunk.page_path,
            "title": chunk.title,
            "meta_description": chunk.meta_description,
            "image_url": resolve_image_url(chunk, asset_base_url),
            "extra_metadata": chunk.metadata,
            "content_hash": digest,
            "embedding_model": EMBEDDING_MODEL,
            "embedding": None,
        }
        prepared.append(row)
        if not unchanged:
            changed_indexes.append(len(prepared) - 1)

    for offset in range(0, len(changed_indexes), batch_size):
        indexes = changed_indexes[offset : offset + batch_size]
        vectors = _embed_with_retry(client, [prepared[index]["text"] for index in indexes])
        for index, vector in zip(indexes, vectors, strict=True):
            prepared[index]["embedding"] = vector
    return prepared, len(changed_indexes), len(prepared) - len(changed_indexes)


def run_ingestion(
    manifest: IngestionManifest,
    *,
    settings: Settings,
    asset_base_url: str | None,
    batch_size: int,
    prune: bool,
    store: SupabaseStore | Any | None = None,
    client: Any | None = None,
) -> dict[str, Any]:
    repository = store or SupabaseStore(
        settings.supabase_url,
        settings.supabase_service_role_key.get_secret_value(),
        timeout_s=settings.supabase_timeout_seconds,
    )
    openai_client = client or OpenAI(
        api_key=settings.openai_api_key.get_secret_value(),
        timeout=settings.openai_timeout_seconds,
        max_retries=settings.openai_max_retries,
    )
    run_id: str | None = None
    try:
        fingerprints = repository.get_knowledge_chunk_fingerprints(str(manifest.restaurant_id))
        existing = {
            (str(row.get("source_key")), str(row.get("external_id"))): (
                str(row.get("content_hash")),
                str(row.get("embedding_model")),
            )
            for row in fingerprints
        }
        rows, embedded, unchanged = prepare_rows(manifest, existing, openai_client, asset_base_url, batch_size)
        run_id = repository.begin_ingest_run(
            str(manifest.restaurant_id), EMBEDDING_MODEL, manifest.source_name, len(rows)
        )
        if not run_id:
            raise RuntimeError("Supabase did not return an ingestion run id")
        for offset in range(0, len(rows), batch_size):
            repository.stage_knowledge_chunks(run_id, rows[offset : offset + batch_size])
        repository.finish_ingest_run(run_id, "running", embedded)
        finalized = repository.finalize_ingest_run(run_id, prune=prune)
        return {
            "ok": True,
            "runId": run_id,
            "restaurantId": str(manifest.restaurant_id),
            "totalChunks": len(rows),
            "embeddedChunks": embedded,
            "unchangedChunks": unchanged,
            "activatedChunks": finalized["activated_chunks"],
            "prunedChunks": finalized["pruned_chunks"],
        }
    except Exception as exc:
        if run_id:
            with contextlib.suppress(Exception):
                repository.finish_ingest_run(run_id, "error", 0, str(exc)[:1000])
        raise
    finally:
        if store is None:
            repository.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Ingest a versioned restaurant knowledge manifest")
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--asset-base-url")
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--prune", action="store_true", help="Remove active rows omitted from included source keys")
    parser.add_argument("--dry-run", action="store_true", help="Validate locally without network calls or writes")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if not 1 <= args.batch_size <= 100:
            raise ValueError("--batch-size must be between 1 and 100")
        manifest = load_manifest(args.manifest)
        if args.dry_run:
            for chunk in manifest.chunks:
                resolve_image_url(chunk, args.asset_base_url)
            image_chunks = sum(1 for chunk in manifest.chunks if chunk.image_path or chunk.image_url)
            result = {
                "ok": True,
                "dryRun": True,
                "restaurantId": str(manifest.restaurant_id),
                "totalChunks": len(manifest.chunks),
                "sourceKeys": sorted({chunk.source_key for chunk in manifest.chunks}),
                "imageChunks": image_chunks,
            }
        else:
            load_dotenv()
            settings = Settings()  # type: ignore[call-arg]
            result = run_ingestion(
                manifest,
                settings=settings,
                asset_base_url=args.asset_base_url,
                batch_size=args.batch_size,
                prune=args.prune,
            )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
