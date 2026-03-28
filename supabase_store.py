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
                "select": "ip_max_requests,ip_window_seconds,session_max_requests,session_window_seconds,token_max_age_seconds",
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
    ) -> str:
        rid = self._require_uuid(restaurant_id, "restaurant_id")
        payload = {
            "p_restaurant_id": rid,
            "p_session_token": session_token,
            "p_client_meta": client_meta or {},
        }
        data = self._request("POST", "/rest/v1/rpc/upsert_session", payload=payload)

        if isinstance(data, list) and data:
            row = data[0]
            sid = row.get("session_id")
            if sid:
                return str(sid)
        if isinstance(data, dict):
            sid = data.get("session_id")
            if sid:
                return str(sid)
        if isinstance(data, str) and data:
            return data

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
        language: Optional[str] = None,
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
            "language": language,
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

    def update_message_classification(
        self,
        message_id: str,
        query_type: str,
        language: Optional[str] = None,
    ) -> None:
        mid = self._require_uuid(message_id, "message_id")
        qtype = (query_type or "").strip()
        if not qtype:
            raise SupabaseStoreError("query_type is required")
        payload = {"query_type": qtype}
        lang = (language or "").strip()
        if lang:
            payload["language"] = lang
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
