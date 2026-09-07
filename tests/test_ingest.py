import json
import types
import uuid
from pathlib import Path
from typing import Any

import pytest

from mbd_api.config import Settings
from mbd_api.ingest import (
    EMBEDDING_DIMENSIONS,
    EMBEDDING_MODEL,
    IngestionManifest,
    ManifestChunk,
    _embed_with_retry,
    content_hash,
    deterministic_chunk_id,
    load_manifest,
    prepare_rows,
    resolve_image_url,
    run_ingestion,
)

RESTAURANT_ID = uuid.UUID("11111111-1111-4111-8111-555555555555")


def settings() -> Settings:
    return Settings(
        _env_file=None,
        app_env="test",
        openai_api_key="test-key",
        supabase_url="http://supabase.test",
        supabase_service_role_key="test-key",
        widget_signing_keys="v1:test-key",
    )


def manifest(*chunks: ManifestChunk) -> IngestionManifest:
    return IngestionManifest(
        schema_version=1,
        restaurant_id=RESTAURANT_ID,
        source_name="test",
        chunks=list(chunks),
    )


def chunk(external_id: str, text: str = "Useful restaurant fact.", **kwargs: Any) -> ManifestChunk:
    return ManifestChunk(source_key="menu", external_id=external_id, text=text, **kwargs)


class FakeEmbeddings:
    def __init__(self, failures: int = 0) -> None:
        self.failures = failures
        self.calls: list[list[str]] = []

    def create(self, *, model: str, input: list[str]):
        self.calls.append(input)
        if len(self.calls) <= self.failures:
            raise TimeoutError("temporary embedding failure")
        return types.SimpleNamespace(
            data=[
                types.SimpleNamespace(embedding=[float(index)] * EMBEDDING_DIMENSIONS) for index, _ in enumerate(input)
            ]
        )


class FakeClient:
    def __init__(self, failures: int = 0) -> None:
        self.embeddings = FakeEmbeddings(failures)


class FakeStore:
    def __init__(self, existing: list[dict[str, Any]] | None = None, fail_stage: bool = False) -> None:
        self.existing = existing or []
        self.fail_stage = fail_stage
        self.staged: list[dict[str, Any]] = []
        self.finalize_calls: list[bool] = []
        self.finish_calls: list[tuple[Any, ...]] = []

    def get_knowledge_chunk_fingerprints(self, restaurant_id: str) -> list[dict[str, Any]]:
        return self.existing

    def begin_ingest_run(self, restaurant_id: str, model: str, source_name: str, total_chunks: int) -> str:
        return "22222222-2222-4222-8222-222222222222"

    def stage_knowledge_chunks(self, run_id: str, rows: list[dict[str, Any]]) -> None:
        if self.fail_stage:
            raise RuntimeError("staging failed")
        self.staged.extend(rows)

    def finish_ingest_run(self, *args: Any) -> None:
        self.finish_calls.append(args)

    def finalize_ingest_run(self, run_id: str, prune: bool = False) -> dict[str, int]:
        self.finalize_calls.append(prune)
        return {"activated_chunks": len(self.staged), "pruned_chunks": 2 if prune else 0}


def test_deterministic_ids_are_stable_and_tenant_scoped() -> None:
    first = deterministic_chunk_id(RESTAURANT_ID, "menu", "salmon")
    assert first == deterministic_chunk_id(RESTAURANT_ID, "menu", "salmon")
    assert first != deterministic_chunk_id(RESTAURANT_ID, "menu", "burger")
    assert str(uuid.UUID(first)) == first


def test_manifest_rejects_duplicate_keys_and_extra_fields(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "restaurant_id": str(RESTAURANT_ID),
                "source_name": "test",
                "chunks": [
                    {"source_key": "menu", "external_id": "same", "text": "one"},
                    {"source_key": "menu", "external_id": "same", "text": "two"},
                ],
            }
        )
    )
    with pytest.raises(ValueError, match="must be unique"):
        load_manifest(path)


