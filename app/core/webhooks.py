from __future__ import annotations

import hashlib
import hmac
import json
import logging
from collections.abc import Callable
from typing import Any
from typing import Literal

import httpx
from fastapi import HTTPException
from fastapi import status
from pydantic import BaseModel
from pydantic import Field
from pydantic import field_validator

logger = logging.getLogger("uvicorn.error")

WebhookFailMode = Literal["open", "closed"]


class WebhookEndpoint(BaseModel):
    url: str
    events: list[str] = Field(default_factory=lambda: ["*"])
    active: bool = True
    timeout_seconds: float | None = None
    fail_mode: WebhookFailMode | None = None

    @field_validator("url")
    @classmethod
    def _required_url(cls, value: str) -> str:
        value = str(value or "").strip()
        if not value:
            raise ValueError("url required")
        return value

    @field_validator("events", mode="before")
    @classmethod
    def _parse_events(cls, value: Any) -> list[str]:
        if value in (None, "", []):
            return ["*"]
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        if isinstance(value, (list, tuple, set)):
            return [str(item).strip() for item in value if str(item).strip()]
        raise ValueError("events must be a list or CSV string")

    def accepts(self, event: str) -> bool:
        return self.active and ("*" in self.events or event in self.events)


class WebhookResult(BaseModel):
    allowed: bool = True
    payload: dict[str, Any] | None = None
    data: Any = None


class WebhookDispatcher:
    def __init__(
        self,
        *,
        enabled: bool,
        endpoints: list[WebhookEndpoint],
        timeout_seconds: float = 5.0,
        fail_mode: WebhookFailMode = "open",
        signing_secret: str = "",
        http_client_factory: Callable[[], httpx.AsyncClient] | None = None,
    ) -> None:
        self.enabled = enabled
        self.endpoints = endpoints
        self.timeout_seconds = timeout_seconds
        self.fail_mode = fail_mode
        self.signing_secret = signing_secret
        self.http_client_factory = (
            http_client_factory or self._default_http_client_factory
        )

    @classmethod
    def from_settings(cls, settings: Any) -> "WebhookDispatcher":
        raw_config = str(getattr(settings, "core_webhooks_json", "") or "")
        endpoints = parse_webhook_endpoints(raw_config)
        return cls(
            enabled=bool(getattr(settings, "core_webhooks_enabled", False)),
            endpoints=endpoints,
            timeout_seconds=float(
                getattr(settings, "core_webhooks_timeout_seconds", 5.0) or 5.0
            ),
            fail_mode=str(
                getattr(settings, "core_webhooks_fail_mode", "open") or "open"
            ),
            signing_secret=str(
                getattr(settings, "core_webhooks_signing_secret", "") or ""
            ),
        )

    def _default_http_client_factory(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=self.timeout_seconds)

    async def emit(
        self,
        event: str,
        *,
        context: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> WebhookResult:
        if not self.enabled:
            return WebhookResult(payload=payload)
        matching = [
            endpoint for endpoint in self.endpoints if endpoint.accepts(event)
        ]
        if not matching:
            return WebhookResult(payload=payload)

        current_payload = (
            payload.copy() if isinstance(payload, dict) else payload
        )
        merged_data: Any = None
        for endpoint in matching:
            response = await self._send(
                endpoint,
                event=event,
                context=context or {},
                payload=current_payload,
            )
            if response is None:
                continue
            if not response.allowed:
                return response
            if isinstance(response.payload, dict):
                current_payload = response.payload
            if response.data is not None:
                merged_data = response.data
        return WebhookResult(payload=current_payload, data=merged_data)

    async def _send(
        self,
        endpoint: WebhookEndpoint,
        *,
        event: str,
        context: dict[str, Any],
        payload: dict[str, Any] | None,
    ) -> WebhookResult | None:
        request_payload = {
            "event": event,
            "context": context,
            "payload": payload or {},
        }
        body = json.dumps(request_payload, separators=(",", ":"), default=str)
        headers = {
            "content-type": "application/json",
            "x-ozon-webhook-event": event,
        }
        if self.signing_secret:
            headers["x-ozon-webhook-signature"] = _signature(
                self.signing_secret,
                body,
            )
        try:
            async with self.http_client_factory() as client:
                response = await client.post(
                    endpoint.url,
                    content=body,
                    headers=headers,
                    timeout=endpoint.timeout_seconds or self.timeout_seconds,
                )
                response.raise_for_status()
                data = response.json() if response.content else {}
        except Exception as exc:
            return self._handle_failure(endpoint, event, exc)
        return _parse_response(data)

    def _handle_failure(
        self,
        endpoint: WebhookEndpoint,
        event: str,
        exc: Exception,
    ) -> WebhookResult | None:
        fail_mode = endpoint.fail_mode or self.fail_mode
        if fail_mode == "closed":
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "message": "Webhook unavailable",
                    "event": event,
                    "url": endpoint.url,
                },
            ) from exc
        logger.warning(
            "webhook failed open event=%s url=%s error=%s",
            event,
            endpoint.url,
            exc,
        )
        return None


def parse_webhook_endpoints(raw_config: str) -> list[WebhookEndpoint]:
    raw_config = str(raw_config or "").strip()
    if not raw_config:
        return []
    parsed = json.loads(raw_config)
    if isinstance(parsed, dict):
        parsed = parsed.get("endpoints", [])
    if not isinstance(parsed, list):
        raise ValueError("core webhooks config must be a list or object")
    return [WebhookEndpoint.model_validate(item) for item in parsed]


def _parse_response(data: Any) -> WebhookResult:
    if not isinstance(data, dict):
        return WebhookResult(data=data)
    allowed = not bool(data.get("denied", False))
    if "allow" in data:
        allowed = bool(data["allow"])
    if "allowed" in data:
        allowed = bool(data["allowed"])
    result = WebhookResult(
        allowed=allowed,
        payload=(
            data.get("payload")
            if isinstance(data.get("payload"), dict)
            else None
        ),
        data=data.get("data"),
    )
    if not result.allowed:
        raise HTTPException(
            status_code=int(
                data.get("status_code") or status.HTTP_403_FORBIDDEN
            ),
            detail=data.get("detail")
            or data.get("message")
            or {"message": "Webhook denied"},
        )
    return result


def _signature(secret: str, body: str) -> str:
    digest = hmac.new(
        secret.encode("utf-8"),
        body.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"sha256={digest}"
