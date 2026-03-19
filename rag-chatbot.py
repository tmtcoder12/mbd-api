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
    "You are a restaurant assistant.\n"
    "Answer naturally, briefly, and helpfully.\n"
    "Use ONLY the provided menu context for factual claims.\n"
    "If the menu context does not support a fact, say you are not sure.\n"
    "Resolve follow-up references using session context when confidence is high.\n"
    "If reference ambiguity is high, ask one short clarifying question.\n"
    "Mention specific item names clearly instead of vague pronouns.\n"
    "Do not invent ingredients, prices, sides, dietary tags, or availability."
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

IMAGE_INTENT_KEYWORDS_RECOMMEND = (
    "recommend",
    "suggest",
    "best",
    "popular",
    "top pick",
    "what should i get",
)

IMAGE_INTENT_KEYWORDS_PHOTO = (
    "photo",
    "picture",
    "image",
    "look like",
    "show me",
    "see",
)

MAX_IMAGE_URLS_SENT = 3

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


def should_include_images(user_query: str, intent: Optional[str]) -> Tuple[bool, str]:
    lowered = (user_query or "").lower()
    if any(k in lowered for k in IMAGE_INTENT_KEYWORDS_PHOTO):
        return True, "photo_keyword_match"
    if any(k in lowered for k in IMAGE_INTENT_KEYWORDS_RECOMMEND):
        return True, "recommendation_keyword_match"
    if intent in {"compare_price", "lighter_option"} and "show" in lowered:
        return True, "intent_plus_show"
    return False, "no_image_intent"


