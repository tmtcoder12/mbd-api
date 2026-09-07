import json
import sys
import types
import unittest

try:
    import requests
except ModuleNotFoundError:
    fake_requests = types.ModuleType("requests")

    class _FakeRequestException(Exception):
        pass

    class _FakeHTTPError(_FakeRequestException):
        def __init__(self, *args, response=None, **kwargs):
            super().__init__(*args)
            self.response = response

    class _FakeSessionFactory:
        def __init__(self):
            self.headers = {}

    fake_requests.RequestException = _FakeRequestException
    fake_requests.HTTPError = _FakeHTTPError
    fake_requests.Session = _FakeSessionFactory
    sys.modules["requests"] = fake_requests
    requests = fake_requests

from supabase_store import SupabaseStore, SupabaseStoreError

TEST_RESTAURANT_ID = "11111111-1111-4111-8111-111111111111"
TEST_SESSION_ID = "22222222-2222-4222-8222-222222222222"


class _FakeResponse:
    def __init__(self, status_code=200, payload=None, text=None, content_type="application/json"):
        self.status_code = status_code
        self.text = text if text is not None else json.dumps(payload)
        self.headers = {"Content-Type": content_type}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(response=self)


class _FakeSession:
    def __init__(self, response):
        self.response = response
        self.calls = []
        self.headers = {}

    def request(self, method, url, data=None, headers=None, timeout=None):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "data": json.loads(data.decode("utf-8")) if isinstance(data, bytes) else data,
                "headers": headers,
                "timeout": timeout,
            }
        )
        return self.response


class _FailingSession:
    def __init__(self, exc):
        self.exc = exc
        self.calls = []
        self.headers = {}

    def request(self, method, url, data=None, headers=None, timeout=None):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "data": json.loads(data.decode("utf-8")) if isinstance(data, bytes) else data,
                "headers": headers,
                "timeout": timeout,
            }
        )
        raise self.exc


class SupabaseStoreBootstrapTests(unittest.TestCase):
    def test_chat_access_context_posts_expected_payload_and_parses_row(self):
        store = SupabaseStore("https://example.supabase.co", "service-key", timeout_s=12.5)
        session = _FakeSession(
            _FakeResponse(
                payload=[
                    {
                        "restaurant_exists": True,
                        "subscription_active": True,
                        "origin_allowed": True,
                        "ip_max_requests": 10,
                        "ip_window_seconds": 20,
                        "session_max_requests": 30,
                        "session_window_seconds": 40,
                        "token_max_age_seconds": 500,
                        "token_issue_max_requests": 60,
                        "token_issue_window_seconds": 70,
                        "system_prompt": "Prompt {language}",
                    }
                ]
            )
        )
        store.session = session

        result = store.chat_access_context(TEST_RESTAURANT_ID, "https://Restaurant.Example/", origin_preallowed=True)

        self.assertEqual(session.calls[0]["method"], "POST")
        self.assertEqual(session.calls[0]["url"], "https://example.supabase.co/rest/v1/rpc/chat_access_context")
        self.assertEqual(
            session.calls[0]["data"],
            {
                "p_restaurant_id": TEST_RESTAURANT_ID,
                "p_origin": "https://restaurant.example",
                "p_origin_preallowed": True,
            },
        )
        self.assertTrue(result["restaurant_exists"])
        self.assertTrue(result["subscription_active"])
        self.assertEqual(result["ip_max_requests"], 10)
        self.assertEqual(result["system_prompt"], "Prompt {language}")

    def test_chat_session_bootstrap_posts_expected_payload_and_parses_state(self):
        store = SupabaseStore("https://example.supabase.co", "service-key")
        session = _FakeSession(
            _FakeResponse(
                payload={
                    "session_id": TEST_SESSION_ID,
                    "language": "spa",
                    "last_response_id": "resp-prev",
                    "last_discussed_item_ids": ["chunk-1"],
                    "last_candidate_item_ids": ["chunk-2"],
                    "last_intent": "ingredients",
                    "active_constraints": {"category": "pizza"},
                }
            )
        )
        store.session = session

        result = store.chat_session_bootstrap(
            TEST_RESTAURANT_ID,
            "33333333-3333-4333-8333-333333333333",
            {"user_agent": "tests"},
            language="spa",
        )

        self.assertEqual(session.calls[0]["url"], "https://example.supabase.co/rest/v1/rpc/chat_session_bootstrap")
        self.assertEqual(session.calls[0]["data"]["p_language"], "spa")
        self.assertEqual(result["session_id"], TEST_SESSION_ID)
        self.assertEqual(result["language"], "spa")
        self.assertEqual(result["session_state"]["last_response_id"], "resp-prev")
        self.assertEqual(result["session_state"]["active_constraints"], {"category": "pizza"})

    def test_chat_access_context_converts_http_errors(self):
        store = SupabaseStore("https://example.supabase.co", "service-key")
        store.session = _FakeSession(_FakeResponse(status_code=404, text="missing rpc"))

        with self.assertRaisesRegex(
            SupabaseStoreError,
            r"HTTP 404 POST /rest/v1/rpc/chat_access_context: missing rpc",
        ):
            store.chat_access_context(TEST_RESTAURANT_ID, "https://restaurant.example")

    def test_chat_access_context_retries_transient_network_error_once(self):
        store = SupabaseStore("https://example.supabase.co", "service-key")
        first_session = _FailingSession(requests.RequestException("('Connection aborted.',)"))
        retry_session = _FakeSession(
            _FakeResponse(
                payload=[
                    {
                        "restaurant_exists": True,
                        "subscription_active": True,
                        "origin_allowed": True,
                        "system_prompt": "Prompt {language}",
                    }
                ]
            )
        )
        store.session = first_session
        store._build_session = lambda: retry_session

        result = store.chat_access_context(TEST_RESTAURANT_ID, "https://restaurant.example")

        self.assertTrue(result["restaurant_exists"])
        self.assertEqual(len(first_session.calls), 1)
        self.assertEqual(len(retry_session.calls), 1)
        self.assertEqual(retry_session.calls[0]["url"], "https://example.supabase.co/rest/v1/rpc/chat_access_context")

    def test_non_retryable_post_network_errors_are_not_retried(self):
        store = SupabaseStore("https://example.supabase.co", "service-key")
        first_session = _FailingSession(requests.RequestException("boom"))
        retry_session = _FakeSession(_FakeResponse(payload={"ok": True}))
        store.session = first_session
        store._build_session = lambda: retry_session

        with self.assertRaisesRegex(SupabaseStoreError, "Network error calling Supabase: boom"):
            store._request("POST", "/rest/v1/chat_messages", payload={"role": "user"})

        self.assertEqual(len(first_session.calls), 1)
        self.assertEqual(len(retry_session.calls), 0)


if __name__ == "__main__":
    unittest.main()
