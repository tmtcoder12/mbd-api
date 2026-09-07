from typing import Any

import pytest

from mbd_api.config import Settings
from mbd_api.security import verify_widget_token
from mbd_api.service import ApplicationServices, ServiceError

RESTAURANT_ID = "11111111-1111-4111-8111-555555555555"


def settings() -> Settings:
    return Settings(
        _env_file=None,
        app_env="test",
        openai_api_key="test-key",
        supabase_url="http://supabase.test",
        supabase_service_role_key="test-key",
        widget_signing_keys="v1:test-signing-key",
    )


class FakeStore:
    def __init__(self, *, active: bool = True, origin_allowed: bool = True) -> None:
        self.active = active
        self.origin_allowed = origin_allowed

    def restaurant_exists(self, restaurant_id: str) -> bool:
        return restaurant_id == RESTAURANT_ID

    def restaurant_has_active_subscription(self, restaurant_id: str) -> bool:
        return self.active

    def get_restaurant_security_settings(self, restaurant_id: str) -> dict[str, int]:
        return {}

    def origin_allowed_for_restaurant(self, restaurant_id: str, origin: str) -> bool:
        return self.origin_allowed

    def close(self) -> None:
        pass


class FakeLimiter:
    def __init__(self, allowed: bool = True) -> None:
        self.allowed = allowed
        self.keys: list[str] = []

    def allow(self, key: str, **kwargs: Any) -> bool:
        self.keys.append(key)
        return self.allowed


def service(store: FakeStore, limiter: FakeLimiter | None = None) -> ApplicationServices:
    return ApplicationServices(
        settings(),
        openai_client=object(),
        store=store,
        rate_limiter=limiter or FakeLimiter(),
    )


def test_subscription_and_origin_gates_are_enforced() -> None:
    with pytest.raises(ServiceError, match="subscription is inactive") as inactive:
        service(FakeStore(active=False)).issue_widget_token(RESTAURANT_ID, "https://restaurant.example", "203.0.113.10")
    assert inactive.value.status_code == 403

    with pytest.raises(ServiceError, match="Origin is not allowed") as denied:
        service(FakeStore(origin_allowed=False)).issue_widget_token(
            RESTAURANT_ID, "https://restaurant.example", "203.0.113.10"
        )
    assert denied.value.status_code == 403


def test_widget_token_is_bound_to_tenant_and_normalized_origin() -> None:
    app_service = service(FakeStore())
    response = app_service.issue_widget_token(RESTAURANT_ID, "https://restaurant.example", "203.0.113.10")
    payload = verify_widget_token(
        response["widgetToken"],
        app_service.widget_signing_keys,
        expected_restaurant_id=RESTAURANT_ID,
        expected_origin="https://restaurant.example",
        max_age_seconds=900,
    )
    assert payload["rid"] == RESTAURANT_ID
    with pytest.raises(ValueError, match="restaurant mismatch"):
        verify_widget_token(
            response["widgetToken"],
            app_service.widget_signing_keys,
            expected_restaurant_id="22222222-2222-4222-8222-222222222222",
            expected_origin="https://restaurant.example",
            max_age_seconds=900,
        )


def test_rate_limit_failure_is_safe() -> None:
    with pytest.raises(ServiceError, match="Rate limit exceeded") as error:
        service(FakeStore(), FakeLimiter(allowed=False)).issue_widget_token(
            RESTAURANT_ID, "https://restaurant.example", "203.0.113.10"
        )
    assert error.value.status_code == 429
