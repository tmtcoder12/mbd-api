"""
RAG chatbot with streaming web API.

Web server mode:
  python rag-chatbot.py serve [top_k] [port]

Environment flags:
  CHAT_PERSISTENCE=true|false        (default: true)
  SUPABASE_URL=...
  SUPABASE_SERVICE_ROLE_KEY=...
Request body for POST /api/chat-stream must include:
  message: string
  restaurantId: UUID string
  sessionToken: UUID string (optional; server generates if missing)
  widgetToken: signed JWT (required)
"""

import json
import os
import re
import hmac
import hashlib
import base64
import threading
import time
import uuid
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Generator, List, Optional, Tuple
from urllib.parse import urlparse

import numpy as np
from dotenv import load_dotenv
from openai import OpenAI

from supabase_store import SupabaseStore, SupabaseStoreError
try:
    import redis
except ModuleNotFoundError:
    redis = None


EMBED_MODEL = "text-embedding-3-small"
CHAT_MODEL = "gpt-5-mini"
QUERY_CLASSIFIER_MODEL_DEFAULT = "gpt-5-mini"
MAX_SOURCES_SENT = 4
MIN_SCORE_DEFAULT = 0.0
DEBUG_TIMINGS = True
EMBED_DIM = 1536

SESSION_TOKEN_RE = re.compile(r"^[0-9a-fA-F-]{36}$")

DEFAULT_SYSTEM_INSTRUCTIONS = (
    "You are a helpful restaurant waiter. You goal is to be humourous, charismatic to persuade customers to try food here.\n"
    "Answer using the provided Sources.\n"
    "When you mention a menu item, include its price if present.\n"
    "Keep answers brief.\n"
    "Do not invent items, prices, or details, and do not ask follow up questions"
)

QUERY_TYPE_LABELS = (
    "Operations",
    "Dietary",
    "Events",
    "Menu",
    "Transactions",
)

QUERY_CLASSIFIER_INSTRUCTIONS = (
    "Classify the user's restaurant query into exactly one category.\n"
    "Allowed labels: Operations, Dietary, Events, Menu, Transactions.\n"
    "Return exactly one label and no other text."
)


class SlidingWindowRateLimiter:
    def __init__(self, max_requests: int, window_seconds: int):
        self.max_requests = max(1, max_requests)
        self.window_seconds = max(1, window_seconds)
        self._hits: Dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str, max_requests: Optional[int] = None, window_seconds: Optional[int] = None) -> bool:
        max_req = max(1, int(max_requests if max_requests is not None else self.max_requests))
        window_s = max(1, int(window_seconds if window_seconds is not None else self.window_seconds))
        now = time.time()
        cutoff = now - window_s
        with self._lock:
            bucket = self._hits[key]
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if len(bucket) >= max_req:
                return False
            bucket.append(now)
            return True


class RedisSlidingWindowRateLimiter:
    def __init__(self, redis_url: str):
        if redis is None:
            raise RuntimeError("redis package is required when RATE_LIMIT_REDIS_URL is set")
        self.client = redis.Redis.from_url(redis_url)

    def allow(self, key: str, max_requests: int, window_seconds: int) -> bool:
        now = time.time()
        cutoff = now - max(1, window_seconds)
        pipe = self.client.pipeline()
        pipe.zremrangebyscore(key, 0, cutoff)
        pipe.zcard(key)
        removed, count = pipe.execute()
        if int(count) >= max(1, max_requests):
            return False
        member = f"{now:.6f}:{uuid.uuid4()}"
        pipe = self.client.pipeline()
        pipe.zadd(key, {member: now})
        pipe.expire(key, max(1, window_seconds))
        pipe.execute()
        return True


def _b64url_decode(text: str) -> bytes:
    pad = "=" * ((4 - len(text) % 4) % 4)
    return base64.urlsafe_b64decode((text + pad).encode("ascii"))


def parse_signing_keys(raw: str) -> Dict[str, bytes]:
    data = (raw or "").strip()
    if not data:
        return {}
    keys: Dict[str, bytes] = {}
    if data.startswith("{"):
        obj = json.loads(data)
        if not isinstance(obj, dict):
            raise ValueError("WIDGET_SIGNING_KEYS JSON must be an object")
        for kid, secret in obj.items():
            if not isinstance(kid, str) or not isinstance(secret, str):
                raise ValueError("WIDGET_SIGNING_KEYS JSON values must be strings")
            keys[kid.strip()] = secret.encode("utf-8")
        return keys

    # CSV fallback: kid1:secret1,kid2:secret2
    parts = [p.strip() for p in data.split(",") if p.strip()]
    for part in parts:
        if ":" not in part:
            raise ValueError("WIDGET_SIGNING_KEYS CSV must be kid:secret pairs")
        kid, secret = part.split(":", 1)
        keys[kid.strip()] = secret.strip().encode("utf-8")
    return keys


