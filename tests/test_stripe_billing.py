import importlib.util
import json
import pathlib
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal


TEST_RESTAURANT_ID = "11111111-1111-4111-8111-111111111111"


def _install_fake_stripe():
    fake_stripe = types.ModuleType("stripe")

    class _Webhook:
        @staticmethod
        def construct_event(payload, sig_header, secret):
            if sig_header != "good-signature":
                raise ValueError("bad signature")
            return json.loads(payload.decode("utf-8"))

    class _Subscription:
        responses = {}

        @classmethod
        def retrieve(cls, subscription_id, api_key=None):
            if subscription_id not in cls.responses:
                raise RuntimeError(f"unknown subscription: {subscription_id}")
            return cls.responses[subscription_id]

    fake_stripe.Webhook = _Webhook
    fake_stripe.Subscription = _Subscription
    sys.modules["stripe"] = fake_stripe
    return fake_stripe


def _load_module():
    _install_fake_stripe()
    root = pathlib.Path(__file__).resolve().parents[1]
    path = root / "stripe_billing.py"
    spec = importlib.util.spec_from_file_location("stripe_billing_test_module", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load stripe_billing.py for tests")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _FakeStore:
    def __init__(self):
        self.restaurants = {TEST_RESTAURANT_ID}
        self.events = {}
        self.subscriptions_by_restaurant = {}
        self.subscriptions_by_subscription_id = {}
        self.subscriptions_by_customer_id = {}
        self.upsert_calls = 0

    def restaurant_exists(self, restaurant_id):
        return restaurant_id in self.restaurants

    def get_stripe_webhook_event(self, event_id):
        row = self.events.get(event_id)
        return dict(row) if row is not None else None

    def create_stripe_webhook_event(self, fields):
        row = dict(fields)
        self.events[row["event_id"]] = row
        return dict(row)

    def update_stripe_webhook_event(self, event_id, fields):
        row = self.events.setdefault(event_id, {"event_id": event_id})
        row.update(fields)

    def upsert_restaurant_subscription(self, restaurant_id, fields):
        self.upsert_calls += 1
        row = dict(fields)
        row["restaurant_id"] = restaurant_id
        self.subscriptions_by_restaurant[restaurant_id] = row
        subscription_id = row.get("stripe_subscription_id")
        customer_id = row.get("stripe_customer_id")
        if subscription_id:
            self.subscriptions_by_subscription_id[subscription_id] = row
        if customer_id:
            self.subscriptions_by_customer_id[customer_id] = row
        return dict(row)

    def get_restaurant_subscription_by_subscription_id(self, stripe_subscription_id):
        row = self.subscriptions_by_subscription_id.get(stripe_subscription_id)
        return dict(row) if row is not None else None

    def get_restaurant_subscription_by_customer_id(self, stripe_customer_id):
        row = self.subscriptions_by_customer_id.get(stripe_customer_id)
        return dict(row) if row is not None else None


class StripeBillingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_module()

    def test_verify_signature_accepts_valid_event(self):
        event = self.mod.verify_stripe_webhook(
            b'{"id":"evt_1","type":"checkout.session.completed","data":{"object":{}}}',
            "good-signature",
            "whsec_test",
        )
        self.assertEqual(event["id"], "evt_1")

    def test_verify_signature_accepts_mapping_like_event_object(self):
        class _StripeLikeEvent:
            def __iter__(self):
                return iter(
                    {
                        "id": "evt_obj",
                        "type": "checkout.session.completed",
                        "data": {"object": {}},
                    }.items()
                )

        original = self.mod.stripe.Webhook.construct_event
        self.mod.stripe.Webhook.construct_event = staticmethod(lambda payload, sig_header, secret: _StripeLikeEvent())
        try:
            event = self.mod.verify_stripe_webhook(b"{}", "good-signature", "whsec_test")
        finally:
            self.mod.stripe.Webhook.construct_event = original

        self.assertEqual(event["id"], "evt_obj")

    def test_verify_signature_rejects_invalid_event(self):
        with self.assertRaises(self.mod.StripeWebhookSignatureError):
            self.mod.verify_stripe_webhook(
                b'{"id":"evt_1","type":"checkout.session.completed","data":{"object":{}}}',
                "bad-signature",
                "whsec_test",
            )

    def test_normalize_subscription_snapshot_extracts_fields(self):
        snapshot = self.mod.normalize_subscription_snapshot(
            {
                "id": "sub_123",
                "status": "active",
                "customer": "cus_123",
                "current_period_start": 1700000000,
                "current_period_end": 1700003600,
                "cancel_at": None,
                "canceled_at": None,
                "ended_at": None,
                "items": {
                    "data": [
                        {
                            "price": {
                                "id": "price_123",
                                "product": "prod_123",
                            }
                        }
                    ]
                },
            },
            restaurant_id=TEST_RESTAURANT_ID,
            stripe_checkout_session_id="cs_123",
            stripe_payment_link_id="plink_123",
            client_reference_id=TEST_RESTAURANT_ID,
            last_checkout_completed_at="2026-04-02T12:00:00+00:00",
        )
        self.assertEqual(snapshot["stripe_subscription_status"], "active")
        self.assertEqual(snapshot["stripe_customer_id"], "cus_123")
        self.assertEqual(snapshot["stripe_checkout_session_id"], "cs_123")
        self.assertEqual(snapshot["stripe_payment_link_id"], "plink_123")
        self.assertEqual(snapshot["stripe_price_id"], "price_123")
        self.assertEqual(snapshot["stripe_product_id"], "prod_123")
        self.assertEqual(snapshot["client_reference_id"], TEST_RESTAURANT_ID)
        self.assertTrue(snapshot["current_period_start"].startswith("2023-11-14"))

    def test_normalize_subscription_snapshot_falls_back_to_item_periods(self):
        snapshot = self.mod.normalize_subscription_snapshot(
            {
                "id": "sub_items",
                "status": "active",
                "customer": "cus_items",
                "items": {
                    "data": [
                        {
                            "current_period_start": 1700000000,
                            "current_period_end": 1700003600,
                            "price": {"id": "price_a", "product": "prod_a"},
                        },
                        {
                            "current_period_start": 1700000100,
                            "current_period_end": 1700007200,
                            "price": {"id": "price_b", "product": "prod_b"},
                        },
                    ]
                },
            },
            restaurant_id=TEST_RESTAURANT_ID,
        )
        self.assertTrue(snapshot["current_period_start"].startswith("2023-11-14"))
        self.assertTrue(snapshot["current_period_end"].startswith("2023-11-15"))

    def test_normalize_subscription_snapshot_uses_paid_through_date_for_cancel_at_period_end(self):
        snapshot = self.mod.normalize_subscription_snapshot(
            {
                "id": "sub_cancel",
                "status": "active",
                "customer": "cus_cancel",
                "cancel_at_period_end": True,
                "canceled_at": 1700000200,
                "items": {
                    "data": [
                        {
                            "current_period_start": 1700000000,
                            "current_period_end": 1700003600,
                            "price": {"id": "price_cancel", "product": "prod_cancel"},
                        }
                    ]
                },
            },
            restaurant_id=TEST_RESTAURANT_ID,
        )
        self.assertEqual(snapshot["cancel_at"], snapshot["current_period_end"])
        self.assertTrue(snapshot["canceled_at"].startswith("2023-11-14"))

    def test_normalize_subscription_snapshot_preserves_existing_periods_when_event_omits_them(self):
        snapshot = self.mod.normalize_subscription_snapshot(
            {
                "id": "sub_existing",
                "status": "canceled",
                "customer": "cus_existing",
                "ended_at": 1700004000,
                "items": {"data": []},
            },
            restaurant_id=TEST_RESTAURANT_ID,
            existing_snapshot={
                "current_period_start": "2026-04-01T00:00:00+00:00",
                "current_period_end": "2026-05-01T00:00:00+00:00",
                "cancel_at": "2026-05-01T00:00:00+00:00",
                "last_checkout_completed_at": "2026-04-01T00:00:00+00:00",
            },
        )
        self.assertEqual(snapshot["current_period_start"], "2026-04-01T00:00:00+00:00")
        self.assertEqual(snapshot["current_period_end"], "2026-05-01T00:00:00+00:00")
        self.assertEqual(snapshot["cancel_at"], "2026-05-01T00:00:00+00:00")
        self.assertTrue(snapshot["ended_at"].startswith("2023-11-14"))

    def test_checkout_session_webhook_upserts_subscription(self):
        store = _FakeStore()
        self.mod.stripe.Subscription.responses["sub_123"] = {
            "id": "sub_123",
            "status": "active",
            "customer": "cus_123",
            "current_period_start": 1700000000,
            "current_period_end": 1700003600,
            "items": {
                "data": [
                    {
                        "price": {
                            "id": "price_123",
                            "product": "prod_123",
                        }
                    }
                ]
            },
        }
        event = {
            "id": "evt_123",
            "type": "checkout.session.completed",
            "created": 1700000100,
            "data": {
                "object": {
                    "id": "cs_123",
                    "client_reference_id": TEST_RESTAURANT_ID,
                    "subscription": "sub_123",
                    "customer": "cus_123",
                    "payment_link": "plink_123",
                    "created": 1700000105,
                }
            },
        }
        result = self.mod.process_stripe_webhook(
            raw_body=json.dumps(event).encode("utf-8"),
            signature_header="good-signature",
            webhook_secret="whsec_test",
            stripe_secret_key="sk_test",
            store=store,
        )
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.processing_status, "processed")
        self.assertEqual(store.upsert_calls, 1)
        row = store.subscriptions_by_restaurant[TEST_RESTAURANT_ID]
        self.assertEqual(row["stripe_subscription_id"], "sub_123")
        self.assertEqual(row["stripe_customer_id"], "cus_123")
        self.assertEqual(row["stripe_payment_link_id"], "plink_123")
        self.assertEqual(row["stripe_subscription_status"], "active")
        self.assertEqual(store.events["evt_123"]["processing_status"], "processed")

    def test_subscription_updated_cancel_at_period_end_keeps_paid_through_access_fields(self):
        store = _FakeStore()
        store.upsert_restaurant_subscription(
            TEST_RESTAURANT_ID,
            {
                "stripe_customer_id": "cus_cancel",
                "stripe_subscription_id": "sub_cancel",
                "stripe_subscription_status": "active",
                "current_period_start": "2026-04-01T00:00:00+00:00",
                "current_period_end": "2026-05-01T00:00:00+00:00",
                "cancel_at": None,
                "canceled_at": None,
                "ended_at": None,
                "last_checkout_completed_at": "2026-04-01T00:00:00+00:00",
            },
        )
        event = {
            "id": "evt_cancel_update",
            "type": "customer.subscription.updated",
            "created": 1700000300,
            "data": {
                "object": {
                    "id": "sub_cancel",
                    "status": "active",
                    "customer": "cus_cancel",
                    "cancel_at_period_end": True,
                    "canceled_at": 1700000200,
                    "items": {
                        "data": [
                            {
                                "current_period_start": 1711929600,
                                "current_period_end": 1714521600,
                                "price": {"id": "price_cancel", "product": "prod_cancel"},
                            }
                        ]
                    },
                }
            },
        }
        result = self.mod.process_stripe_webhook(
            raw_body=json.dumps(event).encode("utf-8"),
            signature_header="good-signature",
            webhook_secret="whsec_test",
            stripe_secret_key="sk_test",
            store=store,
        )
        self.assertEqual(result.status_code, 200)
        row = store.subscriptions_by_restaurant[TEST_RESTAURANT_ID]
        self.assertEqual(row["stripe_subscription_status"], "active")
        self.assertIsNotNone(row["current_period_end"])
        self.assertEqual(row["cancel_at"], row["current_period_end"])
        self.assertIsNotNone(row["canceled_at"])

    def test_webhook_payload_is_sanitized_for_decimal_values(self):
        store = _FakeStore()

        class _DecimalEvent:
            def __iter__(self):
                return iter(
                    {
                        "id": "evt_decimal",
                        "type": "customer.created",
                        "created": 1700000000,
                        "data": {
                            "object": {
                                "id": "cus_decimal",
                                "balance": Decimal("12.34"),
                            }
                        },
                    }.items()
                )

        original = self.mod.stripe.Webhook.construct_event
        self.mod.stripe.Webhook.construct_event = staticmethod(lambda payload, sig_header, secret: _DecimalEvent())
        try:
            result = self.mod.process_stripe_webhook(
                raw_body=b"{}",
                signature_header="good-signature",
                webhook_secret="whsec_test",
                stripe_secret_key="sk_test",
                store=store,
            )
        finally:
            self.mod.stripe.Webhook.construct_event = original

        self.assertEqual(result.status_code, 200)
        self.assertEqual(store.events["evt_decimal"]["payload"]["data"]["object"]["balance"], "12.34")

    def test_subscription_allows_access_when_canceled_but_paid_through_date_is_future(self):
        future_end = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
        allowed = self.mod.subscription_allows_api_access(
            status="canceled",
            current_period_end=future_end,
            ended_at=None,
        )
        self.assertTrue(allowed)

    def test_subscription_denies_access_when_paid_through_date_has_passed(self):
        past_end = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        allowed = self.mod.subscription_allows_api_access(
            status="canceled",
            current_period_end=past_end,
            ended_at=None,
        )
        self.assertFalse(allowed)

    def test_subscription_denies_access_when_ended_at_has_passed(self):
        future_end = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
        past_ended = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        allowed = self.mod.subscription_allows_api_access(
            status="canceled",
            current_period_end=future_end,
            ended_at=past_ended,
        )
        self.assertFalse(allowed)

    def test_subscription_allows_access_when_active_even_without_period_end(self):
        allowed = self.mod.subscription_allows_api_access(
            status="active",
            current_period_end=None,
            ended_at=None,
        )
        self.assertTrue(allowed)

    def test_duplicate_processed_webhook_short_circuits(self):
        store = _FakeStore()
        store.events["evt_dup"] = {
            "event_id": "evt_dup",
            "event_type": "checkout.session.completed",
            "processing_status": "processed",
            "restaurant_id": TEST_RESTAURANT_ID,
        }
        event = {
            "id": "evt_dup",
            "type": "checkout.session.completed",
            "data": {"object": {}},
        }
        result = self.mod.process_stripe_webhook(
            raw_body=json.dumps(event).encode("utf-8"),
            signature_header="good-signature",
            webhook_secret="whsec_test",
            stripe_secret_key="sk_test",
            store=store,
        )
        self.assertEqual(result.status_code, 200)
        self.assertTrue(result.duplicate)
        self.assertEqual(store.upsert_calls, 0)


if __name__ == "__main__":
    unittest.main()
