"""FastAPI application factory and public HTTP routes."""

from __future__ import annotations

import itertools
import logging
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Header, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse

from . import __version__
from .config import Settings, get_settings
from .logging_config import configure_logging
from .models import ChatStreamRequest, WidgetTokenRequest
from .service import ApplicationServices, ServiceError, normalize_origin

logger = logging.getLogger(__name__)


def _request_id(request: Request) -> str:
    return str(getattr(request.state, "request_id", "") or uuid.uuid4())


def _client_ip(request: Request, settings: Settings) -> str:
    if settings.trust_proxy_headers:
        forwarded = (request.headers.get("x-forwarded-for") or "").split(",", 1)[0].strip()
        if forwarded:
            return forwarded
        real_ip = (request.headers.get("x-real-ip") or "").strip()
        if real_ip:
            return real_ip
    return request.client.host if request.client else "unknown"


def create_app(settings: Settings | None = None, services: ApplicationServices | Any | None = None) -> FastAPI:
    provided_settings = settings
    provided_services = services

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        runtime_settings = provided_settings or get_settings()
        configure_logging(runtime_settings.log_level, runtime_settings.json_logs)
        runtime_services = provided_services or ApplicationServices(runtime_settings)
        app.state.settings = runtime_settings
        app.state.services = runtime_services
        logger.info("Application started", extra={"event": "startup"})
        try:
            yield
        finally:
            if provided_services is None:
                runtime_services.close()
            logger.info("Application stopped", extra={"event": "shutdown"})

    app = FastAPI(title="MBD Restaurant RAG API", version=__version__, lifespan=lifespan)

    @app.middleware("http")
    async def request_context(request: Request, call_next: Any) -> Response:
        started = time.perf_counter()
        request.state.request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
        runtime_settings: Settings | None = getattr(app.state, "settings", provided_settings)
        max_bytes = runtime_settings.max_request_bytes if runtime_settings else 65_536
        content_length = request.headers.get("content-length")
        early_response: Response | None = None
        if content_length:
            try:
                too_large = int(content_length) > max_bytes
            except ValueError:
                early_response = JSONResponse(
                    status_code=400,
                    content={"error": "Invalid Content-Length header", "requestId": _request_id(request)},
                )
                too_large = False
            if too_large and early_response is None:
                early_response = JSONResponse(
                    status_code=413,
                    content={"error": "Request body is too large", "requestId": _request_id(request)},
                )

        if early_response is None and request.method in {"POST", "PUT", "PATCH"}:
            body = await request.body()
            if len(body) > max_bytes:
                early_response = JSONResponse(
                    status_code=413,
                    content={"error": "Request body is too large", "requestId": _request_id(request)},
                )

        origin = normalize_origin(request.headers.get("origin", ""))
        response: Response
        if early_response is not None:
            response = early_response
        elif request.method == "OPTIONS":
            response = Response(status_code=204)
        else:
            response = await call_next(request)
        response.headers["X-Request-Id"] = _request_id(request)
        if origin:
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Vary"] = "Origin"
            response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, X-Request-Id"
            response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
            response.headers["Access-Control-Max-Age"] = "600"
        logger.info(
            "Request completed",
            extra={
                "event": "http_request",
                "request_id": _request_id(request),
                "status_code": response.status_code,
                "latency_ms": int((time.perf_counter() - started) * 1000),
            },
        )
        return response

    @app.exception_handler(ServiceError)
    async def service_error_handler(request: Request, exc: ServiceError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code, content={"error": exc.message, "requestId": _request_id(request)}
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        fields = [".".join(str(part) for part in item.get("loc", [])[1:]) for item in exc.errors()]
        return JSONResponse(
            status_code=400,
            content={"error": "Invalid request", "requestId": _request_id(request), "fields": [x for x in fields if x]},
        )

    @app.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled request error", extra={"request_id": _request_id(request)})
        return JSONResponse(
            status_code=500, content={"error": "Internal server error", "requestId": _request_id(request)}
        )

    @app.get("/healthz")
    def health() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/readyz")
    def readiness(request: Request) -> JSONResponse:
        payload = request.app.state.services.ready()
        return JSONResponse(status_code=200 if payload.get("ok") else 503, content=payload)

    @app.post("/api/widget-token")
    def widget_token(payload: WidgetTokenRequest, request: Request) -> dict[str, Any]:
        return request.app.state.services.issue_widget_token(
            str(payload.restaurant_id),
            normalize_origin(request.headers.get("origin", "")),
            _client_ip(request, request.app.state.settings),
        )

    @app.post("/api/chat-stream")
    def chat_stream(payload: ChatStreamRequest, request: Request) -> StreamingResponse:
        if len(payload.message) > request.app.state.settings.max_message_chars:
            raise ServiceError(
                400, f"message must be at most {request.app.state.settings.max_message_chars} characters"
            )
        iterator = request.app.state.services.stream_chat(
            payload,
            origin=request.headers.get("origin", ""),
            client_ip=_client_ip(request, request.app.state.settings),
            request_id=_request_id(request),
            client_meta={
                "user_agent": request.headers.get("user-agent", ""),
                "accept_language": request.headers.get("accept-language", ""),
            },
        )
        try:
            first = next(iterator)
        except StopIteration as exc:
            raise ServiceError(500, "Chat stream ended unexpectedly") from exc
        return StreamingResponse(
            itertools.chain([first], iterator),
            media_type="application/x-ndjson",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/stripe/webhook")
    async def stripe_webhook(
        request: Request,
        stripe_signature: str = Header(default="", alias="Stripe-Signature"),
    ) -> dict[str, Any]:
        return request.app.state.services.handle_stripe_webhook(await request.body(), stripe_signature)

    return app


app = create_app()
