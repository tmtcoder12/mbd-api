"""Application services shared by FastAPI routes."""

from __future__ import annotations

import hashlib
import json
import logging
import queue
import threading
import time
from collections.abc import Iterator
from typing import Any
from urllib.parse import urlparse

from openai import OpenAI

from . import caching, rag, security
from .billing import ensure_stripe_configured, process_stripe_webhook
from .config import Settings
from .models import ChatStreamRequest
from .repository import SupabaseStore, SupabaseStoreError

logger = logging.getLogger(__name__)


class ServiceError(RuntimeError):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def normalize_origin(origin: str) -> str:
    return (origin or "").strip().lower().rstrip("/")


def is_localhost_origin(origin: str) -> bool:
    parsed = urlparse(origin)
    return parsed.scheme in {"http", "https"} and parsed.hostname in {"localhost", "127.0.0.1"}


def client_ip_hash(client_ip: str) -> str:
    return hashlib.sha256((client_ip or "unknown").encode("utf-8")).hexdigest()[:16]


class ApplicationServices:
    """Owns external clients and implements tenant-aware application operations."""

    def __init__(
        self,
        settings: Settings,
        *,
        openai_client: Any | None = None,
        store: SupabaseStore | Any | None = None,
        rate_limiter: Any | None = None,
        query_cache: Any | None = None,
    ):
        self.settings = settings
        self.client = openai_client or OpenAI(
            api_key=settings.openai_api_key.get_secret_value(),
            timeout=settings.openai_timeout_seconds,
            max_retries=settings.openai_max_retries,
        )
        self.store = store or SupabaseStore(
            settings.supabase_url,
            settings.supabase_service_role_key.get_secret_value(),
            timeout_s=settings.supabase_timeout_seconds,
        )
        self.retriever = rag.Retriever(self.store)
        self.widget_signing_keys = security.parse_signing_keys(settings.widget_signing_keys.get_secret_value())
        if settings.widget_active_kid not in self.widget_signing_keys:
            raise ValueError("WIDGET_ACTIVE_KID must reference a key present in WIDGET_SIGNING_KEYS")

        redis_rate_url = (settings.rate_limit_redis_url or "").strip()
        self.rate_limiter = rate_limiter or (
            caching.RedisSlidingWindowRateLimiter(redis_rate_url, timeout_s=settings.readiness_timeout_seconds)
            if redis_rate_url
            else caching.SlidingWindowRateLimiter(
                settings.rate_limit_requests_per_minute,
                settings.rate_limit_window_seconds,
            )
        )

        cache_url = (settings.query_cache_redis_url or settings.rate_limit_redis_url or "").strip()
        if query_cache is not None:
            self.query_cache = query_cache
        elif settings.query_cache_enabled:
            self.query_cache = caching.RedisQueryCache(cache_url, timeout_s=settings.readiness_timeout_seconds)
        else:
            self.query_cache = None

        if settings.stripe_webhooks_enabled:
            ensure_stripe_configured(
                settings.secret(settings.stripe_secret_key),
                settings.secret(settings.stripe_webhook_secret),
            )

    def close(self) -> None:
        close = getattr(self.store, "close", None)
        if callable(close):
            close()
        for candidate in (self.rate_limiter, self.query_cache):
            redis_client = getattr(candidate, "client", None)
            closer = getattr(redis_client, "close", None)
            if callable(closer):
                closer()

    def ready(self) -> dict[str, Any]:
        result: dict[str, Any] = {"ok": True, "supabase": "ok", "redis": "disabled"}
        try:
            if not self.store.healthcheck(timeout_s=self.settings.readiness_timeout_seconds):
                raise SupabaseStoreError("health check returned false")
        except Exception:
            logger.warning("Supabase readiness check failed", exc_info=True)
            result.update({"ok": False, "supabase": "unavailable"})

        redis_client = getattr(self.rate_limiter, "client", None) or getattr(self.query_cache, "client", None)
        if redis_client is not None:
            try:
                redis_client.ping()
                result["redis"] = "ok"
            except Exception:
                logger.warning("Redis readiness check failed", exc_info=True)
                result.update({"ok": False, "redis": "unavailable"})
        return result

    def _default_security_settings(self) -> dict[str, int]:
        return {
            "ip_max_requests": self.settings.rate_limit_requests_per_minute,
            "ip_window_seconds": self.settings.rate_limit_window_seconds,
            "session_max_requests": self.settings.session_rate_limit_requests_per_minute,
            "session_window_seconds": self.settings.session_rate_limit_window_seconds,
            "token_max_age_seconds": self.settings.widget_token_max_age_seconds,
            "token_issue_max_requests": self.settings.token_issue_rate_limit_requests_per_minute,
            "token_issue_window_seconds": self.settings.token_issue_rate_limit_window_seconds,
        }

    def _allow_rate(self, key: str, maximum: int, window: int) -> None:
        try:
            allowed = self.rate_limiter.allow(key, max_requests=maximum, window_seconds=window)
        except TypeError:
            allowed = self.rate_limiter.allow(key, maximum, window)
        if not allowed:
            raise ServiceError(429, "Rate limit exceeded. Please try again in a minute.")

    def issue_widget_token(self, restaurant_id: str, origin: str, client_ip: str) -> dict[str, Any]:
        origin = normalize_origin(origin)
        if not origin:
            raise ServiceError(403, "Origin header is required")
        try:
            if not self.store.restaurant_exists(restaurant_id):
                raise ServiceError(400, "restaurantId does not exist")
            if not self.store.restaurant_has_active_subscription(restaurant_id):
                raise ServiceError(403, "Restaurant subscription is inactive")
            security_settings = self._default_security_settings()
            security_settings.update(self.store.get_restaurant_security_settings(restaurant_id))
            local_allowed = self.settings.allow_localhost_origins and is_localhost_origin(origin)
            if not local_allowed and not self.store.origin_allowed_for_restaurant(restaurant_id, origin):
                raise ServiceError(403, "Origin is not allowed for this restaurant")
        except ServiceError:
            raise
        except SupabaseStoreError as exc:
            logger.warning("Restaurant access lookup failed", exc_info=True)
            raise ServiceError(502, "Unable to validate restaurant access") from exc

        ip_hash = client_ip_hash(client_ip)
        self._allow_rate(
            f"rl:token:{restaurant_id}:{ip_hash}",
            int(security_settings["token_issue_max_requests"]),
            int(security_settings["token_issue_window_seconds"]),
        )
        token, expires_at = security.build_widget_token(
            restaurant_id,
            origin,
            self.settings.widget_active_kid,
            self.widget_signing_keys[self.settings.widget_active_kid],
            int(security_settings["token_max_age_seconds"]),
        )
        return {"widgetToken": token, "expiresAt": expires_at}

    def _chat_access(self, restaurant_id: str, origin: str) -> dict[str, Any]:
        try:
            context = self.store.chat_access_context(
                restaurant_id,
                origin,
                origin_preallowed=self.settings.allow_localhost_origins and is_localhost_origin(origin),
            )
        except SupabaseStoreError as exc:
            logger.warning("Chat access lookup failed", exc_info=True)
            raise ServiceError(502, "Unable to validate restaurant access") from exc
        if not context.get("restaurant_exists"):
            raise ServiceError(400, "restaurantId does not exist")
        if not context.get("subscription_active"):
            raise ServiceError(403, "Restaurant subscription is inactive")
        if not context.get("origin_allowed"):
            raise ServiceError(403, "Origin is not allowed for this restaurant")
        return context

    @staticmethod
    def _source_refs(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                "chunk_id": row.get("id"),
                "score": float(row.get("score") or 0.0),
                "source_url": row.get("source_url", ""),
                "title": row.get("title", ""),
                "image_url": row.get("image_url", ""),
            }
            for row in results[: rag.MAX_SOURCES_SENT]
        ]

    @staticmethod
    def _ndjson(payload: dict[str, Any]) -> bytes:
        return (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")

    def stream_chat(
        self,
        request: ChatStreamRequest,
        *,
        origin: str,
        client_ip: str,
        request_id: str,
        client_meta: dict[str, Any],
    ) -> Iterator[bytes]:
        started = time.perf_counter()
        origin = normalize_origin(origin)
        restaurant_id = str(request.restaurant_id)
        access = self._chat_access(restaurant_id, origin)

        try:
            security.verify_widget_token(
                request.widget_token,
                self.widget_signing_keys,
                expected_restaurant_id=restaurant_id,
                expected_origin=origin,
                max_age_seconds=int(access["token_max_age_seconds"]),
            )
        except ValueError as exc:
            raise ServiceError(403, str(exc)) from exc

        supplied_session = "" if request.new_session else str(request.session_token or "")
        session_token, generated = security.normalize_or_create_session_token(supplied_session)
        generated = generated or request.new_session
        ip_hash = client_ip_hash(client_ip)
        self._allow_rate(
            f"rl:ip:{restaurant_id}:{ip_hash}",
            int(access["ip_max_requests"]),
            int(access["ip_window_seconds"]),
        )
        self._allow_rate(
            f"rl:session:{restaurant_id}:{session_token}",
            int(access["session_max_requests"]),
            int(access["session_window_seconds"]),
        )

        try:
            bootstrap = self.store.chat_session_bootstrap(
                restaurant_id,
                session_token,
                client_meta,
                language=request.language,
            )
        except SupabaseStoreError as exc:
            logger.warning("Session bootstrap failed", exc_info=True)
            raise ServiceError(502, "Unable to start chat session") from exc

        session_id = str(bootstrap["session_id"])
        language = str(bootstrap.get("language") or request.language or rag.DEFAULT_SESSION_LANGUAGE)
        session_state = rag.copy_session_state(bootstrap.get("session_state") or {})
        session_state["session_id"] = session_id
        prompt = str(access.get("system_prompt") or rag.DEFAULT_SYSTEM_INSTRUCTIONS).replace("{language}", language)

        events: queue.Queue[bytes | None] = queue.Queue()
        events.put(
            self._ndjson(
                {
                    "type": "session",
                    "sessionToken": session_token,
                    "restaurantId": restaurant_id,
                    "generated": bool(generated),
                }
            )
        )

        def worker() -> None:
            assistant_text = ""
            results: list[dict[str, Any]] = []
            try:
                if self.settings.chat_persistence:
                    self.store.insert_message(session_id, "user", request.message)

                turn = rag.handle_chat_turn(
                    client=self.client,
                    retriever=self.retriever,
                    store=self.store,
                    restaurant_id=restaurant_id,
                    user_query=request.message,
                    system_instructions=prompt,
                    session_state=session_state,
                    top_k=self.settings.top_k,
                    min_score=self.settings.min_score_default,
                    query_cache=self.query_cache,
                    query_cache_namespace=self.settings.query_cache_namespace,
                    query_cache_ttl_seconds=self.settings.query_cache_ttl_seconds,
                    query_cache_semantic_threshold=self.settings.query_cache_semantic_threshold,
                    query_cache_semantic_max_candidates=self.settings.query_cache_semantic_max_candidates,
                    query_cache_require_restaurant_relevance=self.settings.query_cache_require_restaurant_relevance,
                    query_cache_classifier_model=self.settings.query_cache_classifier_model,
                    query_cache_classifier_timeout_ms=self.settings.query_cache_classifier_timeout_ms,
                    request_id=request_id,
                    assistant_delta_callback=lambda text: events.put(self._ndjson({"type": "delta", "content": text})),
                )
                assistant_text = str(turn.get("assistant_text") or "").strip()
                results = list(turn.get("results") or [])
                if turn.get("cache_hit") or turn.get("fallback_reason"):
                    events.put(self._ndjson({"type": "delta", "content": assistant_text}))
                images = rag.build_image_payload_from_decision(results, turn.get("image_decision") or {})
                if images:
                    events.put(self._ndjson({"type": "images", "images": images}))
                self.store.upsert_session_state(session_id, turn.get("new_session_state") or {})
                if self.settings.chat_persistence:
                    self.store.insert_message(
                        session_id,
                        "assistant",
                        assistant_text,
                        sources=self._source_refs(results),
                        latency_ms=int((time.perf_counter() - started) * 1000),
                    )
                events.put(self._ndjson({"type": "done"}))
            except Exception:
                logger.exception(
                    "Chat stream failed",
                    extra={"request_id": request_id, "restaurant_id": restaurant_id, "event": "chat_error"},
                )
                events.put(self._ndjson({"type": "error", "message": "Unable to complete this response"}))
                if self.settings.chat_persistence:
                    try:
                        self.store.insert_message(
                            session_id,
                            "assistant",
                            assistant_text or "[stream aborted]",
                            sources=[],
                            latency_ms=int((time.perf_counter() - started) * 1000),
                            delivery_status="error",
                        )
                    except Exception:
                        logger.warning("Failed to persist stream error", exc_info=True)
            finally:
                events.put(None)

        threading.Thread(target=worker, name=f"chat-{request_id}", daemon=True).start()
        while True:
            event = events.get()
            if event is None:
                break
            yield event

    def handle_stripe_webhook(self, raw_body: bytes, signature: str) -> dict[str, Any]:
        if not self.settings.stripe_webhooks_enabled:
            raise ServiceError(503, "Stripe webhooks are disabled")
        try:
            result = process_stripe_webhook(
                raw_body=raw_body,
                signature_header=signature,
                webhook_secret=self.settings.secret(self.settings.stripe_webhook_secret),
                stripe_secret_key=self.settings.secret(self.settings.stripe_secret_key),
                store=self.store,
            )
        except Exception as exc:
            logger.warning("Stripe webhook processing failed", exc_info=True)
            status = 400 if "signature" in str(exc).lower() else 502
            raise ServiceError(status, "Unable to process Stripe webhook") from exc
        return {
            "ok": result.ok,
            "message": result.message,
            "eventId": result.event_id,
            "eventType": result.event_type,
        }
