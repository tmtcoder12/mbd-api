"""Stripe webhook processing and subscription normalization helpers."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from datetime import datetime, timezone
from typing import Any, Dict, Optional
import uuid

from supabase_store import SupabaseStore, SupabaseStoreError

try:
    import stripe
except ModuleNotFoundError:
    stripe = None


SUPPORTED_STRIPE_SUBSCRIPTION_STATUSES = frozenset(
    {
        "active",
        "trialing",
        "past_due",
        "canceled",
        "unpaid",
        "incomplete",
        "incomplete_expired",
        "paused",
    }
)
SUPPORTED_STRIPE_EVENT_TYPES = frozenset(
    {
        "checkout.session.completed",
        "customer.subscription.created",
        "customer.subscription.updated",
        "customer.subscription.deleted",
    }
)


class StripeWebhookError(RuntimeError):
    """Base Stripe webhook error."""


class StripeWebhookSignatureError(StripeWebhookError):
    """Raised when the incoming webhook cannot be verified."""


class StripeConfigError(StripeWebhookError):
    """Raised when Stripe runtime configuration is missing."""


class StripeSdkError(StripeWebhookError):
    """Raised when the Stripe SDK is not installed or unsupported."""


class PermanentStripeWebhookError(StripeWebhookError):
    """Raised for permanent, non-retryable webhook failures."""


class TransientStripeWebhookError(StripeWebhookError):
    """Raised for transient Stripe or Supabase failures that should be retried."""


@dataclass(frozen=True)
class StripeWebhookResult:
    status_code: int
    event_id: Optional[str]
    event_type: Optional[str]
    restaurant_id: Optional[str]
    message: str
    processing_status: str
    duplicate: bool = False
    ok: bool = True


def ensure_stripe_sdk_available() -> None:
    if stripe is None:
        raise StripeSdkError("stripe package is required for Stripe billing integration")


def ensure_stripe_configured(stripe_secret_key: str, stripe_webhook_secret: str) -> None:
    if not (stripe_secret_key or "").strip():
        raise StripeConfigError("STRIPE_SECRET_KEY is required")
    if not (stripe_webhook_secret or "").strip():
        raise StripeConfigError("STRIPE_WEBHOOK_SECRET is required")
    ensure_stripe_sdk_available()


def is_subscription_active_status(status: Optional[str]) -> bool:
    normalized = _coerce_text(status)
    return normalized == "active"


def verify_stripe_webhook(payload: bytes, signature_header: str, webhook_secret: str) -> Dict[str, Any]:
    ensure_stripe_sdk_available()
    secret = (webhook_secret or "").strip()
    if not secret:
        raise StripeConfigError("STRIPE_WEBHOOK_SECRET is required")
    sig_header = (signature_header or "").strip()
    if not sig_header:
        raise StripeWebhookSignatureError("Missing Stripe-Signature header")
    try:
        event = stripe.Webhook.construct_event(payload=payload, sig_header=sig_header, secret=secret)
    except Exception as exc:  # noqa: BLE001
        raise StripeWebhookSignatureError(f"Invalid Stripe webhook signature: {exc}") from exc
    normalized_event = _to_plain_dict(event)
    if not isinstance(normalized_event, dict):
        raise StripeWebhookSignatureError("Stripe webhook did not produce a valid event payload")
    return normalized_event


def fetch_stripe_subscription(subscription_id: str, stripe_secret_key: str) -> Dict[str, Any]:
    ensure_stripe_sdk_available()
    secret = (stripe_secret_key or "").strip()
    if not secret:
        raise StripeConfigError("STRIPE_SECRET_KEY is required")
    sub_id = _coerce_text(subscription_id)
    if not sub_id:
        raise PermanentStripeWebhookError("Stripe subscription ID is required")
    try:
        if hasattr(stripe, "Subscription") and hasattr(stripe.Subscription, "retrieve"):
            subscription = stripe.Subscription.retrieve(sub_id, api_key=secret)
        elif hasattr(stripe, "StripeClient"):
            client = stripe.StripeClient(secret)
            subscription = client.v1.subscriptions.retrieve(sub_id)
        else:
            raise StripeSdkError("Unsupported stripe SDK version for subscription retrieval")
    except StripeWebhookError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise TransientStripeWebhookError(f"Failed to retrieve Stripe subscription: {exc}") from exc
    normalized = _to_plain_dict(subscription)
    if not isinstance(normalized, dict):
        raise TransientStripeWebhookError("Stripe subscription retrieval returned an invalid payload")
    return normalized


def normalize_subscription_snapshot(
    subscription: Dict[str, Any],
    *,
    restaurant_id: str,
    stripe_customer_id: Optional[str] = None,
    stripe_checkout_session_id: Optional[str] = None,
    stripe_payment_link_id: Optional[str] = None,
    client_reference_id: Optional[str] = None,
    last_checkout_completed_at: Optional[str] = None,
    existing_snapshot: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    restaurant_uuid = _require_uuid_text(restaurant_id, "restaurant_id")
    if not isinstance(subscription, dict):
        raise PermanentStripeWebhookError("Stripe subscription payload must be a JSON object")

    status = _coerce_text(subscription.get("status"))
    if not status:
        raise PermanentStripeWebhookError("Stripe subscription status is required")
    if status not in SUPPORTED_STRIPE_SUBSCRIPTION_STATUSES:
        raise PermanentStripeWebhookError(f"Unsupported Stripe subscription status: {status}")

    subscription_id = _extract_id(subscription)
    if not subscription_id:
        raise PermanentStripeWebhookError("Stripe subscription object is missing an id")

    items = subscription.get("items") or {}
    item_rows = items.get("data") if isinstance(items, dict) else None
    first_item = item_rows[0] if isinstance(item_rows, list) and item_rows else {}
    price = first_item.get("price") if isinstance(first_item, dict) else None
    if not isinstance(price, dict):
        price = {}
    product = price.get("product")
    if isinstance(product, dict):
        product_id = _coerce_text(product.get("id"))
    else:
        product_id = _coerce_text(product)

    current_period_start = _first_present_timestamp(
        subscription.get("current_period_start"),
        _subscription_items_period_timestamp(subscription, "current_period_start", prefer="min"),
        _snapshot_field(existing_snapshot, "current_period_start"),
    )
    current_period_end = _first_present_timestamp(
        subscription.get("current_period_end"),
        _subscription_items_period_timestamp(subscription, "current_period_end", prefer="max"),
        _snapshot_field(existing_snapshot, "current_period_end"),
    )
    cancel_at = _first_present_timestamp(
        subscription.get("cancel_at"),
        current_period_end if bool(subscription.get("cancel_at_period_end")) else None,
        _snapshot_field(existing_snapshot, "cancel_at"),
    )
    canceled_at = _first_present_timestamp(
        subscription.get("canceled_at"),
        _snapshot_field(existing_snapshot, "canceled_at"),
    )
    ended_at = _first_present_timestamp(
        subscription.get("ended_at"),
        _snapshot_field(existing_snapshot, "ended_at"),
    )

    snapshot = {
        "restaurant_id": restaurant_uuid,
        "stripe_customer_id": _coerce_identifier(subscription.get("customer")) or _coerce_text(stripe_customer_id),
        "stripe_subscription_id": subscription_id,
        "stripe_payment_link_id": _coerce_identifier(stripe_payment_link_id),
        "stripe_checkout_session_id": _coerce_identifier(stripe_checkout_session_id),
        "client_reference_id": _coerce_text(client_reference_id),
        "stripe_price_id": _coerce_text(price.get("id")),
        "stripe_product_id": product_id,
        "stripe_subscription_status": status,
        "current_period_start": current_period_start,
        "current_period_end": current_period_end,
        "cancel_at": cancel_at,
        "canceled_at": canceled_at,
        "ended_at": ended_at,
        "last_checkout_completed_at": _first_present_timestamp(
            last_checkout_completed_at,
            _snapshot_field(existing_snapshot, "last_checkout_completed_at"),
        ),
        "last_synced_at": _utcnow_iso(),
    }
    return snapshot


def process_stripe_webhook(
    *,
    raw_body: bytes,
    signature_header: str,
    webhook_secret: str,
    stripe_secret_key: str,
    store: SupabaseStore,
) -> StripeWebhookResult:
    if store is None:
        raise StripeConfigError("Supabase store is required for Stripe webhook processing")

    event = verify_stripe_webhook(raw_body, signature_header, webhook_secret)
    event_id = _coerce_text(event.get("id"))
    event_type = _coerce_text(event.get("type"))
    if not event_id or not event_type:
        raise PermanentStripeWebhookError("Stripe event payload is missing id or type")

    identifiers = _extract_event_identifiers(event)
    existing = store.get_stripe_webhook_event(event_id)
    if existing and _coerce_text(existing.get("processing_status")) == "processed":
        return StripeWebhookResult(
            status_code=200,
            event_id=event_id,
            event_type=event_type,
            restaurant_id=_coerce_text(existing.get("restaurant_id")),
            message="Stripe webhook event already processed",
            processing_status="processed",
            duplicate=True,
        )

    event_row = {
        "event_id": event_id,
        "event_type": event_type,
        "stripe_created_at": _timestamp_to_iso(event.get("created")),
        "restaurant_id": identifiers.get("restaurant_id"),
        "stripe_customer_id": identifiers.get("stripe_customer_id"),
        "stripe_subscription_id": identifiers.get("stripe_subscription_id"),
        "processing_status": "received",
        "payload": _to_json_safe(event),
        "error_message": None,
        "processed_at": None,
    }
    if existing is None:
        try:
            store.create_stripe_webhook_event(event_row)
        except SupabaseStoreError:
            existing = store.get_stripe_webhook_event(event_id)
            if existing and _coerce_text(existing.get("processing_status")) == "processed":
                return StripeWebhookResult(
                    status_code=200,
                    event_id=event_id,
                    event_type=event_type,
                    restaurant_id=_coerce_text(existing.get("restaurant_id")),
                    message="Stripe webhook event already processed",
                    processing_status="processed",
                    duplicate=True,
                )
            if existing is None:
                raise
    else:
        store.update_stripe_webhook_event(event_id, event_row)

    if event_type not in SUPPORTED_STRIPE_EVENT_TYPES:
        store.update_stripe_webhook_event(
            event_id,
            {
                "processing_status": "processed",
                "processed_at": _utcnow_iso(),
                "error_message": None,
            },
        )
        return StripeWebhookResult(
            status_code=200,
            event_id=event_id,
            event_type=event_type,
            restaurant_id=identifiers.get("restaurant_id"),
            message="Stripe webhook event type ignored",
            processing_status="processed",
        )

    try:
        if event_type == "checkout.session.completed":
            handled = _handle_checkout_session_completed(event, stripe_secret_key, store)
        else:
            handled = _handle_subscription_event(event_type, event, store)
        store.update_stripe_webhook_event(
            event_id,
            {
                "restaurant_id": handled.get("restaurant_id"),
                "stripe_customer_id": handled.get("stripe_customer_id"),
                "stripe_subscription_id": handled.get("stripe_subscription_id"),
                "processing_status": "processed",
                "processed_at": _utcnow_iso(),
                "error_message": None,
            },
        )
        return StripeWebhookResult(
            status_code=200,
            event_id=event_id,
            event_type=event_type,
            restaurant_id=handled.get("restaurant_id"),
            message=handled.get("message") or "Stripe webhook processed",
            processing_status="processed",
        )
    except PermanentStripeWebhookError as exc:
        store.update_stripe_webhook_event(
            event_id,
            {
                "restaurant_id": identifiers.get("restaurant_id"),
                "stripe_customer_id": identifiers.get("stripe_customer_id"),
                "stripe_subscription_id": identifiers.get("stripe_subscription_id"),
                "processing_status": "failed",
                "error_message": str(exc),
            },
        )
        return StripeWebhookResult(
            status_code=200,
            event_id=event_id,
            event_type=event_type,
            restaurant_id=identifiers.get("restaurant_id"),
            message=str(exc),
            processing_status="failed",
            ok=False,
        )
    except (SupabaseStoreError, TransientStripeWebhookError, StripeConfigError, StripeSdkError) as exc:
        store.update_stripe_webhook_event(
            event_id,
            {
                "restaurant_id": identifiers.get("restaurant_id"),
                "stripe_customer_id": identifiers.get("stripe_customer_id"),
                "stripe_subscription_id": identifiers.get("stripe_subscription_id"),
                "processing_status": "failed",
                "error_message": str(exc),
            },
        )
        return StripeWebhookResult(
            status_code=500,
            event_id=event_id,
            event_type=event_type,
            restaurant_id=identifiers.get("restaurant_id"),
            message=str(exc),
            processing_status="failed",
            ok=False,
        )


def _handle_checkout_session_completed(
    event: Dict[str, Any],
    stripe_secret_key: str,
    store: SupabaseStore,
) -> Dict[str, Optional[str]]:
    session = _event_object(event)
    session_id = _extract_id(session)
    if not session_id:
        raise PermanentStripeWebhookError("Stripe checkout session is missing an id")

    client_reference_id = _coerce_text(session.get("client_reference_id"))
    if not client_reference_id:
        raise PermanentStripeWebhookError("Stripe checkout session is missing client_reference_id")
    restaurant_id = _require_uuid_text(client_reference_id, "client_reference_id")
    if not store.restaurant_exists(restaurant_id):
        raise PermanentStripeWebhookError("Stripe checkout references an unknown restaurant")

    subscription_id = _coerce_identifier(session.get("subscription"))
    if not subscription_id:
        raise PermanentStripeWebhookError("Stripe checkout session is missing subscription")

    payment_link_id = _coerce_identifier(session.get("payment_link"))
    customer_id = _coerce_identifier(session.get("customer"))
    subscription = fetch_stripe_subscription(subscription_id, stripe_secret_key)
    snapshot = normalize_subscription_snapshot(
        subscription,
        restaurant_id=restaurant_id,
        stripe_customer_id=customer_id,
        stripe_checkout_session_id=session_id,
        stripe_payment_link_id=payment_link_id,
        client_reference_id=client_reference_id,
        last_checkout_completed_at=_timestamp_to_iso(session.get("created")) or _utcnow_iso(),
        existing_snapshot=store.get_restaurant_subscription_by_subscription_id(subscription_id),
    )
    row = store.upsert_restaurant_subscription(restaurant_id, snapshot)
    return {
        "restaurant_id": restaurant_id,
        "stripe_customer_id": _coerce_text(row.get("stripe_customer_id")) or customer_id,
        "stripe_subscription_id": _coerce_text(row.get("stripe_subscription_id")) or subscription_id,
        "message": "Stripe checkout session processed",
    }


def _handle_subscription_event(
    event_type: str,
    event: Dict[str, Any],
    store: SupabaseStore,
) -> Dict[str, Optional[str]]:
    subscription = _event_object(event)
    subscription_id = _extract_id(subscription)
    if not subscription_id:
        raise PermanentStripeWebhookError("Stripe subscription event is missing a subscription id")
    customer_id = _coerce_identifier(subscription.get("customer"))

    existing = store.get_restaurant_subscription_by_subscription_id(subscription_id)
    if existing is None and customer_id:
        existing = store.get_restaurant_subscription_by_customer_id(customer_id)
    if existing is None:
        raise PermanentStripeWebhookError("No restaurant subscription mapping found for Stripe subscription event")

    restaurant_id = _coerce_text(existing.get("restaurant_id"))
    if not restaurant_id:
        raise PermanentStripeWebhookError("Restaurant subscription mapping is missing restaurant_id")

    normalized_subscription = dict(subscription)
    if event_type == "customer.subscription.deleted" and not _coerce_text(normalized_subscription.get("status")):
        normalized_subscription["status"] = "canceled"

    snapshot = normalize_subscription_snapshot(
        normalized_subscription,
        restaurant_id=restaurant_id,
        stripe_customer_id=customer_id or _coerce_text(existing.get("stripe_customer_id")),
        stripe_checkout_session_id=_coerce_text(existing.get("stripe_checkout_session_id")),
        stripe_payment_link_id=_coerce_text(existing.get("stripe_payment_link_id")),
        client_reference_id=_coerce_text(existing.get("client_reference_id")),
        last_checkout_completed_at=_coerce_text(existing.get("last_checkout_completed_at")),
        existing_snapshot=existing,
    )
    row = store.upsert_restaurant_subscription(restaurant_id, snapshot)
    return {
        "restaurant_id": restaurant_id,
        "stripe_customer_id": _coerce_text(row.get("stripe_customer_id")) or customer_id,
        "stripe_subscription_id": _coerce_text(row.get("stripe_subscription_id")) or subscription_id,
        "message": "Stripe subscription event processed",
    }


def _event_object(event: Dict[str, Any]) -> Dict[str, Any]:
    data = event.get("data") or {}
    if not isinstance(data, dict):
        raise PermanentStripeWebhookError("Stripe event data is invalid")
    obj = data.get("object")
    if not isinstance(obj, dict):
        normalized = _to_plain_dict(obj)
        if not isinstance(normalized, dict):
            raise PermanentStripeWebhookError("Stripe event object is invalid")
        return normalized
    return obj


def _extract_event_identifiers(event: Dict[str, Any]) -> Dict[str, Optional[str]]:
    event_type = _coerce_text(event.get("type"))
    try:
        obj = _event_object(event)
    except PermanentStripeWebhookError:
        return {
            "restaurant_id": None,
            "stripe_customer_id": None,
            "stripe_subscription_id": None,
        }

    if event_type == "checkout.session.completed":
        client_reference_id = _coerce_text(obj.get("client_reference_id"))
        restaurant_id = None
        if client_reference_id:
            try:
                restaurant_id = _require_uuid_text(client_reference_id, "client_reference_id")
            except PermanentStripeWebhookError:
                restaurant_id = None
        return {
            "restaurant_id": restaurant_id,
            "stripe_customer_id": _coerce_identifier(obj.get("customer")),
            "stripe_subscription_id": _coerce_identifier(obj.get("subscription")),
        }

    return {
        "restaurant_id": None,
        "stripe_customer_id": _coerce_identifier(obj.get("customer")),
        "stripe_subscription_id": _extract_id(obj),
    }


def _require_uuid_text(value: Any, field_name: str) -> str:
    text = _coerce_text(value)
    if not text:
        raise PermanentStripeWebhookError(f"{field_name} is required")
    try:
        return str(uuid.UUID(text))
    except ValueError as exc:
        raise PermanentStripeWebhookError(f"{field_name} must be a valid UUID") from exc


def _timestamp_to_iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    if isinstance(value, Decimal):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
        except Exception:  # noqa: BLE001
            return str(value)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
    text = _coerce_text(value)
    return text or None


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _coerce_identifier(value: Any) -> Optional[str]:
    if isinstance(value, dict):
        return _coerce_text(value.get("id"))
    return _coerce_text(value)


def _extract_id(value: Any) -> Optional[str]:
    if isinstance(value, dict):
        return _coerce_text(value.get("id"))
    return _coerce_text(value)


def _coerce_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _to_plain_dict(value: Any) -> Any:
    if value is None:
        return None
    if hasattr(value, "to_dict_recursive"):
        return _to_plain_dict(value.to_dict_recursive())
    if hasattr(value, "to_dict"):
        try:
            return _to_plain_dict(value.to_dict())
        except Exception:  # noqa: BLE001
            pass
    if isinstance(value, Mapping):
        return {str(k): _to_plain_dict(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_to_plain_dict(item) for item in value]
    if isinstance(value, tuple):
        return [_to_plain_dict(item) for item in value]
    try:
        as_dict = dict(value)
    except Exception:  # noqa: BLE001
        as_dict = None
    if isinstance(as_dict, dict):
        return {str(k): _to_plain_dict(v) for k, v in as_dict.items()}
    if hasattr(value, "__dict__"):
        public_fields = {
            str(k): _to_plain_dict(v)
            for k, v in vars(value).items()
            if not str(k).startswith("_")
        }
        if public_fields:
            return public_fields
    return value


def _to_json_safe(value: Any) -> Any:
    normalized = _to_plain_dict(value)
    if isinstance(normalized, datetime):
        return normalized.astimezone(timezone.utc).isoformat() if normalized.tzinfo else normalized.replace(tzinfo=timezone.utc).isoformat()
    if isinstance(normalized, Decimal):
        return str(normalized)
    if isinstance(normalized, Mapping):
        return {str(k): _to_json_safe(v) for k, v in normalized.items()}
    if isinstance(normalized, list):
        return [_to_json_safe(item) for item in normalized]
    if isinstance(normalized, tuple):
        return [_to_json_safe(item) for item in normalized]
    if isinstance(normalized, set):
        return [_to_json_safe(item) for item in normalized]
    return normalized


def subscription_allows_api_access(
    *,
    status: Optional[str],
    current_period_end: Optional[str],
    ended_at: Optional[str],
    now: Optional[datetime] = None,
) -> bool:
    normalized_status = _coerce_text(status)
    if normalized_status == "active":
        return True

    reference = now or datetime.now(timezone.utc)
    period_end_dt = _parse_timestamp(current_period_end)
    ended_at_dt = _parse_timestamp(ended_at)
    if period_end_dt is None or period_end_dt <= reference:
        return False
    if ended_at_dt is not None and ended_at_dt <= reference:
        return False
    return True


def _subscription_items_period_timestamp(subscription: Dict[str, Any], field_name: str, prefer: str) -> Optional[str]:
    items = subscription.get("items") or {}
    item_rows = items.get("data") if isinstance(items, dict) else None
    if not isinstance(item_rows, list):
        return None

    timestamps = []
    for item in item_rows:
        if not isinstance(item, dict):
            continue
        candidate = _parse_timestamp(item.get(field_name))
        if candidate is not None:
            timestamps.append(candidate)
    if not timestamps:
        return None
    selected = min(timestamps) if prefer == "min" else max(timestamps)
    return selected.astimezone(timezone.utc).isoformat()


def _first_present_timestamp(*values: Any) -> Optional[str]:
    for value in values:
        normalized = _timestamp_to_iso(value)
        if normalized:
            return normalized
    return None


def _snapshot_field(snapshot: Optional[Dict[str, Any]], field_name: str) -> Any:
    if isinstance(snapshot, dict):
        return snapshot.get(field_name)
    return None


def _parse_timestamp(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    if isinstance(value, Decimal):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except Exception:  # noqa: BLE001
            return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    return None