def test_asset_paths_require_and_resolve_against_public_base_url() -> None:
    item = chunk("salmon", image_path="assets/salmon.webp")
    with pytest.raises(ValueError, match="asset-base-url"):
        resolve_image_url(item, None)
    assert resolve_image_url(item, "https://cdn.example/demo") == "https://cdn.example/demo/assets/salmon.webp"


def test_unchanged_rows_skip_embeddings_while_changed_rows_are_embedded() -> None:
    unchanged = chunk("hours", "Open Tuesday through Sunday.")
    changed = chunk("salmon", "Cedar-plank salmon is $34.")
    existing = {("menu", "hours"): (content_hash(unchanged.text), EMBEDDING_MODEL)}
    client = FakeClient()
    rows, embedded, skipped = prepare_rows(
        manifest(unchanged, changed), existing, client, "https://cdn.example", batch_size=8
    )
    assert (embedded, skipped) == (1, 1)
    assert rows[0]["embedding"] is None
    assert len(rows[1]["embedding"]) == EMBEDDING_DIMENSIONS
    assert client.embeddings.calls == [[changed.text]]


def test_embedding_retry_and_dimension_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("mbd_api.ingest.time.sleep", lambda _: None)
    client = FakeClient(failures=2)
    vectors = _embed_with_retry(client, ["hello"])
    assert len(client.embeddings.calls) == 3
    assert len(vectors[0]) == EMBEDDING_DIMENSIONS

    bad_client = FakeClient()
    bad_client.embeddings.create = lambda **_: types.SimpleNamespace(data=[types.SimpleNamespace(embedding=[0.0])])
    with pytest.raises(RuntimeError, match="1536 dimensions"):
        _embed_with_retry(bad_client, ["hello"], attempts=1)


def test_rerun_uses_same_ids_and_skips_all_unchanged_embeddings() -> None:
    item = chunk("salmon", "Cedar-plank salmon is $34.")
    first_client = FakeClient()
    first_rows, _, _ = prepare_rows(manifest(item), {}, first_client, "https://cdn.example", 8)
    existing = {("menu", "salmon"): (first_rows[0]["content_hash"], EMBEDDING_MODEL)}
    second_client = FakeClient()
    second_rows, embedded, skipped = prepare_rows(manifest(item), existing, second_client, "https://cdn.example", 8)
    assert second_rows[0]["id"] == first_rows[0]["id"]
    assert (embedded, skipped) == (0, 1)
    assert second_client.embeddings.calls == []


def test_ingestion_batches_then_finalizes_with_explicit_pruning() -> None:
    store = FakeStore()
    result = run_ingestion(
        manifest(chunk("one"), chunk("two")),
        settings=settings(),
        asset_base_url="https://cdn.example",
        batch_size=1,
        prune=True,
        store=store,
        client=FakeClient(),
    )
    assert result["ok"] is True
    assert result["activatedChunks"] == 2
    assert result["prunedChunks"] == 2
    assert store.finalize_calls == [True]
    assert len(store.staged) == 2


def test_failed_staging_never_calls_transactional_finalize() -> None:
    store = FakeStore(fail_stage=True)
    with pytest.raises(RuntimeError, match="staging failed"):
        run_ingestion(
            manifest(chunk("one")),
            settings=settings(),
            asset_base_url="https://cdn.example",
            batch_size=1,
            prune=True,
            store=store,
            client=FakeClient(),
        )
    assert store.finalize_calls == []
    assert store.finish_calls[-1][1] == "error"


def test_bundled_demo_manifest_and_assets_are_consistent() -> None:
    root = Path(__file__).resolve().parents[1]
    demo = load_manifest(root / "demo/cedar-and-salt/restaurant.json")
    assert 25 <= len(demo.chunks) <= 35
    paths = [item.image_path for item in demo.chunks if item.image_path]
    assert len(paths) == 6
    for relative_path in paths:
        assert relative_path is not None
        asset = root / "demo/cedar-and-salt" / relative_path
        assert asset.exists()
        assert asset.suffix == ".webp"
