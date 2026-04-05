"""Supabase PostgREST adapter for chat persistence and vector retrieval."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class SupabaseStoreError(RuntimeError):
    """Raised when Supabase API operations fail."""


class SupabaseStore:
    def __init__(self, url: str, service_role_key: str, timeout_s: float = 30.0):
        self.base_url = url.rstrip("/")
        self.timeout_s = timeout_s
        self.headers = {
            "apikey": service_role_key,
            "Authorization": f"Bearer {service_role_key}",
            "Content-Type": "application/json",
        }

    def _request(
        self,
        method: str,
        path: str,
        payload: Optional[Any] = None,
        query: Optional[Dict[str, str]] = None,
        prefer: Optional[str] = None,
    ) -> Any:
        qs = ""
        if query:
            qs = "?" + urlencode(query, safe="(),")

        body = None
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        req = Request(f"{self.base_url}{path}{qs}", data=body, method=method.upper())
        for k, v in self.headers.items():
            req.add_header(k, v)
        if prefer:
            req.add_header("Prefer", prefer)

        try:
            with urlopen(req, timeout=self.timeout_s) as resp:
                raw = resp.read().decode("utf-8")
                if not raw:
                    return None
                ct = resp.headers.get("Content-Type", "")
                if "application/json" in ct:
                    return json.loads(raw)
                return raw
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise SupabaseStoreError(f"HTTP {exc.code} {method} {path}: {detail}") from exc
        except URLError as exc:
            raise SupabaseStoreError(f"Network error calling Supabase: {exc}") from exc

    @staticmethod
    def _vector_literal(vector: List[float]) -> str:
        # Postgres pgvector literal accepted by RPC arg casts.
        return "[" + ",".join(f"{float(v):.8f}" for v in vector) + "]"

    @staticmethod
    def _normalize_origin(origin: str) -> str:
        normalized = (origin or "").strip().lower()
        return normalized.rstrip("/")

    @staticmethod
    def _require_uuid(value: str, field_name: str) -> str:
        raw = (value or "").strip()
        if not raw:
            raise SupabaseStoreError(f"{field_name} is required")
        try:
            return str(uuid.UUID(raw))
        except ValueError as exc:
            raise SupabaseStoreError(f"{field_name} must be a valid UUID") from exc

    @staticmethod
    def _normalize_optional_text(value: Optional[Any]) -> Optional[str]:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    def upsert_restaurant(self, restaurant_id: str, slug: Optional[str] = None, name: Optional[str] = None) -> str:
        rid = self._require_uuid(restaurant_id, "restaurant_id")
        payload = {
            "p_restaurant_id": rid,
            "p_slug": (slug or "").strip() or None,
            "p_name": (name or "").strip() or None,
        }
        data = self._request("POST", "/rest/v1/rpc/upsert_restaurant", payload=payload)

        if isinstance(data, list) and data:
            row = data[0]
            out_id = row.get("restaurant_id")
            if out_id:
                return str(out_id)
        if isinstance(data, dict):
            out_id = data.get("restaurant_id")
            if out_id:
                return str(out_id)
        if isinstance(data, str) and data:
            return data

        raise SupabaseStoreError("upsert_restaurant did not return restaurant_id")

    def restaurant_exists(self, restaurant_id: str) -> bool:
        rid = self._require_uuid(restaurant_id, "restaurant_id")
        data = self._request(
            "GET",
            "/rest/v1/restaurants",
            query={"select": "id", "id": f"eq.{rid}", "limit": "1"},
        )
        return isinstance(data, list) and len(data) > 0

    def restaurant_has_active_subscription(self, restaurant_id: str) -> bool:
        rid = self._require_uuid(restaurant_id, "restaurant_id")
        data = self._request(
            "POST",
            "/rest/v1/rpc/restaurant_has_active_subscription",
            payload={"p_restaurant_id": rid},
        )
        if isinstance(data, bool):
            return data
        if isinstance(data, list) and data:
            row = data[0]
            if isinstance(row, bool):
                return row
            if isinstance(row, dict):
                value = row.get("restaurant_has_active_subscription")
                if isinstance(value, bool):
                    return value
                if len(row) == 1:
                    only_value = next(iter(row.values()))
                    if isinstance(only_value, bool):
                        return only_value
        if isinstance(data, dict):
            value = data.get("restaurant_has_active_subscription")
            if isinstance(value, bool):
                return value
            if len(data) == 1:
                only_value = next(iter(data.values()))
                if isinstance(only_value, bool):
                    return only_value
        if isinstance(data, str):
            normalized = data.strip().lower()
            if normalized in {"true", "false"}:
                return normalized == "true"
        raise SupabaseStoreError("restaurant_has_active_subscription returned an invalid payload")

    def get_restaurant_subscription_by_subscription_id(self, stripe_subscription_id: str) -> Optional[Dict[str, Any]]:
        subscription_id = self._normalize_optional_text(stripe_subscription_id)
        if not subscription_id:
            raise SupabaseStoreError("stripe_subscription_id is required")
        data = self._request(
            "GET",
            "/rest/v1/restaurant_subscriptions",
            query={
                "select": (
                    "restaurant_id,stripe_customer_id,stripe_subscription_id,stripe_payment_link_id,"
                    "stripe_checkout_session_id,client_reference_id,stripe_price_id,stripe_product_id,"
                    "stripe_subscription_status,current_period_start,current_period_end,cancel_at,"
                    "canceled_at,ended_at,last_checkout_completed_at,last_synced_at,created_at,updated_at"
                ),
                "stripe_subscription_id": f"eq.{subscription_id}",
                "limit": "1",
            },
        )
        if isinstance(data, list) and data:
            row = data[0]
            if isinstance(row, dict):
                return row
        return None

    def get_restaurant_subscription_by_customer_id(self, stripe_customer_id: str) -> Optional[Dict[str, Any]]:
        customer_id = self._normalize_optional_text(stripe_customer_id)
        if not customer_id:
            raise SupabaseStoreError("stripe_customer_id is required")
        data = self._request(
            "GET",
            "/rest/v1/restaurant_subscriptions",
            query={
                "select": (
                    "restaurant_id,stripe_customer_id,stripe_subscription_id,stripe_payment_link_id,"
                    "stripe_checkout_session_id,client_reference_id,stripe_price_id,stripe_product_id,"
                    "stripe_subscription_status,current_period_start,current_period_end,cancel_at,"
                    "canceled_at,ended_at,last_checkout_completed_at,last_synced_at,created_at,updated_at"
                ),
                "stripe_customer_id": f"eq.{customer_id}",
                "limit": "1",
            },
        )
        if isinstance(data, list) and data:
            row = data[0]
            if isinstance(row, dict):
                return row
        return None

    def upsert_restaurant_subscription(self, restaurant_id: str, fields: Dict[str, Any]) -> Dict[str, Any]:
        rid = self._require_uuid(restaurant_id, "restaurant_id")
        payload = {
            "restaurant_id": rid,
            "stripe_customer_id": self._normalize_optional_text(fields.get("stripe_customer_id")),
            "stripe_subscription_id": self._normalize_optional_text(fields.get("stripe_subscription_id")),
            "stripe_payment_link_id": self._normalize_optional_text(fields.get("stripe_payment_link_id")),
            "stripe_checkout_session_id": self._normalize_optional_text(fields.get("stripe_checkout_session_id")),
            "client_reference_id": self._normalize_optional_text(fields.get("client_reference_id")),
            "stripe_price_id": self._normalize_optional_text(fields.get("stripe_price_id")),
            "stripe_product_id": self._normalize_optional_text(fields.get("stripe_product_id")),
            "stripe_subscription_status": self._normalize_optional_text(fields.get("stripe_subscription_status")),
            "current_period_start": fields.get("current_period_start"),
            "current_period_end": fields.get("current_period_end"),
            "cancel_at": fields.get("cancel_at"),
            "canceled_at": fields.get("canceled_at"),
            "ended_at": fields.get("ended_at"),
            "last_checkout_completed_at": fields.get("last_checkout_completed_at"),
            "last_synced_at": fields.get("last_synced_at") or datetime.now(timezone.utc).isoformat(),
            "updated_at": fields.get("updated_at") or datetime.now(timezone.utc).isoformat(),
        }
        if not payload["stripe_subscription_status"]:
            raise SupabaseStoreError("stripe_subscription_status is required")
        data = self._request(
            "POST",
            "/rest/v1/restaurant_subscriptions",
            payload=[payload],
            query={"on_conflict": "restaurant_id"},
            prefer="resolution=merge-duplicates,return=representation",
        )
        if isinstance(data, list) and data:
            row = data[0]
            if isinstance(row, dict):
                return row
        if isinstance(data, dict):
            return data
        raise SupabaseStoreError("upsert_restaurant_subscription did not return row data")

    def get_stripe_webhook_event(self, event_id: str) -> Optional[Dict[str, Any]]:
        normalized_event_id = self._normalize_optional_text(event_id)
        if not normalized_event_id:
            raise SupabaseStoreError("event_id is required")
        data = self._request(
            "GET",
            "/rest/v1/stripe_webhook_events",
            query={
                "select": (
                    "event_id,event_type,stripe_created_at,restaurant_id,stripe_customer_id,"
                    "stripe_subscription_id,processing_status,payload,error_message,processed_at,created_at"
                ),
                "event_id": f"eq.{normalized_event_id}",
                "limit": "1",
            },
        )
        if isinstance(data, list) and data:
            row = data[0]
            if isinstance(row, dict):
                return row
        return None

    def create_stripe_webhook_event(self, fields: Dict[str, Any]) -> Dict[str, Any]:
        event_id = self._normalize_optional_text(fields.get("event_id"))
        event_type = self._normalize_optional_text(fields.get("event_type"))
        if not event_id:
            raise SupabaseStoreError("event_id is required")
        if not event_type:
            raise SupabaseStoreError("event_type is required")
        restaurant_id = fields.get("restaurant_id")
        payload = {
            "event_id": event_id,
            "event_type": event_type,
            "stripe_created_at": fields.get("stripe_created_at"),
            "restaurant_id": self._require_uuid(restaurant_id, "restaurant_id") if restaurant_id else None,
            "stripe_customer_id": self._normalize_optional_text(fields.get("stripe_customer_id")),
            "stripe_subscription_id": self._normalize_optional_text(fields.get("stripe_subscription_id")),
            "processing_status": self._normalize_optional_text(fields.get("processing_status")) or "received",
            "payload": fields.get("payload") or {},
            "error_message": self._normalize_optional_text(fields.get("error_message")),
            "processed_at": fields.get("processed_at"),
        }
        data = self._request(
            "POST",
            "/rest/v1/stripe_webhook_events",
            payload=[payload],
            prefer="return=representation",
        )
        if isinstance(data, list) and data:
            row = data[0]
            if isinstance(row, dict):
                return row
        if isinstance(data, dict):
            return data
        raise SupabaseStoreError("create_stripe_webhook_event did not return row data")

    def update_stripe_webhook_event(self, event_id: str, fields: Dict[str, Any]) -> None:
        normalized_event_id = self._normalize_optional_text(event_id)
        if not normalized_event_id:
            raise SupabaseStoreError("event_id is required")
        payload = dict(fields)
        if "restaurant_id" in payload and payload["restaurant_id"]:
            payload["restaurant_id"] = self._require_uuid(str(payload["restaurant_id"]), "restaurant_id")
        if "event_type" in payload:
            payload["event_type"] = self._normalize_optional_text(payload.get("event_type"))
        if "stripe_customer_id" in payload:
            payload["stripe_customer_id"] = self._normalize_optional_text(payload.get("stripe_customer_id"))
        if "stripe_subscription_id" in payload:
            payload["stripe_subscription_id"] = self._normalize_optional_text(payload.get("stripe_subscription_id"))
        if "processing_status" in payload:
            payload["processing_status"] = self._normalize_optional_text(payload.get("processing_status"))
        if "error_message" in payload:
            payload["error_message"] = self._normalize_optional_text(payload.get("error_message"))
        self._request(
            "PATCH",
            "/rest/v1/stripe_webhook_events",
            payload=payload,
            query={"event_id": f"eq.{normalized_event_id}"},
            prefer="return=minimal",
        )

    def get_restaurant_system_prompt(self, restaurant_id: str) -> Optional[str]:
        rid = self._require_uuid(restaurant_id, "restaurant_id")
        data = self._request(
            "GET",
            "/rest/v1/restaurants",
            query={"select": "system_prompt", "id": f"eq.{rid}", "limit": "1"},
        )
        if isinstance(data, list) and data:
            value = data[0].get("system_prompt")
            if isinstance(value, str):
                text = value.strip()
                return text or None
        return None

    def origin_exists(self, origin: str) -> bool:
        normalized = self._normalize_origin(origin)
        if not normalized:
            return False
        data = self._request(
            "GET",
            "/rest/v1/restaurant_allowed_origins",
            query={"select": "id", "origin": f"eq.{normalized}", "limit": "1"},
        )
        return isinstance(data, list) and len(data) > 0

    def origin_allowed_for_restaurant(self, restaurant_id: str, origin: str) -> bool:
        rid = self._require_uuid(restaurant_id, "restaurant_id")
        normalized = self._normalize_origin(origin)
        if not normalized:
            return False
        data = self._request(
            "GET",
            "/rest/v1/restaurant_allowed_origins",
            query={
                "select": "id",
                "restaurant_id": f"eq.{rid}",
                "origin": f"eq.{normalized}",
                "limit": "1",
            },
        )
        return isinstance(data, list) and len(data) > 0

    def get_restaurant_security_settings(self, restaurant_id: str) -> Dict[str, int]:
        rid = self._require_uuid(restaurant_id, "restaurant_id")
        data = self._request(
            "GET",
            "/rest/v1/restaurant_security_settings",
            query={
                "select": (
                    "ip_max_requests,ip_window_seconds,session_max_requests,session_window_seconds,"
                    "token_max_age_seconds,token_issue_max_requests,token_issue_window_seconds"
                ),
                "restaurant_id": f"eq.{rid}",
                "limit": "1",
            },
        )
        if isinstance(data, list) and data:
            row = data[0]
            return {
                "ip_max_requests": int(row.get("ip_max_requests") or 30),
                "ip_window_seconds": int(row.get("ip_window_seconds") or 60),
                "session_max_requests": int(row.get("session_max_requests") or 45),
                "session_window_seconds": int(row.get("session_window_seconds") or 60),
                "token_max_age_seconds": int(row.get("token_max_age_seconds") or 900),
                "token_issue_max_requests": int(row.get("token_issue_max_requests") or 30),
                "token_issue_window_seconds": int(row.get("token_issue_window_seconds") or 60),
            }
        return {
            "ip_max_requests": 30,
            "ip_window_seconds": 60,
            "session_max_requests": 45,
            "session_window_seconds": 60,
            "token_max_age_seconds": 900,
            "token_issue_max_requests": 30,
            "token_issue_window_seconds": 60,
        }

    def insert_audit_event(
        self,
        event_type: str,
        restaurant_id: Optional[str] = None,
        actor: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        if not event_type or not event_type.strip():
            raise SupabaseStoreError("event_type is required")
        payload = {
            "restaurant_id": self._require_uuid(restaurant_id, "restaurant_id") if restaurant_id else None,
            "event_type": event_type.strip(),
            "actor": (actor or "").strip() or None,
            "details": details or {},
        }
        self._request(
            "POST",
            "/rest/v1/audit_events",
            payload=[payload],
            prefer="return=minimal",
        )

    def upsert_session(
        self,
        restaurant_id: str,
        session_token: str,
        client_meta: Optional[Dict[str, Any]] = None,
        language: Optional[str] = None,
    ) -> Dict[str, Optional[str]]:
        rid = self._require_uuid(restaurant_id, "restaurant_id")
        payload = {
            "p_restaurant_id": rid,
            "p_session_token": session_token,
            "p_client_meta": client_meta or {},
            "p_language": (language or "").strip() or None,
        }
        data = self._request("POST", "/rest/v1/rpc/upsert_session", payload=payload)

        if isinstance(data, list) and data:
            row = data[0]
            sid = row.get("session_id")
            if sid:
                raw_language = row.get("language")
                session_language = str(raw_language).strip() if raw_language is not None else None
                return {
                    "session_id": str(sid),
                    "language": session_language or None,
                }
        if isinstance(data, dict):
            sid = data.get("session_id")
            if sid:
                raw_language = data.get("language")
                session_language = str(raw_language).strip() if raw_language is not None else None
                return {
                    "session_id": str(sid),
                    "language": session_language or None,
                }
        if isinstance(data, str) and data:
            return {
                "session_id": data,
                "language": (language or "").strip() or None,
            }

        raise SupabaseStoreError("upsert_session did not return session_id")

    def insert_message(
        self,
        session_id: str,
        role: str,
        content: str,
        sources: Optional[List[Dict[str, Any]]] = None,
        latency_ms: Optional[int] = None,
        delivery_status: str = "complete",
        query_type: Optional[str] = None,
        return_row: bool = False,
    ) -> Optional[Dict[str, Any]]:
        payload = {
            "session_id": session_id,
            "role": role,
            "content": content,
            "sources": sources if sources is not None else None,
            "latency_ms": latency_ms,
            "delivery_status": delivery_status,
            "query_type": query_type,
        }
        data = self._request(
            "POST",
            "/rest/v1/chat_messages",
            payload=[payload],
            prefer="return=representation" if return_row else "return=minimal",
        )
        if not return_row:
            return None
        if isinstance(data, list) and data:
            row = data[0]
            if isinstance(row, dict):
                return row
        if isinstance(data, dict):
            return data
        raise SupabaseStoreError("insert_message did not return row data")

    def update_message_query_type(self, message_id: str, query_type: str) -> None:
        mid = self._require_uuid(message_id, "message_id")
        qtype = (query_type or "").strip()
        if not qtype:
            raise SupabaseStoreError("query_type is required")
        payload = {"query_type": qtype}
        self._request(
            "PATCH",
            "/rest/v1/chat_messages",
            payload=payload,
            query={"id": f"eq.{mid}"},
            prefer="return=minimal",
        )

    def get_session_state(self, session_id: str) -> Dict[str, Any]:
        sid = self._require_uuid(session_id, "session_id")
        data = self._request(
            "GET",
            "/rest/v1/chat_session_state",
            query={
                "select": "session_id,last_response_id,last_discussed_item_ids,last_candidate_item_ids,last_intent,active_constraints,updated_at",
                "session_id": f"eq.{sid}",
                "limit": "1",
            },
        )
        if isinstance(data, list) and data:
            row = data[0]
            return {
                "session_id": str(row.get("session_id") or sid),
                "last_response_id": row.get("last_response_id"),
                "last_discussed_item_ids": row.get("last_discussed_item_ids") or [],
                "last_candidate_item_ids": row.get("last_candidate_item_ids") or [],
                "last_intent": row.get("last_intent"),
                "active_constraints": row.get("active_constraints") or {},
            }
        return {
            "session_id": sid,
            "last_response_id": None,
            "last_discussed_item_ids": [],
            "last_candidate_item_ids": [],
            "last_intent": None,
            "active_constraints": {},
        }

    def upsert_session_state(self, session_id: str, state_fields: Dict[str, Any]) -> Dict[str, Any]:
        sid = self._require_uuid(session_id, "session_id")
        payload = {
            "session_id": sid,
            "last_response_id": state_fields.get("last_response_id"),
            "last_discussed_item_ids": state_fields.get("last_discussed_item_ids") or [],
            "last_candidate_item_ids": state_fields.get("last_candidate_item_ids") or [],
            "last_intent": state_fields.get("last_intent"),
            "active_constraints": state_fields.get("active_constraints") or {},
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        data = self._request(
            "POST",
            "/rest/v1/chat_session_state",
            payload=[payload],
            query={"on_conflict": "session_id"},
            prefer="resolution=merge-duplicates,return=representation",
        )
        if isinstance(data, list) and data:
            row = data[0]
            if isinstance(row, dict):
                return row
        if isinstance(data, dict):
            return data
        raise SupabaseStoreError("upsert_session_state did not return row data")

    def get_knowledge_chunks_by_ids(self, chunk_ids: List[str]) -> List[Dict[str, Any]]:
        if not chunk_ids:
            return []
        normalized: List[str] = []
        for cid in chunk_ids:
            raw = str(cid or "").strip()
            if raw:
                normalized.append(raw)
        if not normalized:
            return []
        in_expr = "(" + ",".join(f'"{cid}"' for cid in normalized) + ")"
        data = self._request(
            "GET",
            "/rest/v1/knowledge_chunks",
            query={
                "select": "id,title,text,type,source_url,page_path,image_url,extra_metadata",
                "id": f"in.{in_expr}",
                "limit": str(len(normalized)),
            },
        )
        if isinstance(data, list):
            return data
        raise SupabaseStoreError("get_knowledge_chunks_by_ids returned non-list payload")

    def match_chunks(
        self,
        restaurant_id: str,
        query_embedding: List[float],
        match_count: int,
        min_score: float,
    ) -> List[Dict[str, Any]]:
        rid = self._require_uuid(restaurant_id, "restaurant_id")
        payload = {
            "p_restaurant_id": rid,
            "query_embedding": self._vector_literal(query_embedding),
            "match_count": int(match_count),
            "min_score": float(min_score),
        }
        data = self._request("POST", "/rest/v1/rpc/match_chunks", payload=payload)
        if not isinstance(data, list):
            raise SupabaseStoreError("match_chunks returned non-list payload")
        return data

    def begin_ingest_run(self, restaurant_id: str, model: str, source_name: str, total_chunks: int) -> Optional[str]:
        rid = self._require_uuid(restaurant_id, "restaurant_id")
        payload = {
            "restaurant_id": rid,
            "model": model,
            "source_name": source_name,
            "total_chunks": total_chunks,
            "status": "running",
        }
        data = self._request(
            "POST",
            "/rest/v1/ingest_runs",
            payload=[payload],
            prefer="return=representation",
        )
        if isinstance(data, list) and data:
            row = data[0]
            run_id = row.get("id")
            if run_id:
                return str(run_id)
        return None

    def finish_ingest_run(
        self,
        run_id: Optional[str],
        status: str,
        embedded_chunks: int,
        error_message: Optional[str] = None,
    ) -> None:
        if not run_id:
            return
        payload = {
            "status": status,
            "embedded_chunks": embedded_chunks,
            "error_message": error_message,
        }
        self._request(
            "PATCH",
            "/rest/v1/ingest_runs",
            payload=payload,
            query={"id": f"eq.{run_id}"},
            prefer="return=minimal",
        )

    def upsert_knowledge_chunks(self, rows: List[Dict[str, Any]]) -> None:
        if not rows:
            return
        for row in rows:
            self._require_uuid(str(row.get("restaurant_id") or ""), "restaurant_id")
        self._request(
            "POST",
            "/rest/v1/knowledge_chunks",
            payload=rows,
            query={"on_conflict": "id"},
            prefer="resolution=merge-duplicates,return=minimal",
        )
