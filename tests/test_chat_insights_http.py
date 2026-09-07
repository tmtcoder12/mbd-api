import io
import importlib.util
import json
import pathlib
import sys
import types
import unittest


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
    spec = importlib.util.spec_from_file_location("rag_chatbot_insights_http_test_module", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load rag-chatbot.py for tests")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _FakeStore:
    def __init__(self, active_subscription, membership_role="owner", token_valid=True):
        self.active_subscription = active_subscription
        self.membership_role = membership_role
        self.token_valid = token_valid

    def restaurant_exists(self, restaurant_id):
        return restaurant_id == TEST_RESTAURANT_ID

    def restaurant_has_active_subscription(self, restaurant_id):
        return self.active_subscription

    def get_restaurant_security_settings(self, restaurant_id):
        return {}

    def origin_allowed_for_restaurant(self, restaurant_id, origin):
        return True

    def origin_exists(self, origin):
        return True

    def insert_audit_event(self, event_type, restaurant_id=None, actor=None, details=None):
        return None

    def get_authenticated_user(self, access_token):
        if not self.token_valid or access_token != "valid-token":
            raise self._error("bad token")
        return {"id": "22222222-2222-4222-8222-222222222222", "email": "owner@example.com"}

    def get_restaurant_user_membership(self, restaurant_id, user_id, allowed_roles=None):
        if restaurant_id != TEST_RESTAURANT_ID:
            return None
        if self.membership_role is None:
            return None
        if allowed_roles and self.membership_role not in allowed_roles:
            return None
        return {
            "id": "33333333-3333-4333-8333-333333333333",
            "restaurant_id": restaurant_id,
            "user_id": user_id,
            "role": self.membership_role,
        }

    @staticmethod
    def _error(message):
        import supabase_store

        return supabase_store.SupabaseStoreError(message)


class _FakeInsightsService:
    def __init__(self):
        self.calls = []

    def generate(self, restaurant_id, window_days=None):
        self.calls.append({"restaurant_id": restaurant_id, "window_days": window_days})
        if isinstance(window_days, str) and not window_days.isdigit():
            raise ValueError("days must be an integer")
        return {
            "restaurantId": restaurant_id,
            "windowDays": int(window_days) if window_days is not None else 30,
            "coverage": {"sessionsAnalyzed": 2, "sessionLimitReached": False},
            "aggregateMetrics": {"successful_session_count": 2, "failed_session_count": 0},
            "summary": {"executive_summary": "Two sessions analyzed."},
            "sessionExtractions": [],
        }


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


class ChatInsightsHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_module()

    def _configure_handler(self, store, insights_service):
        mod = self.mod
        mod.ChatHandler.client = object()
        mod.ChatHandler.retriever = object()
        mod.ChatHandler.chat_insights_service = insights_service
        mod.ChatHandler.top_k = 8
        mod.ChatHandler.min_score = 0.0
        mod.ChatHandler.store = store
        mod.ChatHandler.persist_chat = False
        mod.ChatHandler.allow_localhost_origins = False
        mod.ChatHandler.rate_limiter = None
        mod.ChatHandler.redis_rate_limiter = None
        mod.ChatHandler.query_cache = None
        mod.ChatHandler.widget_signing_keys = {"v1": b"secret"}
        mod.ChatHandler.widget_active_kid = "v1"
        mod.ChatHandler.stripe_secret_key = "sk_test"
        mod.ChatHandler.stripe_webhook_secret = "whsec_test"
        return mod.ChatHandler

    def _request(self, store, insights_service, body, auth_token="valid-token"):
        handler_cls = self._configure_handler(store, insights_service)
        encoded_body = body.encode("utf-8")
        header_lines = [
            "POST /api/chat-insights HTTP/1.1",
            "Host: localhost",
            f"Content-Length: {len(encoded_body)}",
            "Origin: https://owner.example",
            "Content-Type: application/json",
        ]
        if auth_token is not None:
            header_lines.append(f"Authorization: Bearer {auth_token}")
        raw_request = ("\r\n".join(header_lines) + "\r\n\r\n").encode("utf-8") + encoded_body
        fake_socket = _FakeSocket(raw_request)
        handler_cls(fake_socket, ("127.0.0.1", 12345), _FakeServer())
        raw_response = fake_socket._wfile.getvalue().decode("utf-8")
        header_blob, _, payload = raw_response.partition("\r\n\r\n")
        status_line = header_blob.splitlines()[0]
        status_code = int(status_line.split(" ")[1])
        return status_code, payload

    def test_chat_insights_endpoint_returns_summary_payload(self):
        service = _FakeInsightsService()
        status, payload = self._request(
            _FakeStore(active_subscription=True),
            service,
            json.dumps({"restaurantId": TEST_RESTAURANT_ID, "days": 14}),
        )

        body = json.loads(payload)
        self.assertEqual(status, 200)
        self.assertEqual(body["summary"]["executive_summary"], "Two sessions analyzed.")
        self.assertEqual(service.calls[0]["restaurant_id"], TEST_RESTAURANT_ID)
        self.assertEqual(service.calls[0]["window_days"], 14)

    def test_chat_insights_endpoint_rejects_invalid_days(self):
        service = _FakeInsightsService()
        status, payload = self._request(
            _FakeStore(active_subscription=True),
            service,
            json.dumps({"restaurantId": TEST_RESTAURANT_ID, "days": "thirty"}),
        )

        body = json.loads(payload)
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "days must be an integer")

    def test_chat_insights_blocked_when_subscription_inactive(self):
        service = _FakeInsightsService()
        status, payload = self._request(
            _FakeStore(active_subscription=False),
            service,
            json.dumps({"restaurantId": TEST_RESTAURANT_ID, "days": 30}),
        )

        body = json.loads(payload)
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "Restaurant subscription is inactive")
        self.assertEqual(service.calls, [])

    def test_chat_insights_requires_authorization_token(self):
        service = _FakeInsightsService()
        status, payload = self._request(
            _FakeStore(active_subscription=True),
            service,
            json.dumps({"restaurantId": TEST_RESTAURANT_ID, "days": 30}),
            auth_token=None,
        )

        body = json.loads(payload)
        self.assertEqual(status, 401)
        self.assertEqual(body["error"], "Authorization bearer token is required")
        self.assertEqual(service.calls, [])

    def test_chat_insights_rejects_non_manager_membership(self):
        service = _FakeInsightsService()
        status, payload = self._request(
            _FakeStore(active_subscription=True, membership_role="staff"),
            service,
            json.dumps({"restaurantId": TEST_RESTAURANT_ID, "days": 30}),
        )

        body = json.loads(payload)
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "User is not authorized for this restaurant")
        self.assertEqual(service.calls, [])


if __name__ == "__main__":
    unittest.main()