def build_image_payload(results: List[Dict[str, Any]], max_images: int = MAX_IMAGE_URLS_SENT) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    seen_urls = set()
    limit = max(1, int(max_images))
    for row in results:
        image_url = str(row.get("image_url") or "").strip()
        if not image_url:
            continue
        if image_url in seen_urls:
            continue
        seen_urls.add(image_url)
        out.append(
            {
                "chunk_id": str(row.get("id") or ""),
                "title": str(row.get("title") or ""),
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
) -> Dict[str, Any]:
    prep = retriever.retrieve(
        client=client,
        user_q=retrieval_query,
        top_k=max(top_k * 2, top_k),
        min_score=min_score,
        restaurant_id=restaurant_id,
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


def create_assistant_response(
    client: OpenAI,
    system_instructions: str,
    user_query: str,
    menu_context: str,
    session_context: str,
    previous_response_id: Optional[str],
) -> Tuple[str, Optional[str]]:
    user_payload = (
        "Session context:\n"
        f"{session_context}\n\n"
        "Retrieved menu context (facts):\n"
        f"{menu_context or '[no menu context found]'}\n\n"
        "Current user message:\n"
        f"{user_query}"
    )
    kwargs: Dict[str, Any] = {
        "model": CHAT_MODEL,
        "instructions": system_instructions,
        "input": [{"role": "user", "content": [{"type": "input_text", "text": user_payload}]}],
    }
    if previous_response_id:
        kwargs["previous_response_id"] = previous_response_id
    resp = client.responses.create(**kwargs)
    text = _extract_response_text(resp).strip()
    if not text:
        text = "I’m sorry, I couldn’t generate a response right now."
    return text, getattr(resp, "id", None)


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
) -> Dict[str, Any]:
    raw_query = (user_query or "").strip()
    intent = infer_intent(raw_query) or session_state.get("last_intent")
    active_constraints = extract_active_constraints(raw_query, session_state.get("active_constraints") or {})
    resolved_reference = resolve_reference(raw_query, session_state, store)
    retrieval_query = build_retrieval_query(raw_query, resolved_reference, intent, active_constraints)

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
            "new_session_state": new_state,
            "fallback_reason": "ambiguity",
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
        return {
            "assistant_text": assistant_text,
            "results": [],
            "retrieval_query": retrieval_query,
            "resolved_reference": resolved_reference,
            "intent": intent,
            "active_constraints": active_constraints,
            "response_id": None,
            "new_session_state": new_state,
            "fallback_reason": "no_retrieval_results",
        }

    session_context = build_session_context(
        session_state=session_state,
        resolved_reference=resolved_reference,
        intent=intent,
        active_constraints=active_constraints,
    )
    menu_context = format_menu_context(results)
    assistant_text, response_id = create_assistant_response(
        client=client,
        system_instructions=system_instructions,
        user_query=raw_query,
        menu_context=menu_context,
        session_context=session_context,
        previous_response_id=session_state.get("last_response_id"),
    )
    new_state = infer_new_session_state(
        session_state,
        resolved_reference,
        results,
        intent,
        active_constraints,
    )
    new_state["last_response_id"] = response_id
    return {
        "assistant_text": assistant_text,
        "results": results,
        "retrieval_query": retrieval_query,
        "resolved_reference": resolved_reference,
        "intent": intent,
        "active_constraints": active_constraints,
        "response_id": response_id,
        "new_session_state": new_state,
        "fallback_reason": None,
    }


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
                    "image_url": row.get("image_url", ""),
                    "score": float(row.get("score") or 0.0),
                }
            )

        missing_image_ids = [
            str(r.get("id") or "")
            for r in results
            if str(r.get("id") or "") and not str(r.get("image_url") or "").strip()
        ]
        if missing_image_ids:
            try:
                hydrated = self.supabase_store.get_knowledge_chunks_by_ids(missing_image_ids)
                hydrated_map = {
                    str(r.get("id") or ""): r
                    for r in hydrated
                    if str(r.get("id") or "")
                }
                for row in results:
                    rid = str(row.get("id") or "")
                    if not rid or str(row.get("image_url") or "").strip():
                        continue
                    extra = hydrated_map.get(rid, {})
                    img = str(extra.get("image_url") or "").strip()
                    if img:
                        row["image_url"] = img
            except SupabaseStoreError:
                pass

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
                    "image_url": row.get("image_url", ""),
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
        if self.store is None:
            self._send_json_error(500, "Supabase store is not configured", request_id=request_id)
            return
        try:
            session_id = self.store.upsert_session(restaurant_id, session_token, self._extract_client_meta())
            if self.persist_chat:
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
        results: List[Dict[str, Any]] = []
        turn: Optional[Dict[str, Any]] = None
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

            session_state = self._load_session_state(session_id, request_id)
            log_event(
                "chat_turn_start",
                request_id=request_id,
                restaurant_id=restaurant_id,
                session_id=session_id,
                raw_user_query=user_msg,
                previous_response_id=session_state.get("last_response_id"),
            )

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
            )
            assistant_text = (turn.get("assistant_text") or "").strip()
            results = list(turn.get("results") or [])

            log_event(
                "chat_turn_resolution",
                request_id=request_id,
                restaurant_id=restaurant_id,
                session_id=session_id,
                resolved_reference=turn.get("resolved_reference"),
                rewritten_retrieval_query=turn.get("retrieval_query"),
                retrieved_item_ids=[str(r.get("id") or "") for r in results],
                intent=turn.get("intent"),
                active_constraints=turn.get("active_constraints"),
                response_id=turn.get("response_id"),
                fallback_reason=turn.get("fallback_reason"),
            )

            if assistant_text:
                line = json.dumps({"type": "delta", "content": assistant_text}, ensure_ascii=False) + "\n"
                self._write_chunk(line)

            turn_intent = turn.get("intent") if isinstance(turn, dict) else None
            include_images, image_gate_reason = should_include_images(user_msg, turn_intent)
            log_event(
                "image_intent_gate",
                request_id=request_id,
                restaurant_id=restaurant_id,
                session_id=session_id,
                include_images=include_images,
                reason=image_gate_reason,
                intent=turn_intent,
            )
            images_payload: List[Dict[str, Any]] = []
            if include_images:
                images_payload = build_image_payload(results, max_images=MAX_IMAGE_URLS_SENT)
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
                images_emitted_count=len(images_payload),
                image_chunk_ids=[x.get("chunk_id") for x in images_payload],
            )

            done_line = json.dumps({"type": "done"}) + "\n"
            self._write_chunk(done_line)
            self._end_chunked()

            self._schedule_query_type_classification(
                user_message_id,
                user_msg,
                restaurant_id=restaurant_id,
                request_id=request_id,
            )

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
