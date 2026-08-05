from __future__ import annotations

import asyncio
import json
import ssl
import time
import urllib.parse
import urllib.request
from collections.abc import Sequence
from typing import Any

try:
    import grpc
except (
    ImportError
):  # pragma: no cover - exercised only when Camunda is enabled
    grpc = None


class OAuthClientCredentialsTokenProvider:
    def __init__(
        self,
        *,
        token_url: str,
        client_id: str,
        client_secret: str,
        verify_tls: bool,
        audience: str | None = None,
        scope: str | None = None,
        timeout_seconds: float = 20.0,
    ) -> None:
        self.token_url = token_url
        self.client_id = client_id
        self.client_secret = client_secret
        self.verify_tls = verify_tls
        self.audience = audience
        self.scope = scope
        self.timeout_seconds = timeout_seconds
        self._access_token: str | None = None
        self._expires_at: float = 0.0
        self._lock = asyncio.Lock()

    async def get_access_token(self) -> str:
        if self._access_token and time.time() < self._expires_at:
            return self._access_token

        async with self._lock:
            if self._access_token and time.time() < self._expires_at:
                return self._access_token
            self._access_token, self._expires_at = await asyncio.to_thread(
                self._fetch_token
            )
            return self._access_token

    def _fetch_token(self) -> tuple[str, float]:
        payload = {
            "grant_type": "client_credentials",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }
        if self.audience:
            payload["audience"] = self.audience
        if self.scope:
            payload["scope"] = self.scope

        request = urllib.request.Request(
            self.token_url,
            data=urllib.parse.urlencode(payload).encode("utf-8"),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )

        context = None
        if self.token_url.lower().startswith("https://"):
            context = ssl.create_default_context()
            if not self.verify_tls:
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE

        # token_url e' l'endpoint M2M di Camunda da configurazione (non
        # input utente); sopra si valida schema https con ssl context.
        with urllib.request.urlopen(  # nosemgrep
            request,
            timeout=self.timeout_seconds,
            context=context,
        ) as response:
            response_payload = json.loads(response.read().decode("utf-8"))

        access_token = response_payload["access_token"]
        expires_in = int(response_payload.get("expires_in", 300))
        expires_at = time.time() + max(expires_in - 30, 30)
        return access_token, expires_at


_interceptor_bases = (
    (
        grpc.aio.UnaryUnaryClientInterceptor,
        grpc.aio.UnaryStreamClientInterceptor,
    )
    if grpc is not None
    else (object,)
)


class BearerTokenInterceptor(*_interceptor_bases):
    def __init__(
        self, token_provider: OAuthClientCredentialsTokenProvider
    ) -> None:
        self.token_provider = token_provider

    async def intercept_unary_unary(
        self, continuation, client_call_details, request
    ):
        authenticated_call_details = await self._with_authorization(
            client_call_details
        )
        return await continuation(authenticated_call_details, request)

    async def intercept_unary_stream(
        self, continuation, client_call_details, request
    ):
        authenticated_call_details = await self._with_authorization(
            client_call_details
        )
        return await continuation(authenticated_call_details, request)

    async def _with_authorization(self, client_call_details: Any) -> Any:
        if grpc is None:
            raise RuntimeError(
                "grpcio is required to use Camunda gRPC channels"
            )

        access_token = await self.token_provider.get_access_token()
        metadata = list(client_call_details.metadata or [])
        metadata = [
            item for item in metadata if item[0].lower() != "authorization"
        ]
        metadata.append(("authorization", f"Bearer {access_token}"))
        return grpc.aio.ClientCallDetails(
            client_call_details.method,
            client_call_details.timeout,
            metadata,
            client_call_details.credentials,
            client_call_details.wait_for_ready,
        )


def create_camunda_zeebe_channel(
    *,
    grpc_address: str,
    client_id: str,
    client_secret: str,
    authorization_server: str,
    verify_tls: bool,
    secure: bool,
    auth_enabled: bool = True,
    audience: str | None = None,
    scope: str | None = None,
    channel_options: Sequence[tuple[str, object]] | None = None,
):
    if grpc is None:
        raise RuntimeError(
            "grpcio is required to create a Camunda Zeebe channel"
        )

    interceptors = []
    if auth_enabled:
        token_provider = OAuthClientCredentialsTokenProvider(
            token_url=authorization_server,
            client_id=client_id,
            client_secret=client_secret,
            verify_tls=verify_tls,
            audience=audience,
            scope=scope,
        )
        interceptors.append(BearerTokenInterceptor(token_provider))
    if secure:
        return grpc.aio.secure_channel(
            target=grpc_address,
            credentials=grpc.ssl_channel_credentials(),
            options=channel_options,
            interceptors=interceptors,
        )

    return grpc.aio.insecure_channel(
        target=grpc_address,
        options=channel_options,
        interceptors=interceptors,
    )
