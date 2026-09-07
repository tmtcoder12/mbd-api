import io
import importlib.util
import json
import pathlib
import sys
import threading
import types
import unittest
import uuid


TEST_RESTAURANT_ID = "11111111-1111-4111-8111-111111111111"


def _load_module():
    if "numpy" not in sys.modules:
        try:
            import numpy as real_np
            sys.modules["numpy"] = real_np
        except ModuleNotFoundError:
            fake_np = types.ModuleType("numpy")
            fake_np.array = lambda v, dtype=None: v
            fake_np.float32 = float
            fake_np.clip = lambda a, b, c: a
            fake_np.dot = lambda a, b: sum(float(x) * float(y) for x, y in zip(a, b))
            fake_np.linalg = types.SimpleNamespace(
                norm=lambda v, axis=None, keepdims=None: (sum(float(x) * float(x) for x in v) ** 0.5)
            )
            sys.modules["numpy"] = fake_np
    if "dotenv" not in sys.modules:
        fake_dotenv = types.ModuleType("dotenv")
        fake_dotenv.load_dotenv = lambda *args, **kwargs: None
        sys.modules["dotenv"] = fake_dotenv
    if "openai" not in sys.modules:
        fake_openai = types.ModuleType("openai")
        fake_openai.OpenAI = object
        sys.modules["openai"] = fake_openai
    if "stripe" not in sys.modules:
        fake_stripe = types.ModuleType("stripe")
        fake_stripe.Webhook = types.SimpleNamespace(construct_event=lambda *args, **kwargs: {})
        fake_stripe.Subscription = types.SimpleNamespace(retrieve=lambda *args, **kwargs: {})
        sys.modules["stripe"] = fake_stripe

    root = pathlib.Path(__file__).resolve().parents[1]
    path = root / "rag-chatbot.py"
    spec = importlib.util.spec_from_file_location("rag_chatbot_http_test_module", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load rag-chatbot.py for tests")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _FakeStore:
    def __init__(self, active_subscription, origin_allowed=True):
        self.active_subscription = active_subscription
        self.origin_allowed = origin_allowed
        self.audit_events = []
        self.chat_access_calls = 0
        self.origin_exists_calls = 0
        self.session_bootstrap_calls = 0
        self.bootstrap_session_tokens = []
        self.inserted_messages = []
        self.updated_query_types = []

    def restaurant_exists(self, restaurant_id):
        return restaurant_id == TEST_RESTAURANT_ID

    def restaurant_has_active_subscription(self, restaurant_id):
        return self.active_subscription

    def get_restaurant_security_settings(self, restaurant_id):
        return {}

    def origin_allowed_for_restaurant(self, restaurant_id, origin):
        return self.origin_allowed

    def origin_exists(self, origin):
        self.origin_exists_calls += 1
        return True

    def chat_access_context(self, restaurant_id, origin, origin_preallowed=False):
        self.chat_access_calls += 1
        return {
            "restaurant_exists": restaurant_id == TEST_RESTAURANT_ID,
            "subscription_active": self.active_subscription,
            "origin_allowed": bool(origin_preallowed or self.origin_allowed),
            "ip_max_requests": 30,
            "ip_window_seconds": 60,
            "session_max_requests": 45,
            "session_window_seconds": 60,
            "token_max_age_seconds": 900,
            "token_issue_max_requests": 30,
            "token_issue_window_seconds": 60,
            "system_prompt": "Prompt {language}",
        }

    def chat_session_bootstrap(self, restaurant_id, session_token, client_meta=None, language=None):
        self.session_bootstrap_calls += 1
        self.bootstrap_session_tokens.append(session_token)
        return {
            "session_id": "22222222-2222-4222-8222-222222222222",
            "language": language or "eng",
            "session_state": {
                "session_id": "22222222-2222-4222-8222-222222222222",
                "last_response_id": None,
                "last_discussed_item_ids": [],
                "last_candidate_item_ids": [],
                "last_intent": None,
                "active_constraints": {},
            },
        }

    def insert_message(
        self,
        session_id,
        role,
        content,
        sources=None,
        latency_ms=None,
        delivery_status="complete",
        query_type=None,
        return_row=False,
    ):
        row = {
            "id": f"33333333-3333-4333-8333-33333333333{len(self.inserted_messages)}",
            "session_id": session_id,
            "role": role,
            "content": content,
            "sources": sources,
            "latency_ms": latency_ms,
            "delivery_status": delivery_status,
            "query_type": query_type,
        }
        self.inserted_messages.append(row)
        return row if return_row else None

    def update_message_query_type(self, message_id, query_type):
        self.updated_query_types.append((message_id, query_type))

    def upsert_session_state(self, session_id, state_fields):
        return {"session_id": session_id, **dict(state_fields or {})}

    def insert_audit_event(self, event_type, restaurant_id=None, actor=None, details=None):
        self.audit_events.append(
            {
                "event_type": event_type,
                "restaurant_id": restaurant_id,
                "actor": actor,
                "details": details or {},
            }
        )


class _FakeSocket:
    def __init__(self, request_bytes):
        self._rfile = io.BytesIO(request_bytes)
        self._wfile = io.BytesIO()

    def makefile(self, mode, *args, **kwargs):
        if "r" in mode:
            return self._rfile
        return self._wfile

    def sendall(self, data):
        self._wfile.write(data)

    def close(self):
        pass


class _FakeServer:
    pass


class ChatHandlerStripeHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_module()

    def _configure_handler(self, store, persist_chat=False):
        mod = self.mod
        mod.ChatHandler.client = object()
        mod.ChatHandler.retriever = object()
        mod.ChatHandler.top_k = 8
        mod.ChatHandler.min_score = 0.0
        mod.ChatHandler.store = store
        mod.ChatHandler.persist_chat = persist_chat
        mod.ChatHandler.allow_localhost_origins = False
        mod.ChatHandler.rate_limiter = None
        mod.ChatHandler.redis_rate_limiter = None
        mod.ChatHandler.query_cache = None
        mod.ChatHandler.query_classifier_model = mod.QUERY_CLASSIFIER_MODEL_DEFAULT
        mod.ChatHandler.widget_signing_keys = {"v1": b"secret"}
        mod.ChatHandler.widget_active_kid = "v1"
        mod.ChatHandler.stripe_secret_key = "sk_test"
        mod.ChatHandler.stripe_webhook_secret = "whsec_test"
        return mod.ChatHandler

    def _request(self, store, method, path, body, headers, persist_chat=False, include_headers=False):
        handler_cls = self._configure_handler(store, persist_chat=persist_chat)
        encoded_body = body.encode("utf-8") if isinstance(body, str) else body
        header_lines = [
            f"{method} {path} HTTP/1.1",
            "Host: localhost",
            f"Content-Length: {len(encoded_body)}",
        ]
        for key, value in headers.items():
            header_lines.append(f"{key}: {value}")
        raw_request = ("\r\n".join(header_lines) + "\r\n\r\n").encode("utf-8") + encoded_body
        fake_socket = _FakeSocket(raw_request)
        handler_cls(fake_socket, ("127.0.0.1", 12345), _FakeServer())
        raw_response = fake_socket._wfile.getvalue().decode("utf-8")
        header_blob, _, payload = raw_response.partition("\r\n\r\n")
        status_line = header_blob.splitlines()[0]
        status_code = int(status_line.split(" ")[1])
        if include_headers:
            return status_code, payload, header_blob
        return status_code, payload

    def test_widget_token_blocked_when_subscription_inactive(self):
        status, payload = self._request(
            _FakeStore(active_subscription=False),
            "POST",
            "/api/widget-token",
            json.dumps({"restaurantId": TEST_RESTAURANT_ID}),
            {
                "Origin": "https://restaurant.example",
                "Content-Type": "application/json",
            },
        )
        body = json.loads(payload)
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "Restaurant subscription is inactive")

    def test_widget_token_preflight_does_not_require_origin_db_lookup(self):
        store = _FakeStore(active_subscription=True, origin_allowed=False)
        status, payload, headers = self._request(
            store,
            "OPTIONS",
            "/api/widget-token",
            b"",
            {
                "Origin": "https://restaurant.example",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "Content-Type, X-Widget-Client",
            },
            include_headers=True,
        )

        self.assertEqual(status, 204)
        self.assertEqual(payload, "")
        self.assertEqual(store.origin_exists_calls, 0)
        self.assertIn("Access-Control-Allow-Origin: https://restaurant.example", headers)
        self.assertIn("Access-Control-Allow-Headers: Content-Type, X-Widget-Client", headers)
        self.assertIn("Access-Control-Max-Age: 600", headers)
        self.assertIn("Content-Length: 0", headers)

    def test_chat_stream_blocked_when_subscription_inactive(self):
        store = _FakeStore(active_subscription=False)
        status, payload = self._request(
            store,
            "POST",
            "/api/chat-stream",
            json.dumps(
                {
                    "restaurantId": TEST_RESTAURANT_ID,
                    "message": "hello",
                    "widgetToken": "bad-token",
                }
            ),
            {
                "Origin": "https://restaurant.example",
                "Content-Type": "application/json",
            },
        )
        body = json.loads(payload)
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "Restaurant subscription is inactive")
        self.assertEqual(store.session_bootstrap_calls, 0)

    def test_active_subscription_proceeds_past_subscription_gate(self):
        store = _FakeStore(active_subscription=True)
        status, payload = self._request(
            store,
            "POST",
            "/api/chat-stream",
            json.dumps(
                {
                    "restaurantId": TEST_RESTAURANT_ID,
                    "message": "hello",
                    "widgetToken": "bad-token",
                }
            ),
            {
                "Origin": "https://restaurant.example",
                "Content-Type": "application/json",
            },
        )
        body = json.loads(payload)
        self.assertEqual(status, 403)
        self.assertIn("widgetToken", body["error"])
        self.assertNotEqual(body["error"], "Restaurant subscription is inactive")
        self.assertEqual(store.session_bootstrap_calls, 0)

    def test_chat_stream_blocked_when_origin_not_allowed(self):
        store = _FakeStore(active_subscription=True, origin_allowed=False)
        status, payload = self._request(
            store,
            "POST",
            "/api/chat-stream",
            json.dumps(
                {
                    "restaurantId": TEST_RESTAURANT_ID,
                    "message": "hello",
                    "widgetToken": "bad-token",
                }
            ),
            {
                "Origin": "https://restaurant.example",
                "Content-Type": "application/json",
            },
        )
        body = json.loads(payload)
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "Origin is not allowed for this restaurant")
        self.assertEqual(store.session_bootstrap_calls, 0)

    def test_chat_stream_reuses_supplied_session_token_without_new_session_flag(self):
        mod = self.mod
        origin = "https://restaurant.example"
        supplied_session_token = "44444444-4444-4444-8444-444444444444"
        token, _ = mod.build_widget_token(TEST_RESTAURANT_ID, origin, "v1", b"secret", 900)
        store = _FakeStore(active_subscription=True)
        original_handle_chat_turn = mod.handle_chat_turn
        try:
            mod.handle_chat_turn = lambda **kwargs: {
                "assistant_text": "We close at 10 PM.",
                "results": [],
                "retrieval_query": kwargs["user_query"],
                "resolved_reference": {"status": "none"},
                "intent": None,
                "active_constraints": {},
                "response_id": "resp-1",
                "image_decision": {"include_images": False, "max_images": 0, "target_item_names": []},
                "new_session_state": kwargs["session_state"],
                "fallback_reason": None,
                "cache_hit": False,
            }
            status, payload = self._request(
                store,
                "POST",
                "/api/chat-stream",
                json.dumps(
                    {
                        "restaurantId": TEST_RESTAURANT_ID,
                        "message": "When do you close?",
                        "sessionToken": supplied_session_token,
                        "widgetToken": token,
                    }
                ),
                {
                    "Origin": origin,
                    "Content-Type": "application/json",
                },
            )
        finally:
            mod.handle_chat_turn = original_handle_chat_turn

        self.assertEqual(status, 200)
        self.assertEqual(store.bootstrap_session_tokens, [supplied_session_token])
        self.assertIn(f'"sessionToken": "{supplied_session_token}"', payload)
        self.assertIn('"generated": false', payload)

    def test_chat_stream_new_session_flag_forces_fresh_session_token(self):
        mod = self.mod
        origin = "https://restaurant.example"
        supplied_session_token = "44444444-4444-4444-8444-444444444444"
        token, _ = mod.build_widget_token(TEST_RESTAURANT_ID, origin, "v1", b"secret", 900)
        store = _FakeStore(active_subscription=True)
        original_handle_chat_turn = mod.handle_chat_turn
        try:
            mod.handle_chat_turn = lambda **kwargs: {
                "assistant_text": "We close at 10 PM.",
                "results": [],
                "retrieval_query": kwargs["user_query"],
                "resolved_reference": {"status": "none"},
                "intent": None,
                "active_constraints": {},
                "response_id": "resp-1",
                "image_decision": {"include_images": False, "max_images": 0, "target_item_names": []},
                "new_session_state": kwargs["session_state"],
                "fallback_reason": None,
                "cache_hit": False,
            }
            status, payload = self._request(
                store,
                "POST",
                "/api/chat-stream",
                json.dumps(
                    {
                        "restaurantId": TEST_RESTAURANT_ID,
                        "message": "When do you close?",
                        "sessionToken": supplied_session_token,
                        "newSession": True,
                        "widgetToken": token,
                    }
                ),
                {
                    "Origin": origin,
                    "Content-Type": "application/json",
                },
            )
        finally:
            mod.handle_chat_turn = original_handle_chat_turn

        self.assertEqual(status, 200)
        self.assertEqual(len(store.bootstrap_session_tokens), 1)
        self.assertNotEqual(store.bootstrap_session_tokens[0], supplied_session_token)
        uuid.UUID(store.bootstrap_session_tokens[0])
        self.assertIn(f'"sessionToken": "{store.bootstrap_session_tokens[0]}"', payload)
        self.assertIn('"generated": true', payload)

    def test_async_user_persistence_waits_for_stream_close_before_classification(self):
        mod = self.mod
        origin = "https://restaurant.example"
        token, _ = mod.build_widget_token(TEST_RESTAURANT_ID, origin, "v1", b"secret", 900)

        class _SlowUserInsertStore(_FakeStore):
            def __init__(self):
                super().__init__(active_subscription=True)
                self.user_insert_started = threading.Event()
                self.release_user_insert = threading.Event()
                self.query_type_updated = threading.Event()

            def insert_message(self, *args, **kwargs):
                role = args[1]
                if role == "user":
                    self.user_insert_started.set()
                    self.release_user_insert.wait(1.0)
                return super().insert_message(*args, **kwargs)

            def update_message_query_type(self, message_id, query_type):
                super().update_message_query_type(message_id, query_type)
                self.query_type_updated.set()

        store = _SlowUserInsertStore()
        original_handle_chat_turn = mod.handle_chat_turn
        original_classify_query_type = mod.classify_query_type
        try:
            mod.handle_chat_turn = lambda **kwargs: {
                "assistant_text": "We close at 10 PM.",
                "results": [],
                "retrieval_query": kwargs["user_query"],
                "resolved_reference": {"status": "none"},
                "intent": None,
                "active_constraints": {},
                "response_id": "resp-1",
                "image_decision": {"include_images": False, "max_images": 0, "target_item_names": []},
                "new_session_state": kwargs["session_state"],
                "fallback_reason": None,
                "cache_hit": False,
            }
            mod.classify_query_type = lambda *args, **kwargs: (
                "Menu",
                "Menu",
                {"response_id": "classify-1", "latency_ms": 1, "raw_output_len": 4},
            )
            status, payload = self._request(
                store,
                "POST",
                "/api/chat-stream",
                json.dumps(
                    {
                        "restaurantId": TEST_RESTAURANT_ID,
                        "message": "When do you close?",
                        "widgetToken": token,
                    }
                ),
                {
                    "Origin": origin,
                    "Content-Type": "application/json",
                },
                persist_chat=True,
            )

            self.assertEqual(status, 200)
            self.assertIn('"type": "done"', payload)
            self.assertTrue(store.user_insert_started.wait(1.0))
            self.assertEqual(store.updated_query_types, [])

            store.release_user_insert.set()
            self.assertTrue(store.query_type_updated.wait(1.0))
            self.assertEqual(store.updated_query_types[0][1], "Menu")
        finally:
            store.release_user_insert.set()
            mod.handle_chat_turn = original_handle_chat_turn
            mod.classify_query_type = original_classify_query_type

    def test_webhook_route_accepts_supported_event(self):
        store = _FakeStore(active_subscription=True)
        original = self.mod.process_stripe_webhook
        self.mod.process_stripe_webhook = lambda **kwargs: types.SimpleNamespace(
            status_code=200,
            event_id="evt_1",
            event_type="checkout.session.completed",
            restaurant_id=TEST_RESTAURANT_ID,
            message="processed",
            processing_status="processed",
            duplicate=False,
            ok=True,
        )
        try:
            status, payload = self._request(
                store,
                "POST",
                "/api/stripe/webhook",
                json.dumps({"id": "evt_1", "type": "checkout.session.completed"}),
                {
                    "Stripe-Signature": "sig",
                    "Content-Type": "application/json",
                },
            )
        finally:
            self.mod.process_stripe_webhook = original

        body = json.loads(payload)
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["eventId"], "evt_1")

    def test_webhook_permanent_failure_returns_200(self):
        original = self.mod.process_stripe_webhook
        self.mod.process_stripe_webhook = lambda **kwargs: types.SimpleNamespace(
            status_code=200,
            event_id="evt_perm",
            event_type="checkout.session.completed",
            restaurant_id=TEST_RESTAURANT_ID,
            message="unknown restaurant",
            processing_status="failed",
            duplicate=False,
            ok=False,
        )
        try:
            status, payload = self._request(
                _FakeStore(active_subscription=True),
                "POST",
                "/api/stripe/webhook",
                json.dumps({"id": "evt_perm"}),
                {
                    "Stripe-Signature": "sig",
                    "Content-Type": "application/json",
                },
            )
        finally:
            self.mod.process_stripe_webhook = original

        body = json.loads(payload)
        self.assertEqual(status, 200)
        self.assertFalse(body["ok"])

    def test_webhook_transient_failure_returns_500(self):
        original = self.mod.process_stripe_webhook
        self.mod.process_stripe_webhook = lambda **kwargs: types.SimpleNamespace(
            status_code=500,
            event_id="evt_retry",
            event_type="checkout.session.completed",
            restaurant_id=TEST_RESTAURANT_ID,
            message="temporary store outage",
            processing_status="failed",
            duplicate=False,
            ok=False,
        )
        try:
            status, payload = self._request(
                _FakeStore(active_subscription=True),
                "POST",
                "/api/stripe/webhook",
                json.dumps({"id": "evt_retry"}),
                {
                    "Stripe-Signature": "sig",
                    "Content-Type": "application/json",
                },
            )
        finally:
            self.mod.process_stripe_webhook = original

        body = json.loads(payload)
        self.assertEqual(status, 500)
        self.assertFalse(body["ok"])


if __name__ == "__main__":
    unittest.main()
