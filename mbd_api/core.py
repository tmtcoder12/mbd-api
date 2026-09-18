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
  newSession: boolean (optional; ignore sessionToken and start a fresh persisted session)
  widgetToken: signed JWT (required)
  language: ISO 639-3 string (optional; defaults to session language or eng)
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
from typing import Any, Callable, Dict, Generator, List, Optional, Tuple
from urllib.parse import urlparse

import numpy as np
from dotenv import load_dotenv
from openai import OpenAI

from .repository import SupabaseStore, SupabaseStoreError
from .billing import (
    StripeConfigError,
    StripeSdkError,
    StripeWebhookSignatureError,
    ensure_stripe_configured,
    ensure_stripe_sdk_available,
    process_stripe_webhook,
)
try:
    import redis
except ModuleNotFoundError:
    redis = None


EMBED_MODEL = "text-embedding-3-small"
CHAT_MODEL = "gpt-5-mini"
QUERY_CLASSIFIER_MODEL_DEFAULT = "gpt-5-mini"
QUERY_CACHE_CLASSIFIER_MODEL_DEFAULT = "gpt-5-nano"
QUERY_CACHE_CLASSIFIER_TIMEOUT_MS_DEFAULT = 250
MAX_SOURCES_SENT = 4
MIN_SCORE_DEFAULT = 0.0
DEBUG_TIMINGS = True
EMBED_DIM = 1536

SESSION_TOKEN_RE = re.compile(r"^[0-9a-fA-F-]{36}$")

DEFAULT_SYSTEM_INSTRUCTIONS = (
    "You are a restaurant assistant.\n"
    "Answer naturally, briefly, and helpfully.\n"
    "Use ONLY the provided menu context for factual claims.\n"
    "If the menu context does not support a fact, say you are not sure.\n"
    "Resolve follow-up references using session context when confidence is high.\n"
    "If reference ambiguity is high, ask one short clarifying question.\n"
    "Mention specific item names clearly instead of vague pronouns.\n"
    "Do not invent ingredients, prices, sides, dietary tags, or availability."
)

DEFAULT_SESSION_LANGUAGE = "eng"

# Keep this allowlist aligned with the frontend language selector.
SUPPORTED_SESSION_LANGUAGE_CODES = frozenset(
   {
    "cmn", #Mandarin
    "eng", # English
    "fra", # French
    "hin", # Hindi
    "jpn", # Japanese
    "kor", # Korean
    "spa", # Spanish
 }
)

DEFAULT_SESSION_STATE = {
    "session_id": "",
    "last_response_id": None,
    "last_discussed_item_ids": [],
    "last_candidate_item_ids": [],
    "last_intent": None,
    "active_constraints": {},
}

INTENT_KEYWORDS = {
    "compare_price": ("cheaper", "cheapest", "price", "cost", "less expensive"),
    "ingredients": ("comes with", "include", "ingredients", "what's in", "what is in"),
    "dietary_check": ("vegetarian", "vegan", "gluten", "dairy-free", "dairy free", "allergy", "halal"),
    "spice_check": ("spicy", "heat", "mild", "hot"),
    "lighter_option": ("lighter", "light", "lower calorie", "healthier"),
}

CATEGORY_KEYWORDS = (
    "burger",
    "pizza",
    "sandwich",
    "salad",
    "appetizer",
    "drink",
    "dessert",
    "pasta",
    "curry",
    "biryani",
    "wrap",
)

MAX_IMAGE_URLS_SENT = 3

DEFAULT_IMAGE_DECISION = {
    "include_images": False,
    "max_images": 0,
    "target_item_names": [],
}

ASSISTANT_TEXT_START_MARKER = "<<<MINTGEN_ASSISTANT_TEXT_START>>>"
ASSISTANT_TEXT_END_MARKER = "<<<MINTGEN_ASSISTANT_TEXT_END>>>"
IMAGE_DECISION_JSON_START_MARKER = "<<<MINTGEN_IMAGE_DECISION_JSON_START>>>"
IMAGE_DECISION_JSON_END_MARKER = "<<<MINTGEN_IMAGE_DECISION_JSON_END>>>"

GENERIC_IMAGE_TITLES = {
    "",
    "menu",
    "menu item",
    "menu items",
    "food menu",
    "items",
    "dish",
    "dishes",
}

QUERY_TYPE_LABELS = (
    "Operations",
    "Dietary",
    "Events",
    "Menu",
    "Transactions",
)

QUERY_CLASSIFIER_INSTRUCTIONS = (
    "Classify the user's restaurant query into exactly one category.\n"
    "Allowed labels: Operations, Dietary, Events, Menu, Transactions, Other .\n"
    "Return exactly one label and no other text."
)

QUERY_CACHE_CLASSIFIER_INSTRUCTIONS = (
    "Decide whether this user message is a standalone, normal restaurant-related query that is safe to cache.\n"
    "Return exactly one label: CACHEABLE or NOT_CACHEABLE.\n"
    "Use CACHEABLE for direct restaurant questions (menu, hours, location, reservations, pricing, dietary, events, ordering).\n"
    "Use NOT_CACHEABLE for non-restaurant topics, chit-chat, personal requests, abuse, or contextual follow-ups.\n"
    "Return only the label."
)

QUERY_CACHE_NAMESPACE_DEFAULT = "qcache:v1"
QUERY_CACHE_TTL_SECONDS_DEFAULT = 900
QUERY_CACHE_SCHEMA_VERSION = 1
QUERY_CACHE_SEMANTIC_THRESHOLD_DEFAULT = 0.8
QUERY_CACHE_SEMANTIC_MAX_CANDIDATES_DEFAULT = 200
QUERY_CACHE_REQUIRE_RESTAURANT_RELEVANCE_DEFAULT = True

RESTAURANT_RELEVANCE_KEYWORDS = {
    "menu",
    "menus",
    "item",
    "items",
    "dish",
    "dishes",
    "food",
    "drink",
    "drinks",
    "beverage",
    "beverages",
    "cocktail",
    "cocktails",
    "mocktail",
    "mocktails",
    "special",
    "specials",
    "popular",
    "best",
    "recommend",
    "recommended",
    "recommendation",
    "recommendations",
    "recs",
    "suggest",
    "suggestion",
    "suggestions",
    "hours",
    "open",
    "close",
    "closing",
    "location",
    "address",
    "parking",
    "reservation",
    "reservations",
    "book",
    "booking",
    "waitlist",
    "delivery",
    "pickup",
    "takeout",
    "dinein",
    "dine",
    "price",
    "prices",
    "cost",
    "costs",
    "cheap",
    "cheaper",
    "cheapest",
    "dietary",
    "allergen",
    "allergens",
    "allergy",
    "allergies",
    "gluten",
    "vegan",
    "vegetarian",
    "spicy",
    "catering",
    "event",
    "events",
    "party",
    "parties",
    "private",
    "payment",
    "payments",
    "pay",
}

RESTAURANT_RELEVANCE_PHRASES = (
    "what are your hours",
    "when are you open",
    "where are you located",
    "do you have parking",
    "do you take reservations",
    "can i reserve",
    "can i book",
    "do you deliver",
    "do you do takeout",
    "what are your specials",
    "what do you recommend",
    "top picks",
)

CONTEXT_RESET_KEYWORDS = {
    "hour",
    "hours",
    "open",
    "close",
    "closing",
    "location",
    "address",
    "parking",
    "reservation",
    "reservations",
    "waitlist",
    "delivery",
    "pickup",
    "takeout",
    "catering",
    "event",
    "events",
    "payment",
    "payments",
    "pay",
    "phone",
    "contact",
}


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
    def __init__(self, redis_url: str, timeout_s: float = 3.0):
        if redis is None:
            raise RuntimeError("redis package is required when RATE_LIMIT_REDIS_URL is set")
        self.client = redis.Redis.from_url(
            redis_url,
            socket_timeout=timeout_s,
            socket_connect_timeout=timeout_s,
            retry_on_timeout=False,
        )

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


class RedisQueryCache:
    def __init__(self, redis_url: str, timeout_s: float = 3.0):
        if redis is None:
            raise RuntimeError("redis package is required when query cache is enabled")
        self.client = redis.Redis.from_url(
            redis_url,
            socket_timeout=timeout_s,
            socket_connect_timeout=timeout_s,
            retry_on_timeout=False,
        )

    def _loads_json(self, raw: Any) -> Optional[Dict[str, Any]]:
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        if not isinstance(raw, str) or not raw.strip():
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if isinstance(data, dict):
            return data
        return None

    def _dumps_json(self, payload: Dict[str, Any]) -> str:
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    def _entry_key(self, group_prefix: str, entry_id: str) -> str:
        return f"{group_prefix}:entry:{entry_id}"

    def _exact_map_key(self, group_prefix: str, exact_query_hash: str) -> str:
        return f"{group_prefix}:exact:{exact_query_hash}"

    def _normalized_map_key(self, group_prefix: str, normalized_query_hash: str) -> str:
        return f"{group_prefix}:norm:{normalized_query_hash}"

    def _semantic_recent_key(self, group_prefix: str) -> str:
        return f"{group_prefix}:semantic:recent"

    def lookup_exact(self, group_prefix: str, exact_query_hash: str) -> Optional[Dict[str, Any]]:
        entry_id = self.client.get(self._exact_map_key(group_prefix, exact_query_hash))
        if isinstance(entry_id, bytes):
            entry_id = entry_id.decode("utf-8", errors="replace")
        if not isinstance(entry_id, str) or not entry_id.strip():
            return None
        raw = self.client.get(self._entry_key(group_prefix, entry_id))
        payload = self._loads_json(raw)
        if payload is None:
            self.client.delete(self._exact_map_key(group_prefix, exact_query_hash))
        return payload

    def lookup_normalized(self, group_prefix: str, normalized_query_hash: str) -> Optional[Dict[str, Any]]:
        entry_id = self.client.get(self._normalized_map_key(group_prefix, normalized_query_hash))
        if isinstance(entry_id, bytes):
            entry_id = entry_id.decode("utf-8", errors="replace")
        if not isinstance(entry_id, str) or not entry_id.strip():
            return None
        raw = self.client.get(self._entry_key(group_prefix, entry_id))
        payload = self._loads_json(raw)
        if payload is None:
            self.client.delete(self._normalized_map_key(group_prefix, normalized_query_hash))
        return payload

    def semantic_candidates(self, group_prefix: str, max_candidates: int) -> List[Dict[str, Any]]:
        limit = max(1, int(max_candidates))
        zkey = self._semantic_recent_key(group_prefix)
        entry_ids = self.client.zrevrange(zkey, 0, limit - 1)
        if not entry_ids:
            return []

        normalized_ids: List[str] = []
        for item in entry_ids:
            if isinstance(item, bytes):
                normalized_ids.append(item.decode("utf-8", errors="replace"))
            else:
                normalized_ids.append(str(item))

        keys = [self._entry_key(group_prefix, entry_id) for entry_id in normalized_ids]
        raws = self.client.mget(keys)
        out: List[Dict[str, Any]] = []
        stale_ids: List[str] = []
        for entry_id, raw in zip(normalized_ids, raws):
            payload = self._loads_json(raw)
            if payload is None:
                stale_ids.append(entry_id)
                continue
            payload["_entry_id"] = entry_id
            out.append(payload)

        if stale_ids:
            self.client.zrem(zkey, *stale_ids)
        return out

    def store_entry(
        self,
        group_prefix: str,
        exact_query_hash: str,
        normalized_query_hash: str,
        payload: Dict[str, Any],
        ttl_seconds: int,
    ) -> None:
        ttl = max(1, int(ttl_seconds))
        entry_id = str(uuid.uuid4())
        entry_key = self._entry_key(group_prefix, entry_id)
        exact_key = self._exact_map_key(group_prefix, exact_query_hash)
        norm_key = self._normalized_map_key(group_prefix, normalized_query_hash)
        zkey = self._semantic_recent_key(group_prefix)

        body = self._dumps_json(payload)
        now = time.time()
        pipe = self.client.pipeline()
        pipe.set(entry_key, body, ex=ttl)
        pipe.set(exact_key, entry_id, ex=ttl)
        pipe.set(norm_key, entry_id, ex=ttl)
        pipe.zadd(zkey, {entry_id: now})
        pipe.expire(zkey, ttl)
        pipe.execute()


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