def verify_widget_token(
    token: str,
    keys: Dict[str, bytes],
    expected_restaurant_id: str,
    expected_origin: str,
    max_age_seconds: int,
) -> Dict[str, Any]:
    raw = (token or "").strip()
    if not raw:
        raise ValueError("widgetToken is required")
    if not keys:
        raise ValueError("WIDGET_SIGNING_KEYS is not configured on server")

    parts = raw.split(".")
    if len(parts) != 3:
        raise ValueError("widgetToken must be a JWT")
    head_b64, payload_b64, sig_b64 = parts

    try:
        header = json.loads(_b64url_decode(head_b64).decode("utf-8"))
        payload = json.loads(_b64url_decode(payload_b64).decode("utf-8"))
        sig = _b64url_decode(sig_b64)
    except Exception as exc:  # noqa: BLE001
        raise ValueError("widgetToken is malformed") from exc

    if not isinstance(header, dict) or not isinstance(payload, dict):
        raise ValueError("widgetToken is malformed")

    alg = header.get("alg")
    kid = header.get("kid")
    if alg != "HS256":
        raise ValueError("widgetToken alg must be HS256")
    if not isinstance(kid, str) or not kid:
        raise ValueError("widgetToken kid is required")
    key = keys.get(kid)
    if not key:
        raise ValueError("widgetToken kid is not recognized")

    signing_input = f"{head_b64}.{payload_b64}".encode("ascii")
    expected_sig = hmac.new(key, signing_input, hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expected_sig):
        raise ValueError("widgetToken signature is invalid")

    rid = str(payload.get("rid") or "")
    orig = str(payload.get("orig") or "")
    exp = payload.get("exp")
    now = int(time.time())

    if rid != expected_restaurant_id:
        raise ValueError("widgetToken restaurant mismatch")
    if orig != expected_origin:
        raise ValueError("widgetToken origin mismatch")
    if not isinstance(exp, int):
        raise ValueError("widgetToken exp is required")
    if exp < now:
        raise ValueError("widgetToken is expired")
    if exp > now + max(60, max_age_seconds):
        raise ValueError("widgetToken expiry exceeds allowed max age")

    return payload


def hash_client_ip(ip: str) -> str:
    return hashlib.sha256((ip or "unknown").encode("utf-8")).hexdigest()[:16]


