from __future__ import annotations

import logging
import time
from typing import Any

import httpx

logger = logging.getLogger("calendar_scheduler")


class M2MTokenProvider:
    """Token keycloak via client_credentials (machine-to-machine).

    L'endpoint run dell'app verifica il bearer come JWT keycloak (jwks): un
    token statico NON passa `jwt.decode` -> 401. Quindi serve un JWT vero,
    rinnovato prima della scadenza. L'unico segreto e' il `client_secret`.

    Requisiti lato app/keycloak:
      - il `client` keycloak deve avere il service account abilitato;
      - l'`uid` del service account (`service-account-<client>` o
        `preferred_username`) deve essere negli `admins` di OGNI app_code, altri-
        menti l'ACL nega le scritture (403);
      - l'`aud` del token deve combaciare con `OZON_TOKEN_AUDIENCE` dell'app.
    """

    def __init__(
        self,
        token_url: str,
        client_id: str,
        client_secret: str,
        *,
        audience: str = "",
        scope: str = "",
        refresh_skew_seconds: int = 30,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._token_url = token_url
        self._client_id = client_id
        self._client_secret = client_secret
        self._audience = audience
        self._scope = scope
        self._refresh_skew = refresh_skew_seconds
        self._http = http_client or httpx.AsyncClient(timeout=30.0)
        self._token: str = ""
        self._expires_at: float = 0.0

    async def aclose(self) -> None:
        await self._http.aclose()

    def _is_valid(self) -> bool:
        return bool(self._token) and time.time() < (
            self._expires_at - self._refresh_skew
        )

    async def _fetch(self) -> None:
        data: dict[str, Any] = {
            "grant_type": "client_credentials",
            "client_id": self._client_id,
            "client_secret": self._client_secret,
        }
        if self._audience:
            data["audience"] = self._audience
        if self._scope:
            data["scope"] = self._scope
        resp = await self._http.post(self._token_url, data=data)
        resp.raise_for_status()
        body = resp.json()
        token = body.get("access_token")
        if not token:
            raise ValueError("token response without access_token")
        self._token = token
        # `expires_in` da keycloak; fallback prudente a 60s.
        expires_in = float(body.get("expires_in", 60) or 60)
        self._expires_at = time.time() + expires_in
        logger.info("m2m token rinnovato (expires_in=%ss)", expires_in)

    async def token(self) -> str:
        if not self._is_valid():
            await self._fetch()
        return self._token

    async def authorization(self) -> str:
        return f"Bearer {await self.token()}"