def parse_bool_payload(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


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


def normalize_session_language(language: Any) -> Optional[str]:
    if language is None:
        return None
    raw = str(language).strip()
    if not raw:
        raise ValueError("language must be a 3-letter ISO 639-3 code")
    code = raw.lower()
    if not re.fullmatch(r"[a-z]{3}", code):
        raise ValueError("language must be a 3-letter ISO 639-3 code")
    if code not in SUPPORTED_SESSION_LANGUAGE_CODES:
        raise ValueError("language must be a supported ISO 639-3 code")
    return code


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


def _copy_session_state(session_state: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "session_id": str(session_state.get("session_id") or ""),
        "last_response_id": session_state.get("last_response_id"),
        "last_discussed_item_ids": [str(x) for x in (session_state.get("last_discussed_item_ids") or []) if str(x)],
        "last_candidate_item_ids": [str(x) for x in (session_state.get("last_candidate_item_ids") or []) if str(x)],
        "last_intent": session_state.get("last_intent"),
        "active_constraints": dict(session_state.get("active_constraints") or {}),
    }


def _extract_response_text(resp: Any) -> str:
    def _read(obj: Any, key: str) -> Any:
        if isinstance(obj, dict):
            return obj.get(key)
        return getattr(obj, key, None)

    out = _read(resp, "output_text")
    if isinstance(out, str) and out.strip():
        return out.strip()

    output = _read(resp, "output")
    if isinstance(output, list):
        parts: List[str] = []
        for item in output:
            content = _read(item, "content")
            if isinstance(content, list):
                for block in content:
                    text = _read(block, "text")
                    if isinstance(text, str) and text.strip():
                        parts.append(text.strip())
                    elif isinstance(text, dict):
                        value = text.get("value")
                        if isinstance(value, str) and value.strip():
                            parts.append(value.strip())
        if parts:
            return "\n".join(parts).strip()
    return ""


def infer_intent(user_query: str) -> Optional[str]:
    lowered = (user_query or "").lower()
    for intent, keywords in INTENT_KEYWORDS.items():
        if any(k in lowered for k in keywords):
            return intent
    return None


def _extract_price_cap(user_query: str) -> Optional[float]:
    lowered = (user_query or "").lower()
    m = re.search(r"(under|below|less than)\s*\$?\s*(\d+(?:\.\d+)?)", lowered)
    if m:
        return float(m.group(2))
    return None


def extract_active_constraints(user_query: str, previous: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(previous or {})
    lowered = (user_query or "").lower()

    price_cap = _extract_price_cap(user_query)
    if price_cap is not None:
        out["max_price"] = price_cap

    for cat in CATEGORY_KEYWORDS:
        if re.search(rf"\b{re.escape(cat)}s?\b", lowered):
            out["category"] = cat
            break

    dietary = dict(out.get("dietary") or {})
    if "vegetarian" in lowered:
        dietary["vegetarian"] = True
    if "vegan" in lowered:
        dietary["vegan"] = True
    if "gluten" in lowered:
        dietary["gluten_free"] = True
    if "dairy free" in lowered or "dairy-free" in lowered:
        dietary["dairy_free"] = True
    if dietary:
        out["dietary"] = dietary

    if "spicy" in lowered:
        out["spicy"] = True

    return out


def _normalize_image_decision(raw: Any) -> Dict[str, Any]:
    decision = dict(DEFAULT_IMAGE_DECISION)
    if not isinstance(raw, dict):
        return decision
    include = bool(raw.get("include_images"))
    max_images = raw.get("max_images", 0)
    try:
        max_images = int(max_images)
    except (TypeError, ValueError):
        max_images = 0
    max_images = max(0, min(MAX_IMAGE_URLS_SENT, max_images))
    names_raw = raw.get("target_item_names") or []
    names: List[str] = []
    if isinstance(names_raw, list):
        for item in names_raw:
            text = str(item or "").strip()
            if text:
                names.append(text)
    decision["include_images"] = include
    decision["max_images"] = max_images
    decision["target_item_names"] = names[:5]
    return decision


def _exact_query_for_cache(user_query: str) -> str:
    return (user_query or "").strip()


def _normalize_query_for_cache(user_query: str) -> str:
    lowered = (user_query or "").strip().lower()
    lowered = re.sub(r"[^\w\s]", " ", lowered)
    return " ".join(lowered.split())


def _is_followup_reference_query(user_query: str) -> bool:
    lowered = _normalize_query_for_cache(user_query)
    if not lowered:
        return False
    pattern = (
        r"\b("
        r"it|that|that one|this one|the one|"
        r"what about (it|that)|"
        r"cheaper one|spicier one|lighter one|same one"
        r")\b"
    )
    return bool(re.search(pattern, lowered))


def _looks_contextual_followup_query(user_query: str) -> bool:
    normalized = _normalize_query_for_cache(user_query)
    if not normalized:
        return False
    if _is_followup_reference_query(user_query):
        return True
    followup_starts = (
        "what about",
        "how about",
        "and",
        "also",
        "anything",
        "something",
        "what else",
        "anything else",
        "other options",
        "another option",
    )
    if any(normalized.startswith(p) for p in followup_starts):
        return True
    return False


def should_reuse_session_context(
    user_query: str,
    resolved_reference: Dict[str, Any],
    inferred_intent: Optional[str],
) -> bool:
    status = str(resolved_reference.get("status") or "")
    if status in {"resolved", "ambiguous"}:
        return True
    if _looks_contextual_followup_query(user_query):
        return True

    normalized = _normalize_query_for_cache(user_query)
    if not normalized:
        return False
    tokens = set(normalized.split())
    if tokens.intersection(CONTEXT_RESET_KEYWORDS):
        return False

    # Very short intent checks are often elliptical follow-ups ("spicy?", "vegetarian?").
    if inferred_intent in {"spice_check", "dietary_check", "lighter_option"} and len(tokens) <= 3:
        return True
    return False


def is_restaurant_relevant_query(user_query: str, inferred_intent: Optional[str]) -> bool:
    if inferred_intent:
        return True
    normalized = _normalize_query_for_cache(user_query)
    if not normalized:
        return False
    if any(phrase in normalized for phrase in RESTAURANT_RELEVANCE_PHRASES):
        return True
    tokens = set(normalized.split())
    if tokens.intersection(RESTAURANT_RELEVANCE_KEYWORDS):
        return True
    if tokens.intersection(CATEGORY_KEYWORDS):
        return True
    return False


def query_cache_eligibility(
    user_query: str,
    session_state: Dict[str, Any],
    resolved_reference: Dict[str, Any],
    inferred_intent: Optional[str] = None,
    require_restaurant_relevance: bool = QUERY_CACHE_REQUIRE_RESTAURANT_RELEVANCE_DEFAULT,
) -> Tuple[bool, str]:
    _ = session_state, inferred_intent, require_restaurant_relevance
    if not (user_query or "").strip():
        return False, "empty_query"
    status = str(resolved_reference.get("status") or "")
    if status in {"resolved", "ambiguous"}:
        return False, f"reference_status_{status}"
    if _is_followup_reference_query(user_query):
        return False, "followup_reference_pattern"
    return True, "eligible"


def _hash_text(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _cache_namespace(namespace: str) -> str:
    ns = (namespace or QUERY_CACHE_NAMESPACE_DEFAULT).strip()
    return ns or QUERY_CACHE_NAMESPACE_DEFAULT


def build_query_cache_context_hash(
    restaurant_id: str,
    system_instructions: str,
    top_k: int,
    min_score: float,
    model: str = CHAT_MODEL,
) -> str:
    payload = {
        "restaurant_id": str(restaurant_id or ""),
        "model": str(model or ""),
        "top_k": int(top_k),
        "min_score": float(min_score),
        "system_instructions_sha256": _hash_text(system_instructions or ""),
    }
    return _hash_text(json.dumps(payload, sort_keys=True, ensure_ascii=False))


def build_query_cache_group_prefix(
    namespace: str,
    restaurant_id: str,
    context_hash: str,
) -> str:
    ns = _cache_namespace(namespace)
    rid = str(restaurant_id or "")
    return f"{ns}:{rid}:{context_hash}"


def build_query_cache_key(
    namespace: str,
    restaurant_id: str,
    user_query: str,
    system_instructions: str,
    top_k: int,
    min_score: float,
    model: str = CHAT_MODEL,
) -> str:
    context_hash = build_query_cache_context_hash(
        restaurant_id=restaurant_id,
        system_instructions=system_instructions,
        top_k=top_k,
        min_score=min_score,
        model=model,
    )
    prefix = build_query_cache_group_prefix(namespace, restaurant_id, context_hash)
    normalized_query = _normalize_query_for_cache(user_query)
    digest = _hash_text(normalized_query)
    return f"{prefix}:norm:{digest}"


def _cacheable_turn_payload(
    turn: Dict[str, Any],
    ttl_seconds: int,
    exact_query: str,
    normalized_query: str,
    query_embedding: Optional[List[float]],
) -> Dict[str, Any]:
    state = _copy_session_state(turn.get("new_session_state") or {})
    state["last_response_id"] = None
    payload = {
        "assistant_text": str(turn.get("assistant_text") or ""),
        "results": list(turn.get("results") or []),
        "image_decision": _normalize_image_decision(turn.get("image_decision")),
        "intent": turn.get("intent"),
        "active_constraints": dict(turn.get("active_constraints") or {}),
        "retrieval_query": str(turn.get("retrieval_query") or ""),
        "resolved_reference": dict(turn.get("resolved_reference") or {}),
        "new_session_state": state,
        "fallback_reason": turn.get("fallback_reason"),
        "created_at": int(time.time()),
        "ttl_seconds": max(1, int(ttl_seconds)),
        "schema_version": QUERY_CACHE_SCHEMA_VERSION,
        "cache_query_exact": exact_query,
        "cache_query_normalized": normalized_query,
        "cache_query_embedding": list(query_embedding or []),
    }
    return payload


def _turn_from_cached_payload(
    cached_payload: Dict[str, Any],
    session_state: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    if not isinstance(cached_payload, dict):
        return None

    results = cached_payload.get("results")
    if not isinstance(results, list):
        return None

    assistant_text = str(cached_payload.get("assistant_text") or "").strip()
    if not assistant_text:
        return None

    new_state = _copy_session_state(cached_payload.get("new_session_state") or session_state or {})
    new_state["last_response_id"] = session_state.get("last_response_id")

    return {
        "assistant_text": assistant_text,
        "results": results,
        "retrieval_query": str(cached_payload.get("retrieval_query") or ""),
        "resolved_reference": dict(cached_payload.get("resolved_reference") or {}),
        "intent": cached_payload.get("intent"),
        "active_constraints": dict(cached_payload.get("active_constraints") or {}),
        "response_id": None,
        "image_decision": _normalize_image_decision(cached_payload.get("image_decision")),
        "new_session_state": new_state,
        "fallback_reason": cached_payload.get("fallback_reason"),
        "cache_hit": True,
    }


def _cosine_similarity(vec_a: List[float], vec_b: List[float]) -> float:
    if not vec_a or not vec_b:
        return -1.0
    a = [float(x) for x in vec_a]
    b = [float(x) for x in vec_b]
    if len(a) != len(b):
        return -1.0
    denom = (sum(x * x for x in a) ** 0.5) * (sum(x * x for x in b) ** 0.5)
    if denom <= 1e-12:
        return -1.0
    return float(sum(x * y for x, y in zip(a, b)) / denom)


def _title_from_page_path(page_path: str) -> str:
    path = str(page_path or "").strip()
    if not path:
        return ""
    segment = path.rstrip("/").split("/")[-1].strip()
    if not segment:
        return ""
    segment = re.sub(r"[-_]+", " ", segment)
    segment = re.sub(r"\s+", " ", segment).strip()
    if not segment or len(segment) > 80:
        return ""
    return segment.title()


def _extract_name_from_text(text: str) -> str:
    if not text:
        return ""
    lines = [ln.strip() for ln in str(text).splitlines() if ln.strip()]
    if not lines:
        return ""

    ignored_labels = {"price", "description", "ingredients", "includes", "comes with", "notes", "hours"}
    for raw in lines[:5]:
        line = re.sub(r"^[\-\*\d\.\)\s]+", "", raw).strip()
        if not line:
            continue

        left = ""
        if " - " in line:
            left = line.split(" - ", 1)[0].strip()
        elif ":" in line:
            left = line.split(":", 1)[0].strip()
        if left:
            lowered = left.lower()
            if lowered not in ignored_labels and 1 <= len(left.split()) <= 8 and len(left) <= 80:
                return left

        match = re.search(
            r"([A-Z][A-Za-z0-9&'/-]*(?:\s+[A-Z][A-Za-z0-9&'/-]*){0,5})(?:\s+\$|\s+-|$)",
            line,
        )
        if match:
            candidate = match.group(1).strip()
            if candidate and len(candidate) <= 80:
                return candidate
    return ""


def _is_generic_image_title(title: str) -> bool:
    cleaned = str(title or "").strip().lower()
    if not cleaned:
        return True
    if cleaned in GENERIC_IMAGE_TITLES:
        return True
    return cleaned.startswith("menu ")


def _normalize_extra_metadata_key(key: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(key or "").strip().lower()).strip("_")


def _coerce_extra_metadata(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        if isinstance(parsed, dict):
            return parsed
    return {}


def _metadata_image_url(meta: Dict[str, Any]) -> str:
    if not isinstance(meta, dict):
        return ""
    for key, value in meta.items():
        if _normalize_extra_metadata_key(key) == "image_url":
            url = str(value or "").strip()
            if url:
                return url
    return ""


def _priority_score_from_extra_metadata(row: Dict[str, Any]) -> float:
    meta = _coerce_extra_metadata(row.get("extra_metadata"))
    if not meta:
        return 0.0
    for key, value in meta.items():
        if _normalize_extra_metadata_key(key) != "priority_score":
            continue
        if value is None:
            return 0.0
        if isinstance(value, str) and not value.strip():
            return 0.0
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0
    return 0.0


def _item_name_from_extra_metadata(row: Dict[str, Any]) -> str:
    meta = _coerce_extra_metadata(row.get("extra_metadata"))
    if not meta:
        return ""
    for key, value in meta.items():
        if _normalize_extra_metadata_key(key) == "item_name":
            item_name = str(value or "").strip()
            if item_name:
                return item_name
    return ""


def _image_url_from_row(row: Dict[str, Any]) -> str:
    direct = str(row.get("image_url") or "").strip()
    if direct:
        return direct

    return _metadata_image_url(_coerce_extra_metadata(row.get("extra_metadata")))


def _derive_image_title(row: Dict[str, Any], target_item_names: List[str]) -> str:
    from_meta = _item_name_from_extra_metadata(row)
    if from_meta:
        return from_meta

    title = str(row.get("title") or "").strip()
    blob = f"{row.get('title','')} {row.get('text','')}".lower()
    for name in target_item_names:
        n = str(name or "").strip()
        if n and n.lower() in blob:
            return n

    if not _is_generic_image_title(title):
        return title

    from_text = _extract_name_from_text(str(row.get("text") or ""))
    if from_text:
        return from_text

    from_path = _title_from_page_path(str(row.get("page_path") or ""))
    if from_path:
        return from_path

    return title or "Menu Item"


def _row_matches_target_item_name(row: Dict[str, Any], target_names: List[str]) -> bool:
    if not target_names:
        return False
    blob = f"{row.get('title','')} {row.get('text','')}".lower()
    return any(name in blob for name in target_names)


def build_image_payload_from_decision(
    results: List[Dict[str, Any]],
    image_decision: Dict[str, Any],
) -> List[Dict[str, Any]]:
    decision = _normalize_image_decision(image_decision)
    if not decision["include_images"]:
        return []

    limit = int(decision["max_images"])
    if limit <= 0:
        return []

    target_names = [str(x).lower() for x in decision.get("target_item_names") or [] if str(x).strip()]
    matched_rows = [row for row in results if _row_matches_target_item_name(row, target_names)]
    if not matched_rows:
        return []

    out: List[Dict[str, Any]] = []
    seen_urls = set()
    for row in matched_rows:
        image_url = _image_url_from_row(row)
        if not image_url:
            continue
        if image_url in seen_urls:
            continue
        seen_urls.add(image_url)
        out.append(
            {
                "chunk_id": str(row.get("id") or ""),
                "title": _derive_image_title(row, decision.get("target_item_names") or []),
                "image_url": image_url,
                "score": float(row.get("score") or 0.0),
            }
        )
        if len(out) >= limit:
            break
    return out


def _price_from_text(text: str) -> Optional[float]:
    matches = re.findall(r"\$\s*(\d+(?:\.\d{1,2})?)", text or "")
    if not matches:
        return None
    vals = [float(x) for x in matches]
    return min(vals) if vals else None


def _label_for_chunk(row: Dict[str, Any]) -> str:
    for key in ("title", "page_path", "id"):
        value = str(row.get(key) or "").strip()
        if value:
            return value
    return "item"


def _load_chunk_map_by_ids(store: Optional[SupabaseStore], item_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    if store is None or not item_ids:
        return {}
    try:
        rows = store.get_knowledge_chunks_by_ids(item_ids)
    except SupabaseStoreError:
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        rid = str(row.get("id") or "").strip()
        if rid:
            out[rid] = row
    return out


def resolve_reference(
    user_query: str,
    session_state: Dict[str, Any],
    store: Optional[SupabaseStore],
) -> Dict[str, Any]:
    lowered = (user_query or "").lower()
    discussed_ids = [str(x) for x in session_state.get("last_discussed_item_ids") or [] if str(x)]
    candidate_ids = [str(x) for x in session_state.get("last_candidate_item_ids") or [] if str(x)]
    all_ids = list(dict.fromkeys(discussed_ids + candidate_ids))
    chunk_map = _load_chunk_map_by_ids(store, all_ids)

    def _labels(ids: List[str]) -> List[str]:
        labels: List[str] = []
        for cid in ids:
            row = chunk_map.get(cid, {"id": cid})
            labels.append(_label_for_chunk(row))
        return labels

    def _ambiguous(ids: List[str], reason: str) -> Dict[str, Any]:
        labels = _labels(ids)[:3]
        joined = ", ".join(labels)
        clarifier = "Could you clarify which item you mean?"
        if joined:
            clarifier = f"Could you clarify which item you mean: {joined}?"
        return {
            "status": "ambiguous",
            "confidence": 0.2,
            "reason": reason,
            "target_item_ids": ids,
            "target_item_names": labels,
            "clarifying_question": clarifier,
        }

    has_pronoun = bool(re.search(r"\b(it|that|that one|this one|the one)\b", lowered))
    wants_cheaper = "cheaper" in lowered or "cheapest" in lowered
    wants_spicier = "spicier" in lowered or "spicy one" in lowered
    wants_lighter = "lighter" in lowered or "light one" in lowered or "healthier" in lowered

    if wants_cheaper and len(candidate_ids) >= 2:
        scored: List[Tuple[float, str]] = []
        for cid in candidate_ids:
            row = chunk_map.get(cid, {})
            price = _price_from_text(str(row.get("text") or "") + "\n" + str(row.get("title") or ""))
            if price is not None:
                scored.append((price, cid))
        if len(scored) == 1:
            _, cid = scored[0]
            return {
                "status": "resolved",
                "confidence": 0.8,
                "reason": "cheaper_from_candidates",
                "target_item_ids": [cid],
                "target_item_names": _labels([cid]),
            }
        if len(scored) >= 2:
            scored.sort(key=lambda x: x[0])
            cid = scored[0][1]
            return {
                "status": "resolved",
                "confidence": 0.95,
                "reason": "cheaper_from_candidates",
                "target_item_ids": [cid],
                "target_item_names": _labels([cid]),
            }
        return _ambiguous(candidate_ids, "cheaper_without_prices")

    if wants_spicier and candidate_ids:
        spicy_hits: List[str] = []
        for cid in candidate_ids:
            text = str(chunk_map.get(cid, {}).get("text") or "").lower()
            if "spicy" in text or "hot" in text:
                spicy_hits.append(cid)
        if len(spicy_hits) == 1:
            cid = spicy_hits[0]
            return {
                "status": "resolved",
                "confidence": 0.9,
                "reason": "spicy_from_candidates",
                "target_item_ids": [cid],
                "target_item_names": _labels([cid]),
            }
        if len(spicy_hits) > 1:
            return _ambiguous(spicy_hits, "multiple_spicy_candidates")

    if wants_lighter and candidate_ids:
        light_hits: List[str] = []
        for cid in candidate_ids:
            text = str(chunk_map.get(cid, {}).get("text") or "").lower()
            if any(t in text for t in ("light", "lighter", "low calorie", "low-calorie", "lean")):
                light_hits.append(cid)
        if len(light_hits) == 1:
            cid = light_hits[0]
            return {
                "status": "resolved",
                "confidence": 0.85,
                "reason": "lighter_from_candidates",
                "target_item_ids": [cid],
                "target_item_names": _labels([cid]),
            }
        if len(light_hits) > 1:
            return _ambiguous(light_hits, "multiple_lighter_candidates")

    if has_pronoun and len(discussed_ids) == 1:
        cid = discussed_ids[0]
        return {
            "status": "resolved",
            "confidence": 0.95,
            "reason": "single_last_discussed",
            "target_item_ids": [cid],
            "target_item_names": _labels([cid]),
        }

    if has_pronoun and len(candidate_ids) == 1:
        cid = candidate_ids[0]
        return {
            "status": "resolved",
            "confidence": 0.85,
            "reason": "single_candidate",
            "target_item_ids": [cid],
            "target_item_names": _labels([cid]),
        }

    if has_pronoun and len(candidate_ids) > 1:
        return _ambiguous(candidate_ids, "pronoun_multi_candidates")

    if has_pronoun and len(discussed_ids) > 1:
        return _ambiguous(discussed_ids, "pronoun_multi_discussed")

    return {
        "status": "none",
        "confidence": 0.0,
        "reason": "no_vague_reference",
        "target_item_ids": [],
        "target_item_names": [],
    }


def build_retrieval_query(
    raw_query: str,
    resolved_reference: Dict[str, Any],
    intent: Optional[str],
    active_constraints: Dict[str, Any],
) -> str:
    parts: List[str] = [raw_query.strip()]

    names = resolved_reference.get("target_item_names") or []
    if resolved_reference.get("status") == "resolved" and names:
        parts.append(f"target item: {', '.join(names)}")

    if intent == "ingredients":
        parts.append("included sides ingredients")
    elif intent == "compare_price":
        parts.append("price cost cheapest cheaper")
    elif intent == "spice_check":
        parts.append("spice spicy heat level")
    elif intent == "lighter_option":
        parts.append("lighter low-calorie lower calorie")
    elif intent == "dietary_check":
        parts.append("dietary allergens vegetarian vegan gluten-free")

    category = active_constraints.get("category")
    if category:
        parts.append(f"category {category}")
    max_price = active_constraints.get("max_price")
    if max_price is not None:
        parts.append(f"price under ${max_price}")
    if active_constraints.get("spicy") is True:
        parts.append("spicy options")
    dietary = active_constraints.get("dietary") or {}
    for k, v in dietary.items():
        if v:
            parts.append(str(k).replace("_", " "))

    return " | ".join(p for p in parts if p)


def retrieve_menu_items(
    retriever: "Retriever",
    client: OpenAI,
    restaurant_id: str,
    retrieval_query: str,
    top_k: int,
    min_score: float,
    session_state: Dict[str, Any],
    active_constraints: Dict[str, Any],
    query_embedding: Optional[List[float]] = None,
) -> Dict[str, Any]:
    prep = retriever.retrieve(
        client=client,
        user_q=retrieval_query,
        top_k=max(top_k * 2, top_k),
        min_score=min_score,
        restaurant_id=restaurant_id,
        query_embedding=query_embedding,
    )
    base_results = prep["results"]

    discussed = set(str(x) for x in session_state.get("last_discussed_item_ids") or [])
    candidates = set(str(x) for x in session_state.get("last_candidate_item_ids") or [])
    category = str(active_constraints.get("category") or "").lower()
    max_price = active_constraints.get("max_price")

    reranked: List[Dict[str, Any]] = []
    for row in base_results:
        score = float(row.get("score") or 0.0)
        reasons: List[str] = []
        matched_constraints: List[str] = []
        rid = str(row.get("id") or "")
        blob = f"{row.get('title','')} {row.get('text','')}".lower()
        priority_score = _priority_score_from_extra_metadata(row)
        priority_boost = 0.05 * priority_score

        if priority_boost != 0:
            score += priority_boost
            reasons.append("priority_score_boost")

        if rid in discussed:
            score += 0.12
            reasons.append("recent_discussed_boost")
        if rid in candidates:
            score += 0.08
            reasons.append("candidate_boost")

        if category:
            if re.search(rf"\b{re.escape(category)}s?\b", blob):
                score += 0.06
                matched_constraints.append("category")
            else:
                score -= 0.02

        if max_price is not None:
            price = _price_from_text(blob)
            if price is not None and price <= float(max_price):
                score += 0.05
                matched_constraints.append("max_price")
            elif price is not None and price > float(max_price):
                score -= 0.03

        out = row.copy()
        out["score"] = score
        out["priority_score"] = priority_score
        out["priority_boost"] = priority_boost
        out["boost_reason"] = reasons
        out["matched_constraints"] = matched_constraints
        reranked.append(out)

    reranked.sort(key=lambda r: float(r.get("score") or 0.0), reverse=True)
    final_results = reranked[:top_k]

    return {
        "results": final_results,
        "timing_start": prep["timing_start"],
        "menu_context": build_context(final_results[:MAX_SOURCES_SENT]),
    }


def format_menu_context(results: List[Dict[str, Any]]) -> str:
    return build_context(results[:MAX_SOURCES_SENT])


def build_session_context(
    session_state: Dict[str, Any],
    resolved_reference: Dict[str, Any],
    intent: Optional[str],
    active_constraints: Dict[str, Any],
) -> str:
    context_payload = {
        "last_discussed_item_ids": session_state.get("last_discussed_item_ids") or [],
        "last_candidate_item_ids": session_state.get("last_candidate_item_ids") or [],
        "last_intent": session_state.get("last_intent"),
        "active_constraints": active_constraints or {},
        "resolved_reference": resolved_reference,
        "current_intent": intent,
    }
    return json.dumps(context_payload, ensure_ascii=False)


def _extract_first_json_object(raw: str) -> Optional[Dict[str, Any]]:
    text = (raw or "").strip()
    if not text:
        return None

    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z0-9]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()

    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass

    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    snippet = text[start : end + 1]
    try:
        obj = json.loads(snippet)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        return None
    return None


def _strip_one_leading_linebreak(text: str) -> str:
    if text.startswith("\r\n"):
        return text[2:]
    if text.startswith("\n"):
        return text[1:]
    return text


def _strip_one_trailing_linebreak(text: str) -> str:
    if text.endswith("\r\n"):
        return text[:-2]
    if text.endswith("\n"):
        return text[:-1]
    return text


def _event_attr(event: Any, name: str, default: Any = None) -> Any:
    if isinstance(event, dict):
        return event.get(name, default)
    return getattr(event, name, default)


def _response_id_from_obj(value: Any) -> Optional[str]:
    response_id = _event_attr(value, "id")
    if response_id:
        return str(response_id)
    return None


def _extract_stream_delta(event: Any) -> str:
    event_type = str(_event_attr(event, "type", "") or "")
    if event_type not in {"response.output_text.delta", "response.refusal.delta", "output_text.delta"}:
        return ""
    return str(_event_attr(event, "delta", "") or "")


def _extract_stream_response(event: Any) -> Optional[Any]:
    response = _event_attr(event, "response")
    if response is not None:
        return response
    if str(_event_attr(event, "type", "") or "") == "response.completed":
        return event
    return None


def _extract_stream_error(event: Any) -> Optional[str]:
    if str(_event_attr(event, "type", "") or "") != "response.error":
        return None
    error = _event_attr(event, "error")
    if error is None:
        return "OpenAI streaming response failed"
    message = _event_attr(error, "message")
    return str(message or error)


def _extract_tagged_assistant_response_and_images(raw_text: str) -> Optional[Tuple[str, Dict[str, Any]]]:
    raw = raw_text or ""
    start_idx = raw.find(ASSISTANT_TEXT_START_MARKER)
    if start_idx == -1:
        return None

    content_start_idx = start_idx + len(ASSISTANT_TEXT_START_MARKER)
    image_start_idx = raw.find(IMAGE_DECISION_JSON_START_MARKER, content_start_idx)
    end_idx = raw.find(ASSISTANT_TEXT_END_MARKER, content_start_idx)
    if end_idx == -1 and image_start_idx != -1:
        end_idx = image_start_idx
    if end_idx == -1 or end_idx <= start_idx:
        return None

    assistant_text = raw[start_idx + len(ASSISTANT_TEXT_START_MARKER) : end_idx]
    assistant_text = _strip_one_trailing_linebreak(_strip_one_leading_linebreak(assistant_text)).strip()

    image_decision = dict(DEFAULT_IMAGE_DECISION)
    if image_start_idx == -1:
        image_start_idx = raw.find(IMAGE_DECISION_JSON_START_MARKER, end_idx + len(ASSISTANT_TEXT_END_MARKER))
    image_end_idx = raw.find(IMAGE_DECISION_JSON_END_MARKER, image_start_idx + len(IMAGE_DECISION_JSON_START_MARKER))
    if image_start_idx != -1 and image_end_idx != -1 and image_end_idx > image_start_idx:
        image_blob = raw[image_start_idx + len(IMAGE_DECISION_JSON_START_MARKER) : image_end_idx].strip()
        try:
            parsed = json.loads(image_blob)
        except json.JSONDecodeError:
            parsed = None
        image_decision = _normalize_image_decision(parsed)

    return assistant_text, image_decision


class AssistantTaggedStreamParser:
    def __init__(self) -> None:
        self._state = "before_text"
        self._buffer = ""
        self._raw_parts: List[str] = []
        self._assistant_parts: List[str] = []
        self._trim_leading_linebreak = False

    def _record(self, text: str) -> str:
        if text:
            self._assistant_parts.append(text)
        return text

    def _maybe_trim_leading_linebreak(self) -> bool:
        if not self._trim_leading_linebreak:
            return False
        if self._buffer == "\r":
            return True
        self._buffer = _strip_one_leading_linebreak(self._buffer)
        self._trim_leading_linebreak = False
        return False

    def feed(self, delta: str) -> List[str]:
        if not delta:
            return []
        self._raw_parts.append(delta)
        self._buffer += delta

        emitted: List[str] = []
        while True:
            if self._state == "before_text":
                marker_idx = self._buffer.find(ASSISTANT_TEXT_START_MARKER)
                if marker_idx == -1:
                    tail_len = len(ASSISTANT_TEXT_START_MARKER) - 1
                    if len(self._buffer) > tail_len:
                        self._buffer = self._buffer[-tail_len:]
                    break
                self._buffer = self._buffer[marker_idx + len(ASSISTANT_TEXT_START_MARKER) :]
                self._state = "in_text"
                self._trim_leading_linebreak = True
                continue

            if self._state == "in_text":
                if self._maybe_trim_leading_linebreak():
                    break
                text_end_idx = self._buffer.find(ASSISTANT_TEXT_END_MARKER)
                image_start_idx = self._buffer.find(IMAGE_DECISION_JSON_START_MARKER)
                marker_options = [idx for idx in (text_end_idx, image_start_idx) if idx != -1]
                if marker_options:
                    marker_idx = min(marker_options)
                    text = _strip_one_trailing_linebreak(self._buffer[:marker_idx])
                    if text:
                        emitted.append(self._record(text))
                    if marker_idx == text_end_idx:
                        self._buffer = self._buffer[marker_idx + len(ASSISTANT_TEXT_END_MARKER) :]
                    else:
                        self._buffer = self._buffer[marker_idx:]
                    self._state = "after_text"
                    break

                tail_len = max(len(ASSISTANT_TEXT_END_MARKER), len(IMAGE_DECISION_JSON_START_MARKER)) - 1
                if len(self._buffer) > tail_len:
                    text = self._buffer[:-tail_len]
                    self._buffer = self._buffer[-tail_len:]
                    if text:
                        emitted.append(self._record(text))
                break

            break

        return emitted

    def finish(self) -> Tuple[List[str], str, Dict[str, Any]]:
        emitted: List[str] = []
        if self._state == "in_text":
            self._maybe_trim_leading_linebreak()
            if self._buffer:
                emitted.append(self._record(self._buffer))
            self._buffer = ""

        raw_text = "".join(self._raw_parts)
        tagged = _extract_tagged_assistant_response_and_images(raw_text)
        if tagged is not None:
            assistant_text, image_decision = tagged
            return emitted, assistant_text, image_decision

        assistant_text = "".join(self._assistant_parts).strip()
        if assistant_text:
            return emitted, assistant_text, dict(DEFAULT_IMAGE_DECISION)

        assistant_text, image_decision = _extract_assistant_response_and_images(raw_text)
        if assistant_text:
            emitted.append(assistant_text)
        return emitted, assistant_text, image_decision


def _extract_assistant_response_and_images(raw_text: str) -> Tuple[str, Dict[str, Any]]:
    default_text = (raw_text or "").strip()
    if not default_text:
        default_text = "I’m sorry, I couldn’t generate a response right now."

    tagged = _extract_tagged_assistant_response_and_images(raw_text)
    if tagged is not None:
        return tagged

    parsed = _extract_first_json_object(raw_text)
    if not parsed:
        return default_text, dict(DEFAULT_IMAGE_DECISION)

    assistant_text = str(parsed.get("assistant_text") or "").strip()
    if not assistant_text:
        assistant_text = default_text
    image_decision = _normalize_image_decision(parsed.get("image_decision"))
    return assistant_text, image_decision


def create_assistant_response(
    client: OpenAI,
    system_instructions: str,
    user_query: str,
    menu_context: str,
    session_context: str,
    previous_response_id: Optional[str],
    assistant_delta_callback: Optional[Callable[[str], None]] = None,
) -> Tuple[str, Optional[str], Dict[str, Any]]:
    user_payload = (
        "Session context:\n"
        f"{session_context}\n\n"
        "Retrieved menu context (facts):\n"
        f"{menu_context or '[no menu context found]'}\n\n"
        "Current user message:\n"
        f"{user_query}\n\n"
        "Return ONLY this exact tagged envelope, with no markdown and no extra text:\n"
        f"{ASSISTANT_TEXT_START_MARKER}\n"
        "user-facing assistant text\n"
        f"{ASSISTANT_TEXT_END_MARKER}\n"
        f"{IMAGE_DECISION_JSON_START_MARKER}\n"
        '{"include_images":false,"max_images":0,"target_item_names":[]}\n'
        f"{IMAGE_DECISION_JSON_END_MARKER}\n"
        "Rules:\n"
        "- Keep assistant_text concise and natural.\n"
        "- Put only the customer-facing answer inside the assistant text markers.\n"
        "- Put only valid JSON inside the image decision JSON markers.\n"
        "- include_images=true only when user intent suggests recommendations or seeing photos.\n"
        "- For one specific dish, prefer max_images=1 and set target_item_names.\n"
        "- For broad recommendations, max_images can be up to 3.\n"
        "- If no relevant photos should be shown, set include_images=false and max_images=0."
    )
    kwargs: Dict[str, Any] = {
        "model": CHAT_MODEL,
        "instructions": system_instructions,
        "input": [{"role": "user", "content": [{"type": "input_text", "text": user_payload}]}],
    }
    if previous_response_id:
        kwargs["previous_response_id"] = previous_response_id

    parser = AssistantTaggedStreamParser()
    response_id: Optional[str] = None

    def _handle_event(event: Any) -> None:
        nonlocal response_id
        error = _extract_stream_error(event)
        if error:
            raise RuntimeError(error)

        response = _extract_stream_response(event)
        if response is not None:
            response_id = _response_id_from_obj(response) or response_id

        delta = _extract_stream_delta(event)
        for text in parser.feed(delta):
            if assistant_delta_callback is not None:
                assistant_delta_callback(text)

    stream_factory = getattr(client.responses, "stream", None)
    if callable(stream_factory):
        with stream_factory(**kwargs) as stream:
            for event in stream:
                _handle_event(event)
            final_response_getter = getattr(stream, "get_final_response", None)
            if callable(final_response_getter):
                response_id = _response_id_from_obj(final_response_getter()) or response_id
    else:
        stream_kwargs = dict(kwargs)
        stream_kwargs["stream"] = True
        try:
            stream = client.responses.create(**stream_kwargs)
        except TypeError:
            resp = client.responses.create(**kwargs)
            raw_text = _extract_response_text(resp).strip()
            assistant_text, image_decision = _extract_assistant_response_and_images(raw_text)
            if assistant_delta_callback is not None and assistant_text:
                assistant_delta_callback(assistant_text)
            return assistant_text, getattr(resp, "id", None), image_decision
        for event in stream:
            _handle_event(event)

    late_chunks, assistant_text, image_decision = parser.finish()
    for text in late_chunks:
        if assistant_delta_callback is not None:
            assistant_delta_callback(text)
    return assistant_text, response_id, image_decision


def infer_new_session_state(
    session_state: Dict[str, Any],
    resolved_reference: Dict[str, Any],
    retrieved_results: List[Dict[str, Any]],
    intent: Optional[str],
    active_constraints: Dict[str, Any],
) -> Dict[str, Any]:
    out = _copy_session_state(session_state)
    discussed_ids: List[str] = []
    candidate_ids: List[str] = []

    if resolved_reference.get("status") == "resolved":
        discussed_ids = [str(x) for x in resolved_reference.get("target_item_ids") or [] if str(x)]

    if not discussed_ids and retrieved_results:
        discussed_ids = [str(retrieved_results[0].get("id") or "")]
        discussed_ids = [x for x in discussed_ids if x]

    if intent == "compare_price":
        for row in retrieved_results[:2]:
            rid = str(row.get("id") or "")
            if rid:
                candidate_ids.append(rid)
    elif discussed_ids:
        candidate_ids = discussed_ids[:]

    out["last_discussed_item_ids"] = discussed_ids
    out["last_candidate_item_ids"] = candidate_ids
    out["last_intent"] = intent
    out["active_constraints"] = dict(active_constraints or {})
    return out


def handle_chat_turn(
    client: OpenAI,
    retriever: "Retriever",
    store: Optional[SupabaseStore],
    restaurant_id: str,
    user_query: str,
    system_instructions: str,
    session_state: Dict[str, Any],
    top_k: int,
    min_score: float,
    query_cache: Optional[RedisQueryCache] = None,
    query_cache_namespace: str = QUERY_CACHE_NAMESPACE_DEFAULT,
    query_cache_ttl_seconds: int = QUERY_CACHE_TTL_SECONDS_DEFAULT,
    query_cache_semantic_threshold: float = QUERY_CACHE_SEMANTIC_THRESHOLD_DEFAULT,
    query_cache_semantic_max_candidates: int = QUERY_CACHE_SEMANTIC_MAX_CANDIDATES_DEFAULT,
    query_cache_require_restaurant_relevance: bool = QUERY_CACHE_REQUIRE_RESTAURANT_RELEVANCE_DEFAULT,
    query_cache_classifier_model: str = QUERY_CACHE_CLASSIFIER_MODEL_DEFAULT,
    query_cache_classifier_timeout_ms: int = QUERY_CACHE_CLASSIFIER_TIMEOUT_MS_DEFAULT,
    request_id: Optional[str] = None,
    assistant_delta_callback: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    raw_query = (user_query or "").strip()
    exact_query = _exact_query_for_cache(raw_query)
    normalized_query = _normalize_query_for_cache(raw_query)
    cache_classifier_model = (query_cache_classifier_model or QUERY_CACHE_CLASSIFIER_MODEL_DEFAULT).strip()
    if not cache_classifier_model:
        cache_classifier_model = QUERY_CACHE_CLASSIFIER_MODEL_DEFAULT
    cache_classifier_timeout_ms = max(0, int(query_cache_classifier_timeout_ms))
    cache_classifier_event: Optional[threading.Event] = None
    cache_classifier_state: Dict[str, Any] = {
        "status": "disabled",
        "cacheable": None,
        "raw_output": "",
        "meta": {},
        "error": None,
    }
    if query_cache is not None and raw_query:
        cache_classifier_event = threading.Event()
        cache_classifier_state["status"] = "pending"

        def _cache_classifier_worker() -> None:
            t0 = time.perf_counter()
            log_event(
                "query_cache_classification_start",
                request_id=request_id,
                restaurant_id=restaurant_id,
                model=cache_classifier_model,
            )
            try:
                cacheable, raw_output, meta = classify_query_cacheability(client, cache_classifier_model, raw_query)
                status = "ok" if cacheable is not None else "invalid_label"
                cache_classifier_state.update(
                    {
                        "status": status,
                        "cacheable": cacheable,
                        "raw_output": raw_output,
                        "meta": meta,
                    }
                )
                if status == "ok":
                    log_event(
                        "query_cache_classification_complete",
                        request_id=request_id,
                        restaurant_id=restaurant_id,
                        model=cache_classifier_model,
                        cacheable=bool(cacheable),
                        response_id=meta.get("response_id"),
                        raw_output_len=meta.get("raw_output_len"),
                        llm_latency_ms=meta.get("latency_ms"),
                        latency_ms=int((time.perf_counter() - t0) * 1000),
                    )
                else:
                    log_event(
                        "query_cache_classification_failed",
                        request_id=request_id,
                        restaurant_id=restaurant_id,
                        model=cache_classifier_model,
                        reason="invalid_label",
                        raw_output=(raw_output or "")[:200],
                        response_id=meta.get("response_id"),
                        llm_latency_ms=meta.get("latency_ms"),
                        latency_ms=int((time.perf_counter() - t0) * 1000),
                    )
            except Exception as exc:  # noqa: BLE001
                cache_classifier_state.update({"status": "error", "error": str(exc)})
                log_event(
                    "query_cache_classification_failed",
                    request_id=request_id,
                    restaurant_id=restaurant_id,
                    model=cache_classifier_model,
                    error=str(exc),
                    latency_ms=int((time.perf_counter() - t0) * 1000),
                )
            finally:
                cache_classifier_event.set()

        threading.Thread(target=_cache_classifier_worker, daemon=True).start()
    inferred_intent = infer_intent(raw_query)
    resolved_reference = resolve_reference(raw_query, session_state, store)
    reuse_session_context = should_reuse_session_context(raw_query, resolved_reference, inferred_intent)
    intent = inferred_intent or (session_state.get("last_intent") if reuse_session_context else None)
    base_constraints = session_state.get("active_constraints") or {}
    if not reuse_session_context:
        base_constraints = {}
    active_constraints = extract_active_constraints(raw_query, base_constraints)

    cache_eligible, cache_reason = query_cache_eligibility(
        raw_query,
        session_state,
        resolved_reference,
        inferred_intent=inferred_intent,
        require_restaurant_relevance=query_cache_require_restaurant_relevance,
    )
    context_hash = build_query_cache_context_hash(
        restaurant_id=restaurant_id,
        system_instructions=system_instructions,
        top_k=top_k,
        min_score=min_score,
        model=CHAT_MODEL,
    )
    cache_key_prefix = build_query_cache_group_prefix(query_cache_namespace, restaurant_id, context_hash)
    semantic_query_embedding: Optional[List[float]] = None
    if query_cache is not None:
        log_event(
            "query_cache_lookup",
            request_id=request_id,
            restaurant_id=restaurant_id,
            eligible=cache_eligible,
            reason=cache_reason,
            key_prefix=cache_key_prefix,
        )
    if query_cache is not None and cache_eligible:
        exact_hash = _hash_text(exact_query)
        normalized_hash = _hash_text(normalized_query)

        try:
            cached_payload = query_cache.lookup_exact(cache_key_prefix, exact_hash)
        except Exception as exc:  # noqa: BLE001
            log_event(
                "query_cache_error",
                request_id=request_id,
                restaurant_id=restaurant_id,
                action="get",
                error=str(exc),
                key_prefix=cache_key_prefix,
            )
            cached_payload = None
        if cached_payload is not None:
            log_event(
                "query_cache_exact_hit",
                request_id=request_id,
                restaurant_id=restaurant_id,
                key_prefix=cache_key_prefix,
            )
            cached_turn = _turn_from_cached_payload(cached_payload, session_state)
            if cached_turn is not None:
                cached_turn["cache_hit_stage"] = "exact"
                log_event(
                    "query_cache_hit",
                    request_id=request_id,
                    restaurant_id=restaurant_id,
                    key_prefix=cache_key_prefix,
                    stage="exact",
                )
                return cached_turn
        log_event(
            "query_cache_exact_miss",
            request_id=request_id,
            restaurant_id=restaurant_id,
            key_prefix=cache_key_prefix,
        )

        try:
            cached_payload = query_cache.lookup_normalized(cache_key_prefix, normalized_hash)
        except Exception as exc:  # noqa: BLE001
            log_event(
                "query_cache_error",
                request_id=request_id,
                restaurant_id=restaurant_id,
                action="lookup_normalized",
                error=str(exc),
                key_prefix=cache_key_prefix,
            )
            cached_payload = None
        if cached_payload is not None:
            log_event(
                "query_cache_normalized_hit",
                request_id=request_id,
                restaurant_id=restaurant_id,
                key_prefix=cache_key_prefix,
            )
            cached_turn = _turn_from_cached_payload(cached_payload, session_state)
            if cached_turn is not None:
                cached_turn["cache_hit_stage"] = "normalized"
                log_event(
                    "query_cache_hit",
                    request_id=request_id,
                    restaurant_id=restaurant_id,
                    key_prefix=cache_key_prefix,
                    stage="normalized",
                )
                return cached_turn
        log_event(
            "query_cache_normalized_miss",
            request_id=request_id,
            restaurant_id=restaurant_id,
            key_prefix=cache_key_prefix,
        )

        try:
            semantic_query_embedding = embed_query(client, exact_query)[0].tolist()
        except Exception as exc:  # noqa: BLE001
            semantic_query_embedding = None
            log_event(
                "query_cache_error",
                request_id=request_id,
                restaurant_id=restaurant_id,
                action="semantic_embed",
                error=str(exc),
                key_prefix=cache_key_prefix,
            )

        best_payload: Optional[Dict[str, Any]] = None
        best_score = -1.0
        if semantic_query_embedding:
            try:
                candidates = query_cache.semantic_candidates(
                    cache_key_prefix,
                    max_candidates=max(1, int(query_cache_semantic_max_candidates)),
                )
                for candidate in candidates:
                    emb = candidate.get("cache_query_embedding")
                    if not isinstance(emb, list):
                        continue
                    score = _cosine_similarity(semantic_query_embedding, emb)
                    if score > best_score:
                        best_score = score
                        best_payload = candidate
            except Exception as exc:  # noqa: BLE001
                log_event(
                    "query_cache_error",
                    request_id=request_id,
                    restaurant_id=restaurant_id,
                    action="lookup_semantic",
                    error=str(exc),
                    key_prefix=cache_key_prefix,
                )

        semantic_threshold = float(query_cache_semantic_threshold)
        if best_payload is not None and best_score >= semantic_threshold:
            log_event(
                "query_cache_semantic_hit",
                request_id=request_id,
                restaurant_id=restaurant_id,
                key_prefix=cache_key_prefix,
                score=best_score,
                threshold=semantic_threshold,
            )
            cached_turn = _turn_from_cached_payload(best_payload, session_state)
            if cached_turn is not None:
                cached_turn["cache_hit_stage"] = "semantic"
                log_event(
                    "query_cache_hit",
                    request_id=request_id,
                    restaurant_id=restaurant_id,
                    key_prefix=cache_key_prefix,
                    stage="semantic",
                    score=best_score,
                    threshold=semantic_threshold,
                )
                return cached_turn

        log_event(
            "query_cache_semantic_miss",
            request_id=request_id,
            restaurant_id=restaurant_id,
            key_prefix=cache_key_prefix,
            score=best_score if best_score >= 0 else None,
            threshold=semantic_threshold,
        )
        log_event(
            "query_cache_miss",
            request_id=request_id,
            restaurant_id=restaurant_id,
            key_prefix=cache_key_prefix,
            stage="all",
        )

    retrieval_query = build_retrieval_query(raw_query, resolved_reference, intent, active_constraints)

    def _maybe_store_cache(turn_payload: Dict[str, Any]) -> None:
        nonlocal semantic_query_embedding
        if query_cache is None or not cache_eligible:
            return
        if cache_classifier_event is None:
            log_event(
                "query_cache_store_skip",
                request_id=request_id,
                restaurant_id=restaurant_id,
                key_prefix=cache_key_prefix,
                reason="classifier_not_started",
            )
            return
        if not cache_classifier_event.wait(cache_classifier_timeout_ms / 1000.0):
            log_event(
                "query_cache_store_skip",
                request_id=request_id,
                restaurant_id=restaurant_id,
                key_prefix=cache_key_prefix,
                reason="classifier_timeout",
                model=cache_classifier_model,
                timeout_ms=cache_classifier_timeout_ms,
            )
            return
        classifier_status = str(cache_classifier_state.get("status") or "")
        classifier_cacheable = cache_classifier_state.get("cacheable")
        if classifier_status != "ok" or classifier_cacheable is not True:
            reason = "classifier_not_cacheable"
            if classifier_status == "invalid_label":
                reason = "classifier_invalid_label"
            elif classifier_status == "error":
                reason = "classifier_error"
            elif classifier_status != "ok":
                reason = f"classifier_{classifier_status or 'unknown'}"
            log_event(
                "query_cache_store_skip",
                request_id=request_id,
                restaurant_id=restaurant_id,
                key_prefix=cache_key_prefix,
                reason=reason,
                model=cache_classifier_model,
                raw_output=(str(cache_classifier_state.get("raw_output") or "")[:200]),
                error=cache_classifier_state.get("error"),
            )
            return
        if semantic_query_embedding is None:
            try:
                semantic_query_embedding = embed_query(client, exact_query)[0].tolist()
            except Exception as exc:  # noqa: BLE001
                semantic_query_embedding = []
                log_event(
                    "query_cache_error",
                    request_id=request_id,
                    restaurant_id=restaurant_id,
                    action="store_embed",
                    error=str(exc),
                    key_prefix=cache_key_prefix,
                )
        payload = _cacheable_turn_payload(
            turn_payload,
            query_cache_ttl_seconds,
            exact_query=exact_query,
            normalized_query=normalized_query,
            query_embedding=semantic_query_embedding,
        )
        try:
            query_cache.store_entry(
                cache_key_prefix,
                exact_query_hash=_hash_text(exact_query),
                normalized_query_hash=_hash_text(normalized_query),
                payload=payload,
                ttl_seconds=query_cache_ttl_seconds,
            )
            log_event(
                "query_cache_store",
                request_id=request_id,
                restaurant_id=restaurant_id,
                key_prefix=cache_key_prefix,
                ttl_seconds=max(1, int(query_cache_ttl_seconds)),
            )
        except Exception as exc:  # noqa: BLE001
            log_event(
                "query_cache_error",
                request_id=request_id,
                restaurant_id=restaurant_id,
                action="set",
                error=str(exc),
                key_prefix=cache_key_prefix,
            )

    if resolved_reference.get("status") == "ambiguous":
        assistant_text = resolved_reference.get("clarifying_question") or "Could you clarify which item you mean?"
        new_state = infer_new_session_state(
            session_state,
            resolved_reference,
            [],
            intent,
            active_constraints,
        )
        return {
            "assistant_text": assistant_text,
            "results": [],
            "retrieval_query": retrieval_query,
            "resolved_reference": resolved_reference,
            "intent": intent,
            "active_constraints": active_constraints,
            "response_id": None,
            "image_decision": dict(DEFAULT_IMAGE_DECISION),
            "new_session_state": new_state,
            "fallback_reason": "ambiguity",
            "cache_hit": False,
        }

    retrieved = retrieve_menu_items(
        retriever=retriever,
        client=client,
        restaurant_id=restaurant_id,
        retrieval_query=retrieval_query,
        top_k=top_k,
        min_score=min_score,
        session_state=session_state,
        active_constraints=active_constraints,
        query_embedding=semantic_query_embedding,
    )
    results = retrieved["results"]

    if not results:
        assistant_text = (
            "I couldn’t find that in the menu data right now. "
            "If you share the exact item name, I can check again."
        )
        new_state = infer_new_session_state(
            session_state,
            resolved_reference,
            [],
            intent,
            active_constraints,
        )
        turn_payload = {
            "assistant_text": assistant_text,
            "results": [],
            "retrieval_query": retrieval_query,
            "resolved_reference": resolved_reference,
            "intent": intent,
            "active_constraints": active_constraints,
            "response_id": None,
            "image_decision": dict(DEFAULT_IMAGE_DECISION),
            "new_session_state": new_state,
            "fallback_reason": "no_retrieval_results",
            "cache_hit": False,
        }
        _maybe_store_cache(turn_payload)
        return turn_payload

    session_context = build_session_context(
        session_state=session_state,
        resolved_reference=resolved_reference,
        intent=intent,
        active_constraints=active_constraints,
    )
    menu_context = format_menu_context(results)
    assistant_text, response_id, image_decision = create_assistant_response(
        client=client,
        system_instructions=system_instructions,
        user_query=raw_query,
        menu_context=menu_context,
        session_context=session_context,
        previous_response_id=session_state.get("last_response_id"),
        assistant_delta_callback=assistant_delta_callback,
    )
    new_state = infer_new_session_state(
        session_state,
        resolved_reference,
        results,
        intent,
        active_constraints,
    )
    new_state["last_response_id"] = response_id
    turn_payload = {
        "assistant_text": assistant_text,
        "results": results,
        "retrieval_query": retrieval_query,
        "resolved_reference": resolved_reference,
        "intent": intent,
        "active_constraints": active_constraints,
        "response_id": response_id,
        "image_decision": image_decision,
        "new_session_state": new_state,
        "fallback_reason": None,
        "cache_hit": False,
    }
    _maybe_store_cache(turn_payload)
    return turn_payload


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


def normalize_query_cacheability_label(raw: str) -> Optional[bool]:
    value = (raw or "").strip().strip("\"'`")
    if not value:
        return None

    def _clean(s: str) -> str:
        return re.sub(r"[^a-z]+", "", s.lower())

    first_line = value.splitlines()[0].strip()
    first_line_clean = _clean(first_line)
    if first_line_clean == "cacheable":
        return True
    if first_line_clean == "notcacheable":
        return False

    lowered = value.lower()
    if re.search(r"\bnot[\s_-]*cacheable\b", lowered):
        return False
    if re.search(r"\bcacheable\b", lowered):
        return True
    return None


def classify_query_cacheability(client: OpenAI, model: str, user_query: str) -> Tuple[Optional[bool], str, Dict[str, Any]]:
    t0 = time.perf_counter()

    resp = client.responses.create(
        model=model,
        input=[
            {"role": "system", "content": QUERY_CACHE_CLASSIFIER_INSTRUCTIONS},
            {"role": "user", "content": user_query},
        ]
    )

    def _read(obj: Any, key: str) -> Any:
        if isinstance(obj, dict):
            return obj.get(key)
        return getattr(obj, key, None)

    def extract_chat_text(resp_obj: Any) -> str:
        output_text = _read(resp_obj, "output_text")
        if isinstance(output_text, str) and output_text.strip():
            return output_text.strip()

        parts: List[str] = []
        output = _read(resp_obj, "output")
        if isinstance(output, list):
            for msg in output:
                if _read(msg, "type") != "message":
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

    return normalize_query_cacheability_label(raw), raw, meta


def classify_query_type(client: OpenAI, model: str, user_query: str) -> Tuple[Optional[str], str, Dict[str, Any]]:
    t0 = time.perf_counter()

    resp = client.responses.create(
        model=model,
        input=[
            {"role": "system", "content": QUERY_CLASSIFIER_INSTRUCTIONS},
            {"role": "user", "content": user_query},
        ]
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
        query_embedding: Optional[List[float]] = None,
    ) -> Dict[str, Any]:
        t0 = time.perf_counter()
        if query_embedding is None:
            qvec = embed_query(client, user_q)
            query_embedding = qvec[0].tolist()
        t1 = time.perf_counter()

        if not restaurant_id:
            raise RuntimeError("restaurantId is required for retrieval")
        rows = self.supabase_store.match_chunks(
            restaurant_id,
            query_embedding,
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
                    "image_url": row.get("image_url", ""),
                    "extra_metadata": _coerce_extra_metadata(row.get("extra_metadata")),
                    "score": float(row.get("score") or 0.0),
                }
            )

        hydrate_ids = [str(r.get("id") or "") for r in results if str(r.get("id") or "")]
        if hydrate_ids:
            try:
                hydrated = self.supabase_store.get_knowledge_chunks_by_ids(hydrate_ids)
                hydrated_map = {
                    str(r.get("id") or ""): r
                    for r in hydrated
                    if str(r.get("id") or "")
                }
                for row in results:
                    rid = str(row.get("id") or "")
                    if not rid:
                        continue
                    extra = hydrated_map.get(rid, {})
                    img = str(extra.get("image_url") or "").strip()
                    if img and not str(row.get("image_url") or "").strip():
                        row["image_url"] = img
                    meta = _coerce_extra_metadata(extra.get("extra_metadata"))
                    if meta:
                        row["extra_metadata"] = meta
                        if not str(row.get("image_url") or "").strip():
                            fallback_img = _metadata_image_url(meta)
                            if fallback_img:
                                row["image_url"] = fallback_img
            except SupabaseStoreError as exc:
                log_event(
                    "retrieval_image_hydration_failed",
                    restaurant_id=restaurant_id,
                    error=str(exc),
                    chunk_ids=hydrate_ids[:10],
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
    query_cache: Optional[RedisQueryCache] = None
    query_cache_ttl_seconds: int = QUERY_CACHE_TTL_SECONDS_DEFAULT
    query_cache_namespace: str = QUERY_CACHE_NAMESPACE_DEFAULT
    query_cache_semantic_threshold: float = QUERY_CACHE_SEMANTIC_THRESHOLD_DEFAULT
    query_cache_semantic_max_candidates: int = QUERY_CACHE_SEMANTIC_MAX_CANDIDATES_DEFAULT
    query_cache_require_restaurant_relevance: bool = QUERY_CACHE_REQUIRE_RESTAURANT_RELEVANCE_DEFAULT
    query_cache_classifier_model: str = QUERY_CACHE_CLASSIFIER_MODEL_DEFAULT
    query_cache_classifier_timeout_ms: int = QUERY_CACHE_CLASSIFIER_TIMEOUT_MS_DEFAULT
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
    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""
    _cors_origin: Optional[str] = None
    _cors_allow_headers: Optional[str] = None

    def end_headers(self):
        if self._cors_origin:
            self.send_header("Access-Control-Allow-Origin", self._cors_origin)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Headers", self._cors_allow_headers or "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        if self.command == "OPTIONS":
            self.send_header("Access-Control-Max-Age", "600")
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

    def _insert_audit_event_safe(
        self,
        event_type: str,
        restaurant_id: Optional[str] = None,
        actor: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        if self.store is None:
            return
        try:
            self.store.insert_audit_event(
                event_type,
                restaurant_id=restaurant_id,
                actor=actor,
                details=details,
            )
        except SupabaseStoreError:
            pass

    def _source_refs(self, results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        refs = []
        for row in results[:MAX_SOURCES_SENT]:
            refs.append(
                {
                    "chunk_id": row.get("id"),
                    "score": float(row.get("score") or 0.0),
                    "source_url": row.get("source_url", ""),
                    "title": row.get("title", ""),
                    "image_url": row.get("image_url", ""),
                }
            )
        return refs

    def _load_restaurant_access_settings(
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
            self._send_json_error(500, "Supabase store is not configured", request_id=request_id)
            return None
        try:
            if not self.store.restaurant_exists(restaurant_id):
                self._send_json_error(400, "restaurantId does not exist", request_id=request_id)
                return None
            if not self.store.restaurant_has_active_subscription(restaurant_id):
                self._insert_audit_event_safe(
                    "subscription_inactive_block",
                    restaurant_id=restaurant_id,
                    actor="chat_api",
                    details={"request_id": request_id},
                )
                self._send_json_error(403, "Restaurant subscription is inactive", request_id=request_id)
                return None
            loaded = self.store.get_restaurant_security_settings(restaurant_id)
            settings.update(loaded)
        except SupabaseStoreError as exc:
            self._send_json_error(502, f"Failed to validate restaurantId: {exc}", request_id=request_id)
            return None
        return settings

    def _load_system_instructions(
        self,
        restaurant_id: str,
        request_id: str,
        language: str = DEFAULT_SESSION_LANGUAGE,
    ) -> Optional[str]:
        if self.store is None:
            return DEFAULT_SYSTEM_INSTRUCTIONS.replace("{language}", language)
        try:
            custom_prompt = self.store.get_restaurant_system_prompt(restaurant_id)
        except SupabaseStoreError as exc:
            self._send_json_error(502, f"Failed to load restaurant system_prompt: {exc}", request_id=request_id)
            return None
        if custom_prompt:
            return custom_prompt.replace("{language}", language)
        return DEFAULT_SYSTEM_INSTRUCTIONS.replace("{language}", language)

    def _system_instructions_from_prompt(self, custom_prompt: Optional[str], language: str) -> str:
        prompt = (custom_prompt or "").strip()
        if prompt:
            return prompt.replace("{language}", language)
        return DEFAULT_SYSTEM_INSTRUCTIONS.replace("{language}", language)

    def _load_chat_access_context(
        self,
        restaurant_id: str,
        origin: str,
        request_id: str,
    ) -> Optional[Dict[str, Any]]:
        if self.store is None:
            self._send_json_error(500, "Supabase store is not configured", request_id=request_id)
            return None
        try:
            access_context = self.store.chat_access_context(
                restaurant_id,
                origin,
                origin_preallowed=self.allow_localhost_origins and self._is_localhost_origin(origin),
            )
        except SupabaseStoreError as exc:
            self._send_json_error(502, f"Failed to validate restaurantId: {exc}", request_id=request_id)
            return None

        if not access_context.get("restaurant_exists"):
            self._send_json_error(400, "restaurantId does not exist", request_id=request_id)
            return None
        if not access_context.get("subscription_active"):
            self._insert_audit_event_safe(
                "subscription_inactive_block",
                restaurant_id=restaurant_id,
                actor="chat_api",
                details={"request_id": request_id},
            )
            self._send_json_error(403, "Restaurant subscription is inactive", request_id=request_id)
            return None
        if not access_context.get("origin_allowed"):
            self._send_json_error(403, "Origin is not allowed for this restaurant", request_id=request_id)
            self._insert_audit_event_safe(
                "origin_mismatch",
                restaurant_id=restaurant_id,
                actor="chat_api",
                details={"request_id": request_id, "origin": origin},
            )
            return None
        return access_context

    def _load_session_state(self, session_id: str, request_id: str) -> Dict[str, Any]:
        if self.store is None:
            state = dict(DEFAULT_SESSION_STATE)
            state["session_id"] = session_id
            return state
        try:
            state = self.store.get_session_state(session_id)
            if not isinstance(state, dict):
                raise SupabaseStoreError("get_session_state returned non-dict payload")
            state = _copy_session_state(state)
            state["session_id"] = session_id
            return state
        except SupabaseStoreError as exc:
            log_event(
                "session_state_load_failed",
                request_id=request_id,
                session_id=session_id,
                error=str(exc),
            )
            state = dict(DEFAULT_SESSION_STATE)
            state["session_id"] = session_id
            return state

    def _bootstrap_chat_session(
        self,
        restaurant_id: str,
        session_token: str,
        client_meta: Dict[str, Any],
        requested_language: Optional[str],
        request_id: str,
    ) -> Optional[Dict[str, Any]]:
        if self.store is None:
            self._send_json_error(500, "Supabase store is not configured", request_id=request_id)
            return None
        try:
            session_row = self.store.chat_session_bootstrap(
                restaurant_id,
                session_token,
                client_meta,
                language=requested_language,
            )
            session_id = str(session_row.get("session_id") or "")
            if not session_id:
                raise SupabaseStoreError("chat_session_bootstrap did not return session_id")
            try:
                stored_language = normalize_session_language(session_row.get("language"))
            except ValueError:
                stored_language = None
            session_state = _copy_session_state(session_row.get("session_state") or {})
            session_state["session_id"] = session_id
            return {
                "session_id": session_id,
                "language": stored_language or requested_language or DEFAULT_SESSION_LANGUAGE,
                "session_state": session_state,
            }
        except (SupabaseStoreError, ValueError) as exc:
            self._send_json_error(502, f"Failed to persist chat session: {exc}", request_id=request_id)
            return None

    def _persist_session_state(self, session_id: str, new_state: Dict[str, Any], request_id: str) -> None:
        if self.store is None:
            return
        payload = _copy_session_state(new_state)
        payload["session_id"] = session_id
        try:
            self.store.upsert_session_state(session_id, payload)
        except SupabaseStoreError as exc:
            log_event(
                "session_state_persist_failed",
                request_id=request_id,
                session_id=session_id,
                error=str(exc),
            )

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

    def _schedule_user_message_persistence(
        self,
        session_id: Optional[str],
        user_query: str,
        restaurant_id: str,
        request_id: str,
        stream_closed_event: threading.Event,
    ) -> None:
        if not self.persist_chat or not session_id or self.store is None:
            return

        store = self.store

        def _worker():
            message_id: Optional[str] = None
            t0 = time.perf_counter()
            try:
                user_row = store.insert_message(session_id, "user", user_query, return_row=True)
                if user_row is not None:
                    row_id = user_row.get("id")
                    if row_id:
                        message_id = str(row_id)
                log_event(
                    "user_message_persisted",
                    request_id=request_id,
                    restaurant_id=restaurant_id,
                    session_id=session_id,
                    message_id=message_id,
                    latency_ms=int((time.perf_counter() - t0) * 1000),
                )
            except Exception as exc:  # noqa: BLE001
                log_event(
                    "user_message_persist_failed",
                    request_id=request_id,
                    restaurant_id=restaurant_id,
                    session_id=session_id,
                    error=str(exc),
                    latency_ms=int((time.perf_counter() - t0) * 1000),
                )
                return

            stream_closed_event.wait()
            self._schedule_query_type_classification(
                message_id,
                user_query,
                restaurant_id=restaurant_id,
                request_id=request_id,
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
        parsed = urlparse(self.path).path
        if parsed not in {"/api/widget-token", "/api/chat-stream", "/api/stripe/webhook", "/healthz"}:
            if origin:
                self._cors_origin = origin
                self._cors_allow_headers = self.headers.get("Access-Control-Request-Headers")
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if not origin:
            self.send_response(400)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        self._cors_origin = origin
        self._cors_allow_headers = self.headers.get("Access-Control-Request-Headers")
        self.send_response(204)
        self.send_header("Content-Length", "0")
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
        if parsed == "/api/stripe/webhook":
            self._handle_stripe_webhook(request_id, start)
            return
        if parsed == "/api/chat-stream":
            self._handle_chat_stream(request_id, start)
            return
        if parsed == "/api/widget-token":
            self._handle_widget_token(request_id, start)
            return
        self._send_json_error(404, "Not found", request_id=request_id)

    def _handle_stripe_webhook(self, request_id: str, start: float):
        if self.store is None:
            self._send_json_error(500, "Supabase store is not configured", request_id=request_id)
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send_json_error(400, "Invalid Content-Length header", request_id=request_id)
            return
        raw_body = self.rfile.read(length) if length > 0 else b""
        signature_header = (self.headers.get("Stripe-Signature") or "").strip()

        try:
            result = process_stripe_webhook(
                raw_body=raw_body,
                signature_header=signature_header,
                webhook_secret=self.stripe_webhook_secret,
                stripe_secret_key=self.stripe_secret_key,
                store=self.store,
            )
        except StripeWebhookSignatureError as exc:
            self._insert_audit_event_safe(
                "stripe_webhook_failed",
                actor="chat_api",
                details={
                    "request_id": request_id,
                    "error": str(exc),
                    "latency_ms": int((time.perf_counter() - start) * 1000),
                },
            )
            self._send_json_error(400, str(exc), request_id=request_id)
            return
        except (StripeConfigError, StripeSdkError) as exc:
            self._insert_audit_event_safe(
                "stripe_webhook_failed",
                actor="chat_api",
                details={
                    "request_id": request_id,
                    "error": str(exc),
                    "latency_ms": int((time.perf_counter() - start) * 1000),
                },
            )
            self._send_json_error(500, str(exc), request_id=request_id)
            return
        except SupabaseStoreError as exc:
            self._insert_audit_event_safe(
                "stripe_webhook_failed",
                actor="chat_api",
                details={
                    "request_id": request_id,
                    "error": str(exc),
                    "latency_ms": int((time.perf_counter() - start) * 1000),
                },
            )
            self._send_json_error(502, f"Failed to persist Stripe webhook: {exc}", request_id=request_id)
            return
        except Exception as exc:  # noqa: BLE001
            self._insert_audit_event_safe(
                "stripe_webhook_failed",
                actor="chat_api",
                details={
                    "request_id": request_id,
                    "error": str(exc),
                    "latency_ms": int((time.perf_counter() - start) * 1000),
                },
            )
            self._send_json_error(500, f"Unhandled Stripe webhook error: {exc}", request_id=request_id)
            return

        audit_event_type = "stripe_webhook_processed" if result.ok else "stripe_webhook_failed"
        self._insert_audit_event_safe(
            audit_event_type,
            restaurant_id=result.restaurant_id,
            actor="chat_api",
            details={
                "request_id": request_id,
                "event_id": result.event_id,
                "event_type": result.event_type,
                "duplicate": result.duplicate,
                "message": result.message,
                "processing_status": result.processing_status,
                "latency_ms": int((time.perf_counter() - start) * 1000),
            },
        )

        response = {
            "ok": result.ok,
            "message": result.message,
            "eventId": result.event_id,
            "eventType": result.event_type,
        }
        data = json.dumps(response, ensure_ascii=False).encode("utf-8")
        self.send_response(result.status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("X-Request-Id", request_id)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

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

        try:
            requested_language = normalize_session_language(payload.get("language"))
        except ValueError as exc:
            self._send_json_error(400, str(exc), request_id=request_id)
            return

        security_settings = self._load_restaurant_access_settings(restaurant_id, request_id)
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

        new_session_requested = parse_bool_payload(payload.get("newSession"))
        session_token_input = "" if new_session_requested else payload.get("sessionToken") or ""
        try:
            session_token, generated = normalize_or_create_session_token(session_token_input)
        except ValueError as exc:
            self._send_json_error(400, str(exc), request_id=request_id)
            return
        generated = bool(generated or new_session_requested)

        try:
            restaurant_id = normalize_restaurant_id(payload.get("restaurantId") or "")
        except ValueError as exc:
            self._send_json_error(400, str(exc), request_id=request_id)
            return

        try:
            requested_language = normalize_session_language(payload.get("language"))
        except ValueError as exc:
            self._send_json_error(400, str(exc), request_id=request_id)
            return

        access_context = self._load_chat_access_context(restaurant_id, origin, request_id)
        if access_context is None:
            return

        widget_token = (payload.get("widgetToken") or "").strip()
        try:
            verify_widget_token(
                widget_token,
                self.widget_signing_keys,
                expected_restaurant_id=restaurant_id,
                expected_origin=origin,
                max_age_seconds=int(access_context["token_max_age_seconds"]),
            )
        except ValueError as exc:
            self._send_json_error(403, str(exc), request_id=request_id)
            return

        client_ip_hash = hash_client_ip(self._client_ip())
        ip_rate_key = f"rl:ip:{restaurant_id}:{client_ip_hash}"
        session_rate_key = f"rl:session:{restaurant_id}:{session_token}"
        if not self._apply_rate_limit(
            [
                (
                    ip_rate_key,
                    int(access_context["ip_max_requests"]),
                    int(access_context["ip_window_seconds"]),
                ),
                (
                    session_rate_key,
                    int(access_context["session_max_requests"]),
                    int(access_context["session_window_seconds"]),
                ),
            ],
            request_id=request_id,
            restaurant_id=restaurant_id,
            origin=origin,
            client_ip_hash=client_ip_hash,
        ):
            return

        session_bootstrap = self._bootstrap_chat_session(
            restaurant_id,
            session_token,
            self._extract_client_meta(),
            requested_language,
            request_id,
        )
        if session_bootstrap is None:
            return
        session_id = str(session_bootstrap["session_id"])
        resolved_language = str(session_bootstrap["language"])
        session_state = _copy_session_state(session_bootstrap["session_state"])
        session_state["session_id"] = session_id
        system_instructions = self._system_instructions_from_prompt(
            access_context.get("system_prompt"),
            resolved_language,
        )
        stream_closed_event = threading.Event()

        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("X-Request-Id", request_id)
        self.end_headers()

        assistant_text = ""
        results: List[Dict[str, Any]] = []
        turn: Optional[Dict[str, Any]] = None
        streamed_text_chunks: List[str] = []
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

            self._schedule_user_message_persistence(
                session_id,
                user_msg,
                restaurant_id=restaurant_id,
                request_id=request_id,
                stream_closed_event=stream_closed_event,
            )
            log_event(
                "chat_turn_start",
                request_id=request_id,
                restaurant_id=restaurant_id,
                session_id=session_id,
                language=resolved_language,
                new_session_requested=new_session_requested,
                generated_session_token=generated,
                raw_user_query=user_msg,
                previous_response_id=session_state.get("last_response_id"),
            )

            def _write_assistant_delta(content: str) -> None:
                if not content:
                    return
                streamed_text_chunks.append(content)
                line = json.dumps({"type": "delta", "content": content}, ensure_ascii=False) + "\n"
                self._write_chunk(line)

            turn = handle_chat_turn(
                client=self.client,
                retriever=self.retriever,
                store=self.store,
                restaurant_id=restaurant_id,
                user_query=user_msg,
                system_instructions=system_instructions,
                session_state=session_state,
                top_k=self.top_k,
                min_score=self.min_score,
                query_cache=self.query_cache,
                query_cache_namespace=self.query_cache_namespace,
                query_cache_ttl_seconds=self.query_cache_ttl_seconds,
                query_cache_semantic_threshold=self.query_cache_semantic_threshold,
                query_cache_semantic_max_candidates=self.query_cache_semantic_max_candidates,
                query_cache_require_restaurant_relevance=self.query_cache_require_restaurant_relevance,
                query_cache_classifier_model=self.query_cache_classifier_model,
                query_cache_classifier_timeout_ms=self.query_cache_classifier_timeout_ms,
                request_id=request_id,
                assistant_delta_callback=_write_assistant_delta,
            )
            assistant_text = (turn.get("assistant_text") or "").strip()
            results = list(turn.get("results") or [])

            log_event(
                "chat_turn_resolution",
                request_id=request_id,
                restaurant_id=restaurant_id,
                session_id=session_id,
                language=resolved_language,
                resolved_reference=turn.get("resolved_reference"),
                rewritten_retrieval_query=turn.get("retrieval_query"),
                retrieved_item_ids=[str(r.get("id") or "") for r in results],
                intent=turn.get("intent"),
                active_constraints=turn.get("active_constraints"),
                image_decision=turn.get("image_decision"),
                response_id=turn.get("response_id"),
                fallback_reason=turn.get("fallback_reason"),
                cache_hit=bool(turn.get("cache_hit")),
                cache_hit_stage=turn.get("cache_hit_stage"),
            )

            if assistant_text and not streamed_text_chunks:
                _write_assistant_delta(assistant_text)

            image_decision = dict(DEFAULT_IMAGE_DECISION)
            if isinstance(turn, dict):
                image_decision = _normalize_image_decision(turn.get("image_decision"))
            log_event(
                "image_model_decision",
                request_id=request_id,
                restaurant_id=restaurant_id,
                session_id=session_id,
                image_decision=image_decision,
                intent=(turn.get("intent") if isinstance(turn, dict) else None),
            )
            images_payload = build_image_payload_from_decision(results, image_decision)
            if bool(image_decision.get("include_images")) and not images_payload:
                log_event(
                    "images_requested_but_missing",
                    request_id=request_id,
                    restaurant_id=restaurant_id,
                    session_id=session_id,
                    retrieved_item_ids=[str(r.get("id") or "") for r in results],
                    retrieved_has_direct_image_url=[bool(str(r.get("image_url") or "").strip()) for r in results],
                    retrieved_has_metadata_image_url=[
                        bool(_metadata_image_url(_coerce_extra_metadata(r.get("extra_metadata")))) for r in results
                    ],
                    retrieved_metadata_keys=[
                        list(_coerce_extra_metadata(r.get("extra_metadata")).keys())[:10] for r in results
                    ],
                )
            if images_payload:
                images_line = json.dumps(
                    {
                        "type": "images",
                        "images": images_payload,
                    },
                    ensure_ascii=False,
                ) + "\n"
                self._write_chunk(images_line)

            log_event(
                "images_emitted",
                request_id=request_id,
                restaurant_id=restaurant_id,
                session_id=session_id,
                include_images=bool(image_decision.get("include_images")),
                requested_max_images=image_decision.get("max_images"),
                images_emitted_count=len(images_payload),
                image_chunk_ids=[x.get("chunk_id") for x in images_payload],
            )

            done_line = json.dumps({"type": "done"}) + "\n"
            self._write_chunk(done_line)
            self._end_chunked()
            stream_closed_event.set()

            if turn is not None:
                self._persist_session_state(
                    session_id=session_id,
                    new_state=turn.get("new_session_state") or {},
                    request_id=request_id,
                )

            if self.persist_chat and session_id is not None:
                latency_ms = int((time.perf_counter() - start) * 1000)
                self.store.insert_message(
                    session_id,
                    "assistant",
                    assistant_text,
                    sources=self._source_refs(results),
                    latency_ms=latency_ms,
                    delivery_status="complete",
                )

            log_event(
                "chat_request",
                request_id=request_id,
                restaurant_id=restaurant_id,
                origin=origin,
                client_ip_hash=client_ip_hash,
                language=resolved_language,
                status=200,
                new_session_requested=new_session_requested,
                generated_session_token=generated,
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
            stream_closed_event.set()

            if self.persist_chat and session_id is not None:
                try:
                    latency_ms = int((time.perf_counter() - start) * 1000)
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
                language=resolved_language,
                status=500,
                new_session_requested=new_session_requested,
                generated_session_token=generated,
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
    query_cache_redis_url = (os.getenv("QUERY_CACHE_REDIS_URL") or redis_rate_limit_url).strip()
    query_cache_enabled = parse_bool_env("QUERY_CACHE_ENABLED", bool(query_cache_redis_url))
    query_cache_ttl_seconds = int(os.getenv("QUERY_CACHE_TTL_SECONDS", str(QUERY_CACHE_TTL_SECONDS_DEFAULT)))
    query_cache_ttl_seconds = max(1, query_cache_ttl_seconds)
    query_cache_semantic_threshold = float(
        os.getenv("QUERY_CACHE_SEMANTIC_THRESHOLD", str(QUERY_CACHE_SEMANTIC_THRESHOLD_DEFAULT))
    )
    query_cache_semantic_threshold = max(0.0, min(1.0, query_cache_semantic_threshold))
    query_cache_semantic_max_candidates = int(
        os.getenv("QUERY_CACHE_SEMANTIC_MAX_CANDIDATES", str(QUERY_CACHE_SEMANTIC_MAX_CANDIDATES_DEFAULT))
    )
    query_cache_semantic_max_candidates = max(1, query_cache_semantic_max_candidates)
    query_cache_require_restaurant_relevance = parse_bool_env(
        "QUERY_CACHE_REQUIRE_RESTAURANT_RELEVANCE",
        QUERY_CACHE_REQUIRE_RESTAURANT_RELEVANCE_DEFAULT,
    )
    query_cache_classifier_model = (
        os.getenv("QUERY_CACHE_CLASSIFIER_MODEL") or QUERY_CACHE_CLASSIFIER_MODEL_DEFAULT
    ).strip()
    if not query_cache_classifier_model:
        query_cache_classifier_model = QUERY_CACHE_CLASSIFIER_MODEL_DEFAULT
    query_cache_classifier_timeout_ms = int(
        os.getenv("QUERY_CACHE_CLASSIFIER_TIMEOUT_MS", str(QUERY_CACHE_CLASSIFIER_TIMEOUT_MS_DEFAULT))
    )
    query_cache_classifier_timeout_ms = max(0, query_cache_classifier_timeout_ms)
    query_cache_namespace = (os.getenv("QUERY_CACHE_NAMESPACE") or QUERY_CACHE_NAMESPACE_DEFAULT).strip()
    if not query_cache_namespace:
        query_cache_namespace = QUERY_CACHE_NAMESPACE_DEFAULT
    stripe_secret_key = (os.getenv("STRIPE_SECRET_KEY") or "").strip()
    stripe_webhook_secret = (os.getenv("STRIPE_WEBHOOK_SECRET") or "").strip()
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
    try:
        ensure_stripe_configured(stripe_secret_key, stripe_webhook_secret)
        ensure_stripe_sdk_available()
    except (StripeConfigError, StripeSdkError) as exc:
        raise SystemExit(str(exc)) from exc

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
    if query_cache_enabled and query_cache_redis_url:
        try:
            ChatHandler.query_cache = RedisQueryCache(query_cache_redis_url)
        except Exception as exc:  # noqa: BLE001
            ChatHandler.query_cache = None
            print(f"⚠️ Query cache disabled due to init error: {exc}")
    else:
        ChatHandler.query_cache = None
    ChatHandler.query_cache_ttl_seconds = query_cache_ttl_seconds
    ChatHandler.query_cache_namespace = query_cache_namespace
    ChatHandler.query_cache_semantic_threshold = query_cache_semantic_threshold
    ChatHandler.query_cache_semantic_max_candidates = query_cache_semantic_max_candidates
    ChatHandler.query_cache_require_restaurant_relevance = query_cache_require_restaurant_relevance
    ChatHandler.query_cache_classifier_model = query_cache_classifier_model
    ChatHandler.query_cache_classifier_timeout_ms = query_cache_classifier_timeout_ms
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
    ChatHandler.stripe_secret_key = stripe_secret_key
    ChatHandler.stripe_webhook_secret = stripe_webhook_secret

    server = ThreadingHTTPServer(("0.0.0.0", port), ChatHandler)
    print(f"✅ Chat server running at http://localhost:{port}")
    print("   - Retrieval backend: supabase")
    print(f"   - Chat persistence: {'enabled' if persist_chat else 'disabled'}")
    print("   - Stream API: POST /api/chat-stream")
    print("   - Token API: POST /api/widget-token")
    print("   - Stripe API: POST /api/stripe/webhook")
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
    if query_cache_enabled and query_cache_redis_url:
        print(
            "   - Query cache: enabled "
            f"(redis, ttl={query_cache_ttl_seconds}s, namespace={query_cache_namespace}, "
            f"semantic_threshold={query_cache_semantic_threshold}, "
            f"semantic_max_candidates={query_cache_semantic_max_candidates}, "
            f"require_restaurant_relevance={'yes' if query_cache_require_restaurant_relevance else 'no'}, "
            f"classifier_model={query_cache_classifier_model}, "
            f"classifier_timeout_ms={query_cache_classifier_timeout_ms})"
        )
    else:
        print("   - Query cache: disabled")
    print(f"   - Signing keys loaded: {len(widget_signing_keys)}")
    print(f"   - Active widget key id: {widget_active_kid}")
    print(f"   - Query classifier model: {query_classifier_model}")
    print("   - Stripe billing: enabled")
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