def log_event(event: str, **fields: Any):
    record: Dict[str, Any] = {"event": event, "ts": int(time.time())}
    record.update(fields)
    print(json.dumps(record, ensure_ascii=False), flush=True)


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def build_widget_token(
    restaurant_id: str,
    origin: str,
    kid: str,
    secret: bytes,
    ttl_seconds: int,
) -> Tuple[str, int]:
    now = int(time.time())
    exp = now + max(60, int(ttl_seconds))
    header = {"alg": "HS256", "typ": "JWT", "kid": kid}
    payload = {"rid": restaurant_id, "orig": origin, "exp": exp, "iat": now, "v": 1}
    header_b64 = _b64url_encode(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    payload_b64 = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
    sig = hmac.new(secret, signing_input, hashlib.sha256).digest()
    token = f"{header_b64}.{payload_b64}.{_b64url_encode(sig)}"
    return token, exp


def parse_bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def normalize_or_create_session_token(token: str) -> Tuple[str, bool]:
    token = (token or "").strip()
    if not token:
        return str(uuid.uuid4()), True
    if not SESSION_TOKEN_RE.match(token):
        raise ValueError("sessionToken must be a UUID string")
    try:
        value = str(uuid.UUID(token))
    except ValueError as exc:
        raise ValueError("sessionToken must be a valid UUID") from exc
    return value, False


def normalize_restaurant_id(restaurant_id: str) -> str:
    raw = (restaurant_id or "").strip()
    if not raw:
        raise ValueError("restaurantId is required")
    try:
        return str(uuid.UUID(raw))
    except ValueError as exc:
        raise ValueError("restaurantId must be a valid UUID") from exc


def embed_query(client: OpenAI, text: str) -> np.ndarray:
    resp = client.embeddings.create(model=EMBED_MODEL, input=[text])
    v = np.array(resp.data[0].embedding, dtype=np.float32)
    v = v.reshape(1, -1)
    norm = np.linalg.norm(v, axis=1, keepdims=True)
    v = v / np.clip(norm, 1e-12, None)
    return v


def build_context(results: List[Dict[str, Any]]) -> str:
    blocks = []
    for i, r in enumerate(results, start=1):
        page_path = r.get("page_path", "")
        url = r.get("source_url", "")
        rtype = r.get("type", "")
        title = r.get("title", "")

        header = f"[S{i}] type={rtype} page_path={page_path} title={title} url={url}".strip()
        text = (r.get("text") or "").strip()
        blocks.append(f"{header}\n{text}")
    return "\n\n---\n\n".join(blocks)


def build_prompt(user_q: str, context: str) -> str:
    return f"User question:\n{user_q}\n\nSources:\n{context}"


def normalize_query_type_label(raw: str) -> Optional[str]:
    value = (raw or "").strip().strip("\"'`")
    if not value:
        return None

    def _clean(s: str) -> str:
        return re.sub(r"[^a-z]+", "", s.lower())

    first_line = value.splitlines()[0].strip()
    first_line_clean = _clean(first_line)
    for label in QUERY_TYPE_LABELS:
        if first_line_clean == _clean(label):
            return label

    # Fallback: accept when the full output mentions exactly one allowed label.
    lowered = value.lower()
    matches = [
        label
        for label in QUERY_TYPE_LABELS
        if re.search(rf"\b{re.escape(label.lower())}\b", lowered)
    ]
    if len(matches) == 1:
        return matches[0]
    return None


def classify_query_type(client: OpenAI, model: str, user_query: str) -> Tuple[Optional[str], str, Dict[str, Any]]:
    t0 = time.perf_counter()

    resp = client.responses.create(
        model=model,
        input=[
            {"role": "system", "content": QUERY_CLASSIFIER_INSTRUCTIONS},
            {"role": "user", "content": user_query},
        ],
        max_output_tokens=16
    )

    def _read(obj: Any, key: str) -> Any:
        if isinstance(obj, dict):
            return obj.get(key)
        return getattr(obj, key, None)

    def extract_chat_text(resp_obj: Any) -> str:
        # easiest case for Responses API
        output_text = _read(resp_obj, "output_text")
        if isinstance(output_text, str) and output_text.strip():
            return output_text.strip()

        parts: List[str] = []

        output = _read(resp_obj, "output")
        if isinstance(output, list):
            for msg in output:
                msg_type = _read(msg, "type")
                if msg_type != "message":
                    continue

                content = _read(msg, "content")
                if not isinstance(content, list):
                    continue

                for block in content:
                    text = _read(block, "text")
                    if isinstance(text, str) and text.strip():
                        parts.append(text.strip())

        return "\n".join(parts).strip()

    raw = extract_chat_text(resp)

    meta = {
        "response_id": _read(resp, "id"),
        "has_output_text": bool(raw),
        "output_items_count": len(_read(resp, "output")) if isinstance(_read(resp, "output"), list) else None,
        "raw_output_len": len(raw or ""),
        "latency_ms": int((time.perf_counter() - t0) * 1000),
    }

    return normalize_query_type_label(raw), raw, meta

def stream_llm_deltas(client: OpenAI, prompt: str, system_instructions: str) -> Generator[str, None, None]:
    with client.responses.stream(
        model=CHAT_MODEL,
        input=[
            {"role": "system", "content": [{"type": "input_text", "text": system_instructions}]},
            {"role": "user", "content": [{"type": "input_text", "text": prompt}]},
        ],
    ) as stream:
        for event in stream:
            etype = getattr(event, "type", None)

            if etype == "response.output_text.delta":
                delta = getattr(event, "delta", None)
                if isinstance(delta, str) and delta:
                    yield delta
                continue

            if etype == "response.delta":
                delta = getattr(event, "delta", None)
                if isinstance(delta, str) and delta:
                    yield delta
                elif isinstance(delta, dict):
                    seg = delta.get("text") or delta.get("content")
                    if isinstance(seg, str) and seg:
                        yield seg


class Retriever:
    def __init__(
        self,
        supabase_store: SupabaseStore,
    ):
        self.supabase_store = supabase_store

    def retrieve(
        self,
        client: OpenAI,
        user_q: str,
        top_k: int,
        min_score: float,
        restaurant_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        t0 = time.perf_counter()
        qvec = embed_query(client, user_q)
        t1 = time.perf_counter()

        if not restaurant_id:
            raise RuntimeError("restaurantId is required for retrieval")
        rows = self.supabase_store.match_chunks(
            restaurant_id,
            qvec[0].tolist(),
            match_count=top_k,
            min_score=min_score,
        )
        results = []
        for row in rows:
            results.append(
                {
                    "id": row.get("id"),
                    "text": row.get("text", ""),
                    "type": row.get("type", ""),
                    "source_url": row.get("source_url", ""),
                    "page_path": row.get("page_path", ""),
                    "title": row.get("title", ""),
                    "score": float(row.get("score") or 0.0),
                }
            )

        t2 = time.perf_counter()
        results_for_llm = results[:MAX_SOURCES_SENT]
        context = build_context(results_for_llm)
        t3 = time.perf_counter()

        if DEBUG_TIMINGS:
            embed_ms = (t1 - t0) * 1000
            search_ms = (t2 - t1) * 1000
            prep_ms = (t3 - t2) * 1000
            top_score = float(results[0]["score"]) if results else 0.0
            print(
                f"[debug] backend=supabase top_score={top_score:.3f} | "
                f"embed={embed_ms:.1f}ms search={search_ms:.1f}ms prep={prep_ms:.1f}ms"
            )

        return {
            "prompt": build_prompt(user_q, context),
            "results": results,
            "timing_start": t0,
        }


class ChatHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    client: OpenAI = None
    retriever: Retriever = None
    top_k: int = 8
    min_score: float = 0.0
    store: Optional[SupabaseStore] = None
    persist_chat: bool = False
    allow_localhost_origins: bool = True
    rate_limiter: Optional[SlidingWindowRateLimiter] = None
    redis_rate_limiter: Optional[RedisSlidingWindowRateLimiter] = None
    widget_signing_keys: Dict[str, bytes] = {}
    widget_active_kid: str = "v1"
    default_ip_max_requests: int = 30
    default_ip_window_seconds: int = 60
    default_session_max_requests: int = 45
    default_session_window_seconds: int = 60
    default_token_issue_max_requests: int = 30
    default_token_issue_window_seconds: int = 60
    default_token_max_age_seconds: int = 900
    query_classifier_model: str = QUERY_CLASSIFIER_MODEL_DEFAULT
    _cors_origin: Optional[str] = None

    def end_headers(self):
        if self._cors_origin:
            self.send_header("Access-Control-Allow-Origin", self._cors_origin)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        super().end_headers()

    def _normalize_origin(self, origin: str) -> str:
        normalized = (origin or "").strip().lower()
        return normalized.rstrip("/")

    def _is_localhost_origin(self, origin: str) -> bool:
        if not origin:
            return False
        parsed = urlparse(origin)
        return parsed.scheme in {"http", "https"} and parsed.hostname in {"localhost", "127.0.0.1"}

    def _is_origin_allowed_preflight(self, origin: str) -> bool:
        normalized = self._normalize_origin(origin)
        if not normalized:
            return False
        if self.allow_localhost_origins and self._is_localhost_origin(normalized):
            return True
        if self.store is None:
            return False
        try:
            return self.store.origin_exists(normalized)
        except SupabaseStoreError:
            return False

    def _is_origin_allowed_for_restaurant(self, restaurant_id: str, origin: str) -> bool:
        normalized = self._normalize_origin(origin)
        if not normalized:
            return False
        if self.allow_localhost_origins and self._is_localhost_origin(normalized):
            return True
        if self.store is None:
            return False
        return self.store.origin_allowed_for_restaurant(restaurant_id, normalized)

    def _client_ip(self) -> str:
        # Prefer reverse-proxy headers when available.
        xff = (self.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
        if xff:
            return xff
        real_ip = (self.headers.get("X-Real-IP") or "").strip()
        if real_ip:
            return real_ip
        return (self.client_address[0] if self.client_address else "") or "unknown"

    def _send_json_error(self, status: int, message: str, request_id: Optional[str] = None):
        payload = json.dumps({"error": message}, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        if request_id:
            self.send_header("X-Request-Id", request_id)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _write_chunk(self, payload: str):
        encoded = payload.encode("utf-8")
        self.wfile.write(f"{len(encoded):X}\r\n".encode("ascii"))
        self.wfile.write(encoded)
        self.wfile.write(b"\r\n")
        self.wfile.flush()

    def _end_chunked(self):
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    def _extract_client_meta(self) -> Dict[str, Any]:
        return {
            "user_agent": self.headers.get("User-Agent", ""),
            "accept_language": self.headers.get("Accept-Language", ""),
        }

    def _source_refs(self, results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        refs = []
        for row in results[:MAX_SOURCES_SENT]:
            refs.append(
                {
                    "chunk_id": row.get("id"),
                    "score": float(row.get("score") or 0.0),
                    "source_url": row.get("source_url", ""),
                    "title": row.get("title", ""),
                }
            )
        return refs

    def _load_security_settings(
        self,
        restaurant_id: str,
        request_id: str,
    ) -> Optional[Dict[str, int]]:
        settings = {
            "ip_max_requests": self.default_ip_max_requests,
            "ip_window_seconds": self.default_ip_window_seconds,
            "session_max_requests": self.default_session_max_requests,
            "session_window_seconds": self.default_session_window_seconds,
            "token_max_age_seconds": self.default_token_max_age_seconds,
            "token_issue_max_requests": self.default_token_issue_max_requests,
            "token_issue_window_seconds": self.default_token_issue_window_seconds,
        }
        if self.store is None:
            return settings
        try:
            if not self.store.restaurant_exists(restaurant_id):
                self._send_json_error(400, "restaurantId does not exist", request_id=request_id)
                return None
            loaded = self.store.get_restaurant_security_settings(restaurant_id)
            settings.update(loaded)
        except SupabaseStoreError as exc:
            self._send_json_error(502, f"Failed to validate restaurantId: {exc}", request_id=request_id)
            return None
        return settings

    def _load_system_instructions(self, restaurant_id: str, request_id: str) -> Optional[str]:
        if self.store is None:
            return DEFAULT_SYSTEM_INSTRUCTIONS
        try:
            custom_prompt = self.store.get_restaurant_system_prompt(restaurant_id)
        except SupabaseStoreError as exc:
            self._send_json_error(502, f"Failed to load restaurant system_prompt: {exc}", request_id=request_id)
            return None
        if custom_prompt:
            return custom_prompt
        return DEFAULT_SYSTEM_INSTRUCTIONS

    def _schedule_query_type_classification(
        self,
        message_id: Optional[str],
        user_query: str,
        restaurant_id: str,
        request_id: str,
    ) -> None:
        if not message_id or self.store is None:
            return

        client = self.client
        store = self.store
        model = self.query_classifier_model

        def _worker():
            t0 = time.perf_counter()
            log_event(
                "query_classification_start",
                request_id=request_id,
                restaurant_id=restaurant_id,
                message_id=message_id,
                model=model,
            )
            log_event(
                "query_classification_llm_request",
                request_id=request_id,
                restaurant_id=restaurant_id,
                message_id=message_id,
                model=model,
                query_preview=user_query[:120],
                prompt_hash=hashlib.sha256(QUERY_CLASSIFIER_INSTRUCTIONS.encode("utf-8")).hexdigest()[:16],
            )
            try:
                label, raw_label_output, meta = classify_query_type(client, model, user_query)
                log_event(
                    "query_classification_llm_response",
                    request_id=request_id,
                    restaurant_id=restaurant_id,
                    message_id=message_id,
                    model=model,
                    response_id=meta.get("response_id"),
                    has_output_text=meta.get("has_output_text"),
                    output_items_count=meta.get("output_items_count"),
                    finish_reason=meta.get("finish_reason"),
                    raw_output_len=meta.get("raw_output_len"),
                    llm_latency_ms=meta.get("latency_ms"),
                )
                if label is None:
                    log_event(
                        "query_classification_failed",
                        request_id=request_id,
                        restaurant_id=restaurant_id,
                        message_id=message_id,
                        reason="invalid_label",
                        raw_output=(raw_label_output or "")[:200],
                        latency_ms=int((time.perf_counter() - t0) * 1000),
                    )
                    return
                store.update_message_query_type(message_id, label)
                log_event(
                    "query_classification_complete",
                    request_id=request_id,
                    restaurant_id=restaurant_id,
                    message_id=message_id,
                    query_type=label,
                    latency_ms=int((time.perf_counter() - t0) * 1000),
                )
            except Exception as exc:  # noqa: BLE001
                log_event(
                    "query_classification_failed",
                    request_id=request_id,
                    restaurant_id=restaurant_id,
                    message_id=message_id,
                    error=str(exc),
                    latency_ms=int((time.perf_counter() - t0) * 1000),
                )

        threading.Thread(target=_worker, daemon=True).start()

    def _origin_guard(self, restaurant_id: str, origin: str, request_id: str) -> bool:
        try:
            if not self._is_origin_allowed_for_restaurant(restaurant_id, origin):
                self._send_json_error(403, "Origin is not allowed for this restaurant", request_id=request_id)
                if self.store is not None:
                    try:
                        self.store.insert_audit_event(
                            "origin_mismatch",
                            restaurant_id=restaurant_id,
                            actor="chat_api",
                            details={"request_id": request_id, "origin": origin},
                        )
                    except SupabaseStoreError:
                        pass
                return False
            return True
        except SupabaseStoreError as exc:
            self._send_json_error(502, f"Failed to validate origin allowlist: {exc}", request_id=request_id)
            return False

    def _apply_rate_limit(
        self,
        keys: List[Tuple[str, int, int]],
        request_id: str,
        restaurant_id: str,
        origin: str,
        client_ip_hash: str,
    ) -> bool:
        for rate_key, max_requests, window_seconds in keys:
            allowed = True
            if self.redis_rate_limiter is not None:
                allowed = self.redis_rate_limiter.allow(rate_key, max_requests=max_requests, window_seconds=window_seconds)
            elif self.rate_limiter is not None:
                allowed = self.rate_limiter.allow(rate_key, max_requests=max_requests, window_seconds=window_seconds)
            if not allowed:
                if self.store is not None:
                    try:
                        self.store.insert_audit_event(
                            "rate_limit_block",
                            restaurant_id=restaurant_id,
                            actor="chat_api",
                            details={
                                "request_id": request_id,
                                "origin": origin,
                                "client_ip_hash": client_ip_hash,
                                "key": rate_key,
                                "max_requests": max_requests,
                                "window_seconds": window_seconds,
                            },
                        )
                    except SupabaseStoreError:
                        pass
                self._send_json_error(429, "Rate limit exceeded. Please try again in a minute.", request_id=request_id)
                return False
        return True

    def do_OPTIONS(self):
        origin = self._normalize_origin(self.headers.get("Origin", ""))
        if not self._is_origin_allowed_preflight(origin):
            self.send_response(403)
            self.end_headers()
            return
        self._cors_origin = origin
        self.send_response(204)
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path).path
        if parsed == "/healthz":
            payload = json.dumps({"ok": True}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        self._send_json_error(404, "Not found")

    def do_POST(self):
        request_id = str(uuid.uuid4())
        start = time.perf_counter()
        parsed = urlparse(self.path).path
        if parsed == "/api/chat-stream":
            self._handle_chat_stream(request_id, start)
            return
        if parsed == "/api/widget-token":
            self._handle_widget_token(request_id, start)
            return
        self._send_json_error(404, "Not found", request_id=request_id)

    def _handle_widget_token(self, request_id: str, start: float):
        origin = self._normalize_origin(self.headers.get("Origin", ""))
        if not origin:
            self._send_json_error(403, "Origin header is required", request_id=request_id)
            return
        self._cors_origin = origin

        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length > 0 else b"{}"
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, json.JSONDecodeError):
            self._send_json_error(400, "Invalid JSON body", request_id=request_id)
            return

        try:
            restaurant_id = normalize_restaurant_id(payload.get("restaurantId") or "")
        except ValueError as exc:
            self._send_json_error(400, str(exc), request_id=request_id)
            return

        security_settings = self._load_security_settings(restaurant_id, request_id)
        if security_settings is None:
            return
        if not self._origin_guard(restaurant_id, origin, request_id):
            return

        client_ip_hash = hash_client_ip(self._client_ip())
        token_rate_key = f"rl:token:{restaurant_id}:{client_ip_hash}"
        if not self._apply_rate_limit(
            [
                (
                    token_rate_key,
                    int(security_settings["token_issue_max_requests"]),
                    int(security_settings["token_issue_window_seconds"]),
                )
            ],
            request_id=request_id,
            restaurant_id=restaurant_id,
            origin=origin,
            client_ip_hash=client_ip_hash,
        ):
            return

        kid = self.widget_active_kid
        secret = self.widget_signing_keys.get(kid)
        if secret is None:
            self._send_json_error(500, "Active widget signing key is not configured", request_id=request_id)
            return
        token, exp = build_widget_token(
            restaurant_id=restaurant_id,
            origin=origin,
            kid=kid,
            secret=secret,
            ttl_seconds=int(security_settings["token_max_age_seconds"]),
        )

        response = {"widgetToken": token, "expiresAt": exp}
        data = json.dumps(response, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("X-Request-Id", request_id)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
        log_event(
            "widget_token_issue",
            request_id=request_id,
            restaurant_id=restaurant_id,
            origin=origin,
            client_ip_hash=client_ip_hash,
            status=200,
            latency_ms=int((time.perf_counter() - start) * 1000),
            expires_at=exp,
        )

    def _handle_chat_stream(self, request_id: str, start: float):
        origin = self._normalize_origin(self.headers.get("Origin", ""))
        if not origin:
            self._send_json_error(403, "Origin header is required", request_id=request_id)
            return
        self._cors_origin = origin

        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length > 0 else b"{}"
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, json.JSONDecodeError):
            self._send_json_error(400, "Invalid JSON body", request_id=request_id)
            return

        user_msg = (payload.get("message") or "").strip()
        if not user_msg:
            self._send_json_error(400, "message is required", request_id=request_id)
            return

        try:
            session_token, generated = normalize_or_create_session_token(payload.get("sessionToken") or "")
        except ValueError as exc:
            self._send_json_error(400, str(exc), request_id=request_id)
            return

        try:
            restaurant_id = normalize_restaurant_id(payload.get("restaurantId") or "")
        except ValueError as exc:
            self._send_json_error(400, str(exc), request_id=request_id)
            return

        security_settings = self._load_security_settings(restaurant_id, request_id)
        if security_settings is None:
            return
        if not self._origin_guard(restaurant_id, origin, request_id):
            return

        widget_token = (payload.get("widgetToken") or "").strip()
        try:
            verify_widget_token(
                widget_token,
                self.widget_signing_keys,
                expected_restaurant_id=restaurant_id,
                expected_origin=origin,
                max_age_seconds=int(security_settings["token_max_age_seconds"]),
            )
        except ValueError as exc:
            self._send_json_error(403, str(exc), request_id=request_id)
            return

        system_instructions = self._load_system_instructions(restaurant_id, request_id)
        if system_instructions is None:
            return

        client_ip_hash = hash_client_ip(self._client_ip())
        ip_rate_key = f"rl:ip:{restaurant_id}:{client_ip_hash}"
        session_rate_key = f"rl:session:{restaurant_id}:{session_token}"
        if not self._apply_rate_limit(
            [
                (
                    ip_rate_key,
                    int(security_settings["ip_max_requests"]),
                    int(security_settings["ip_window_seconds"]),
                ),
                (
                    session_rate_key,
                    int(security_settings["session_max_requests"]),
                    int(security_settings["session_window_seconds"]),
                ),
            ],
            request_id=request_id,
            restaurant_id=restaurant_id,
            origin=origin,
            client_ip_hash=client_ip_hash,
        ):
            return

        session_id = None
        user_message_id = None
        if self.persist_chat:
            if self.store is None:
                self._send_json_error(
                    500,
                    "Chat persistence is enabled but Supabase is not configured",
                    request_id=request_id,
                )
                return
            try:
                session_id = self.store.upsert_session(restaurant_id, session_token, self._extract_client_meta())
                user_row = self.store.insert_message(session_id, "user", user_msg, return_row=True)
                if user_row is not None:
                    row_id = user_row.get("id")
                    if row_id:
                        user_message_id = str(row_id)
            except SupabaseStoreError as exc:
                self._send_json_error(502, f"Failed to persist chat session: {exc}", request_id=request_id)
                return

        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("X-Request-Id", request_id)
        self.end_headers()

        assistant_text = ""
        prep = None
        try:
            session_line = json.dumps(
                {
                    "type": "session",
                    "sessionToken": session_token,
                    "restaurantId": restaurant_id,
                    "generated": generated,
                }
            ) + "\n"
            self._write_chunk(session_line)

            prep = self.retriever.retrieve(
                self.client,
                user_msg,
                self.top_k,
                self.min_score,
                restaurant_id=restaurant_id,
            )
            prompt = prep["prompt"]
            results = prep["results"]
            first_token_sent = False

            for delta in stream_llm_deltas(self.client, prompt, system_instructions):
                first_token_sent = True
                assistant_text += delta
                line = json.dumps({"type": "delta", "content": delta}, ensure_ascii=False) + "\n"
                self._write_chunk(line)

            done_line = json.dumps({"type": "done"}) + "\n"
            self._write_chunk(done_line)
            self._end_chunked()

            self._schedule_query_type_classification(
                user_message_id,
                user_msg,
                restaurant_id=restaurant_id,
                request_id=request_id,
            )

            if self.persist_chat and session_id is not None:
                latency_ms = int((time.perf_counter() - prep["timing_start"]) * 1000)
                self.store.insert_message(
                    session_id,
                    "assistant",
                    assistant_text.strip(),
                    sources=self._source_refs(results),
                    latency_ms=latency_ms,
                    delivery_status="complete",
                )

            if DEBUG_TIMINGS:
                elapsed_ms = (time.perf_counter() - prep["timing_start"]) * 1000
                print(
                    f"[debug] web request complete | backend=supabase "
                    f"first_token={first_token_sent} total={elapsed_ms:.1f}ms"
                )
            log_event(
                "chat_request",
                request_id=request_id,
                restaurant_id=restaurant_id,
                origin=origin,
                client_ip_hash=client_ip_hash,
                status=200,
                latency_ms=int((time.perf_counter() - start) * 1000),
                sources_count=len(results),
            )
        except Exception as exc:  # noqa: BLE001
            err_line = json.dumps({"type": "error", "message": str(exc)}, ensure_ascii=False) + "\n"
            try:
                self._write_chunk(err_line)
                self._end_chunked()
            except Exception:  # noqa: BLE001
                pass

            self._schedule_query_type_classification(
                user_message_id,
                user_msg,
                restaurant_id=restaurant_id,
                request_id=request_id,
            )

            if self.persist_chat and session_id is not None:
                try:
                    latency_ms = None
                    if prep is not None:
                        latency_ms = int((time.perf_counter() - prep["timing_start"]) * 1000)
                    self.store.insert_message(
                        session_id,
                        "assistant",
                        (assistant_text or "").strip() or "[stream aborted]",
                        sources=[],
                        latency_ms=latency_ms,
                        delivery_status="error",
                    )
                except Exception:  # noqa: BLE001
                    pass
            log_event(
                "chat_request",
                request_id=request_id,
                restaurant_id=restaurant_id,
                origin=origin,
                client_ip_hash=client_ip_hash,
                status=500,
                error=str(exc),
                latency_ms=int((time.perf_counter() - start) * 1000),
            )


def make_supabase_store_if_configured() -> Optional[SupabaseStore]:
    supabase_url = os.getenv("SUPABASE_URL", "").strip()
    service_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    if not supabase_url or not service_key:
        return None
    return SupabaseStore(supabase_url, service_key)


def serve(top_k: int = 8, port: int = 8000):
    load_dotenv()
    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

    persist_chat = parse_bool_env("CHAT_PERSISTENCE", True)
    min_score = float(os.getenv("MIN_SCORE_DEFAULT", str(MIN_SCORE_DEFAULT)))
    rate_limit_rpm = int(os.getenv("RATE_LIMIT_REQUESTS_PER_MINUTE", "30"))
    rate_limit_window_s = int(os.getenv("RATE_LIMIT_WINDOW_SECONDS", "60"))
    session_rate_limit_rpm = int(os.getenv("SESSION_RATE_LIMIT_REQUESTS_PER_MINUTE", "45"))
    session_rate_limit_window_s = int(os.getenv("SESSION_RATE_LIMIT_WINDOW_SECONDS", "60"))
    token_issue_rate_limit_rpm = int(os.getenv("TOKEN_ISSUE_RATE_LIMIT_REQUESTS_PER_MINUTE", "30"))
    token_issue_rate_limit_window_s = int(os.getenv("TOKEN_ISSUE_RATE_LIMIT_WINDOW_SECONDS", "60"))
    token_max_age_s = int(os.getenv("WIDGET_TOKEN_MAX_AGE_SECONDS", "900"))
    allow_localhost_origins = parse_bool_env("ALLOW_LOCALHOST_ORIGINS", True)
    redis_rate_limit_url = (os.getenv("RATE_LIMIT_REDIS_URL") or "").strip()
    signing_keys_raw = os.getenv("WIDGET_SIGNING_KEYS", "").strip()
    widget_active_kid = (os.getenv("WIDGET_ACTIVE_KID") or "v1").strip()
    query_classifier_model = (os.getenv("QUERY_CLASSIFIER_MODEL") or QUERY_CLASSIFIER_MODEL_DEFAULT).strip()
    try:
        widget_signing_keys = parse_signing_keys(signing_keys_raw)
    except ValueError as exc:
        raise SystemExit(f"Invalid WIDGET_SIGNING_KEYS: {exc}") from exc

    if not widget_signing_keys:
        raise SystemExit("WIDGET_SIGNING_KEYS must be configured for production token verification")
    if widget_active_kid not in widget_signing_keys:
        raise SystemExit("WIDGET_ACTIVE_KID must reference a key present in WIDGET_SIGNING_KEYS")

    store = make_supabase_store_if_configured()

    if store is None:
        raise SystemExit("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are required")
    retriever = Retriever(supabase_store=store)

    if persist_chat and store is None:
        raise SystemExit("CHAT_PERSISTENCE=true requires SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY")

    ChatHandler.client = client
    ChatHandler.retriever = retriever
    ChatHandler.top_k = top_k
    ChatHandler.min_score = min_score
    ChatHandler.store = store
    ChatHandler.persist_chat = persist_chat
    ChatHandler.allow_localhost_origins = allow_localhost_origins
    ChatHandler.rate_limiter = SlidingWindowRateLimiter(rate_limit_rpm, rate_limit_window_s)
    if redis_rate_limit_url:
        ChatHandler.redis_rate_limiter = RedisSlidingWindowRateLimiter(redis_rate_limit_url)
        ChatHandler.rate_limiter = None
    else:
        ChatHandler.redis_rate_limiter = None
    ChatHandler.widget_signing_keys = widget_signing_keys
    ChatHandler.widget_active_kid = widget_active_kid
    ChatHandler.default_ip_max_requests = rate_limit_rpm
    ChatHandler.default_ip_window_seconds = rate_limit_window_s
    ChatHandler.default_session_max_requests = session_rate_limit_rpm
    ChatHandler.default_session_window_seconds = session_rate_limit_window_s
    ChatHandler.default_token_issue_max_requests = token_issue_rate_limit_rpm
    ChatHandler.default_token_issue_window_seconds = token_issue_rate_limit_window_s
    ChatHandler.default_token_max_age_seconds = token_max_age_s
    ChatHandler.query_classifier_model = query_classifier_model

    server = ThreadingHTTPServer(("0.0.0.0", port), ChatHandler)
    print(f"✅ Chat server running at http://localhost:{port}")
    print("   - Retrieval backend: supabase")
    print(f"   - Chat persistence: {'enabled' if persist_chat else 'disabled'}")
    print("   - Stream API: POST /api/chat-stream")
    print("   - Token API: POST /api/widget-token")
    print("   - Health API: GET /healthz")
    print(
        f"   - Rate limit: {rate_limit_rpm} requests / {rate_limit_window_s}s per (restaurant_id, client_ip)"
    )
    print(
        f"   - Session rate limit: {session_rate_limit_rpm} requests / {session_rate_limit_window_s}s per session"
    )
    print(
        f"   - Token issue rate limit: {token_issue_rate_limit_rpm} requests / "
        f"{token_issue_rate_limit_window_s}s per (restaurant_id, client_ip)"
    )
    print(f"   - Allow localhost origins: {'yes' if allow_localhost_origins else 'no'}")
    print(f"   - Redis limiter: {'enabled' if redis_rate_limit_url else 'disabled (in-memory fallback)'}")
    print(f"   - Signing keys loaded: {len(widget_signing_keys)}")
    print(f"   - Active widget key id: {widget_active_kid}")
    print(f"   - Query classifier model: {query_classifier_model}")
    server.serve_forever()


if __name__ == "__main__":
    import sys

    if len(sys.argv) >= 2 and sys.argv[1] == "serve":
        top_k = 8
        port = 8000

        if len(sys.argv) >= 3:
            top_k = int(sys.argv[2])
            port = int(sys.argv[3]) if len(sys.argv) >= 4 else 8000

        serve(top_k=top_k, port=port)
    else:
        print("Usage: python rag-chatbot.py serve [top_k] [port]")
        raise SystemExit(1)


'''
The Logic Flow in this File:

User Post: Receives JSON message + Session Token.Log to Supabase: Save the user's question.
Embed: Turn the question into a vector ($1536$ dimensions).
Retrieve: Find the top-k most similar text chunks from Supabase.
Construct Prompt: Wrap those chunks in your "Charismatic Restaurant Assistant" instructions.
Stream: Pipe the LLM's words to the user's screen.
Final Log to Supabase: Save the complete answer and the sources used.
'''
