import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mbd_api.app import create_app
from mbd_api.config import Settings
from mbd_api.service import ApplicationServices, ServiceError

RESTAURANT_ID = "11111111-1111-4111-8111-555555555555"


def make_settings(**overrides: Any) -> Settings:
    values = {
        "app_env": "test",
        "openai_api_key": "test-openai-key",
        "supabase_url": "http://supabase.test",
        "supabase_service_role_key": "test-service-key",
        "widget_signing_keys": "v1:test-signing-key",
        "json_logs": False,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


class FakeServices:
    def __init__(self) -> None:
        self.chat_calls: list[tuple[Any, dict[str, Any]]] = []
        self.token_calls: list[tuple[str, str, str]] = []

    def close(self) -> None:
        pass

    def ready(self) -> dict[str, Any]:
        return {"ok": True, "supabase": "ok", "redis": "disabled"}

    def issue_widget_token(self, restaurant_id: str, origin: str, client_ip: str) -> dict[str, Any]:
        self.token_calls.append((restaurant_id, origin, client_ip))
        return {"widgetToken": "signed", "expiresAt": 123}

    def stream_chat(self, payload: Any, **kwargs: Any):
        self.chat_calls.append((payload, kwargs))
        yield b'{"type":"session","sessionToken":"22222222-2222-4222-8222-222222222222"}\n'
        yield b'{"type":"delta","content":"Hello"}\n'
        yield b'{"type":"done"}\n'

    def handle_stripe_webhook(self, body: bytes, signature: str) -> dict[str, Any]:
        return {"ok": True, "message": "processed", "eventId": "evt_1", "eventType": "test"}


@pytest.fixture
def services() -> FakeServices:
    return FakeServices()


@pytest.fixture
def client(services: FakeServices):
    with TestClient(create_app(make_settings(), services)) as test_client:
        yield test_client


def test_health_and_readiness(client: TestClient) -> None:
    assert client.get("/healthz").json() == {"ok": True}
    ready = client.get("/readyz")
    assert ready.status_code == 200
    assert ready.json() == {"ok": True, "supabase": "ok", "redis": "disabled"}


def test_readiness_returns_503_when_supabase_is_unavailable() -> None:
    services = FakeServices()
    services.ready = lambda: {"ok": False, "supabase": "unavailable", "redis": "disabled"}
    with TestClient(create_app(make_settings(), services)) as client:
        assert client.get("/readyz").status_code == 503


def test_widget_token_normalizes_origin_and_does_not_trust_forwarded_ip_by_default(
    client: TestClient, services: FakeServices
) -> None:
    response = client.post(
        "/api/widget-token",
        json={"restaurantId": RESTAURANT_ID},
        headers={"Origin": "HTTPS://Restaurant.Example/", "X-Forwarded-For": "203.0.113.9"},
    )
    assert response.status_code == 200
    restaurant_id, origin, client_ip = services.token_calls[0]
    assert restaurant_id == RESTAURANT_ID
    assert origin == "https://restaurant.example"
    assert client_ip == "testclient"
    assert response.headers["x-request-id"]


def test_forwarded_ip_is_used_only_when_enabled() -> None:
    services = FakeServices()
    with TestClient(create_app(make_settings(trust_proxy_headers=True), services)) as client:
        client.post(
            "/api/widget-token",
            json={"restaurantId": RESTAURANT_ID},
            headers={"Origin": "https://restaurant.example", "X-Forwarded-For": "203.0.113.9, 10.0.0.1"},
        )
    assert services.token_calls[0][2] == "203.0.113.9"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"restaurantId": "not-a-uuid"},
        {"restaurantId": RESTAURANT_ID, "language": "deu"},
    ],
)
def test_widget_validation_is_safe_and_backward_compatible(client: TestClient, payload: dict[str, Any]) -> None:
    response = client.post("/api/widget-token", json=payload, headers={"Origin": "https://restaurant.example"})
    body = response.json()
    assert response.status_code == 400
    assert body["error"] == "Invalid request"
    assert body["requestId"]


def test_request_size_limit_applies_even_without_content_length(services: FakeServices) -> None:
    with TestClient(create_app(make_settings(max_request_bytes=1024), services)) as client:
        response = client.post(
            "/api/widget-token",
            content=json.dumps({"restaurantId": RESTAURANT_ID, "padding": "x" * 2000}),
            headers={"Origin": "https://restaurant.example", "Content-Type": "application/json"},
        )
    assert response.status_code == 413
    assert response.json()["error"] == "Request body is too large"


def test_chat_stream_preserves_ndjson_contract_and_multilingual_session(
    client: TestClient, services: FakeServices
) -> None:
    session_token = "22222222-2222-4222-8222-222222222222"
    response = client.post(
        "/api/chat-stream",
        json={
            "restaurantId": RESTAURANT_ID,
            "message": "Quelles options sont végétaliennes?",
            "widgetToken": "token",
            "sessionToken": session_token,
            "language": "fra",
        },
        headers={"Origin": "https://restaurant.example"},
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/x-ndjson")
    events = [json.loads(line) for line in response.text.splitlines()]
    assert [event["type"] for event in events] == ["session", "delta", "done"]
    payload, kwargs = services.chat_calls[0]
    assert str(payload.session_token) == session_token
    assert payload.language == "fra"
    assert kwargs["origin"] == "https://restaurant.example"


def test_message_length_limit_returns_error_shape(services: FakeServices) -> None:
    with TestClient(create_app(make_settings(max_message_chars=100), services)) as client:
        response = client.post(
            "/api/chat-stream",
            json={"restaurantId": RESTAURANT_ID, "message": "x" * 101, "widgetToken": "token"},
            headers={"Origin": "https://restaurant.example"},
        )
    assert response.status_code == 400
    assert response.json()["error"] == "message must be at most 100 characters"
    assert response.json()["requestId"]


def test_disabled_stripe_webhook_is_stable_503() -> None:
    service = object.__new__(ApplicationServices)
    service.settings = make_settings(stripe_webhooks_enabled=False)
    with pytest.raises(ServiceError, match="Stripe webhooks are disabled") as error:
        service.handle_stripe_webhook(b"{}", "signature")
    assert error.value.status_code == 503


def test_stripe_route_forwards_raw_payload_and_signature(client: TestClient) -> None:
    response = client.post(
        "/api/stripe/webhook",
        content=b'{"id":"evt_1"}',
        headers={"Stripe-Signature": "test-signature", "Content-Type": "application/json"},
    )
    assert response.status_code == 200
    assert response.json()["eventId"] == "evt_1"


def test_production_configuration_rejects_weak_secrets_and_localhost() -> None:
    with pytest.raises(ValueError, match="32 characters"):
        make_settings(app_env="production", widget_signing_keys="v1:short", allow_localhost_origins=False)
    with pytest.raises(ValueError, match="ALLOW_LOCALHOST_ORIGINS"):
        make_settings(
            app_env="production",
            widget_signing_keys="v1:" + "x" * 32,
            allow_localhost_origins=True,
        )


def test_optional_infrastructure_configuration_is_validated() -> None:
    with pytest.raises(ValueError, match="QUERY_CACHE_ENABLED"):
        make_settings(query_cache_enabled=True)
    with pytest.raises(ValueError, match="test or live secret key"):
        make_settings(
            stripe_webhooks_enabled=True,
            stripe_secret_key="bad-key",
            stripe_webhook_secret="whsec_test",
        )
