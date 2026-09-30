from __future__ import annotations

import logging
from typing import Any

import httpx

from api_gateway.auth import M2MTokenProvider
from api_gateway.config import ResourceConfig, UpstreamConfig

logger = logging.getLogger("api_gateway")


class UpstreamError(RuntimeError):
    """L'upstream non ha risposto in modo utilizzabile."""


class UpstreamClient:
    """Client verso un upstream dichiarato nel config: header statici o
    bearer M2M, path sempre dal config, mai dal chiamante."""

    def __init__(
        self,
        code: str,
        config: UpstreamConfig,
        *,
        timeout: float,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.code = code
        self.config = config
        self.timeout = config.timeout or timeout
        self.token_provider: M2MTokenProvider | None = None
        if config.auth is not None:
            self.token_provider = M2MTokenProvider(
                token_url=config.auth.token_url,
                client_id=config.auth.client_id,
                client_secret=config.auth.client_secret,
                audience=config.auth.audience,
                scope=config.auth.scope,
            )
        self._client = client

    async def aclose(self) -> None:
        if self.token_provider is not None:
            await self.token_provider.aclose()

    async def _headers(self) -> dict[str, str]:
        headers = dict(self.config.headers)
        if self.token_provider is not None:
            headers["Authorization"] = await self.token_provider.authorization()
        return headers

    async def request(
        self,
        resource: ResourceConfig,
        *,
        method: str = "GET",
        params: list[tuple[str, str]] | None = None,
        body: Any = None,
    ) -> Any:
        url = f"{self.config.base_url}{resource.path}"
        kwargs: dict[str, Any] = {
            "headers": await self._headers(),
            "params": params or None,
        }
        if method == "POST":
            kwargs["json"] = body
        if self._client is not None:
            res = await self._client.request(method, url, **kwargs)
        else:
            # follow_redirects=False: un 30x non deve portare le credenziali
            # dell'upstream verso un host diverso da base_url.
            async with httpx.AsyncClient(
                timeout=self.timeout, follow_redirects=False
            ) as client:
                res = await client.request(method, url, **kwargs)
        if not 200 <= res.status_code < 300:
            raise UpstreamError(
                f"{self.code} {method} {resource.path} status={res.status_code}"
            )
        try:
            payload = res.json()
        except ValueError as exc:
            raise UpstreamError(
                f"{self.code} {method} {resource.path} risposta non JSON"
            ) from exc
        return payload
