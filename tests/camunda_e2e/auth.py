from __future__ import annotations

import os
import time

import httpx

# Mint dell'APP_TOKEN (JWT keycloak) per-utente, via password grant del client
# `backend-web`. L'uid della sessione app = preferred_username del token, quindi
# ogni chiamata all'app risulta fatta DA quell'utente.

E2E_PASSWORD = os.getenv("KEYCLOAK_E2E_PASSWORD", "e2e")


def _token_url() -> str:
    base = os.getenv("KEYCLOAK_URL", "http://keycloak:8080").rstrip("/")
    realm = os.getenv("KEYCLOAK_REALM", "backend")
    return f"{base}/realms/{realm}/protocol/openid-connect/token"


def mint_token(
    username: str,
    password: str = E2E_PASSWORD,
    *,
    attempts: int = 30,
    delay: float = 2.0,
) -> str:
    """Minta il JWT keycloak. Retry: keycloak/realm puo' non essere ancora
    pronto (nessun healthcheck sul container)."""
    data = {
        "grant_type": "password",
        "client_id": os.getenv("KEYCLOAK_CLIENT_ID", "backend-web"),
        "client_secret": os.getenv(
            "KEYCLOAK_CLIENT_SECRET", "camunda-e2e-secret"
        ),
        "username": username,
        "password": password,
        "scope": "openid",
    }
    last_exc: Exception | None = None
    for _ in range(attempts):
        try:
            resp = httpx.post(_token_url(), data=data, timeout=20.0)
            resp.raise_for_status()
            return resp.json()["access_token"]
        except Exception as exc:  # noqa: BLE001 - keycloak non ancora pronto
            last_exc = exc
            time.sleep(delay)
    raise RuntimeError(
        f"keycloak token non ottenuto per {username}: {last_exc}"
    )


class TokenCache:
    """Cache token per username (un solo giro keycloak per utente)."""

    def __init__(self) -> None:
        self._tokens: dict[str, str] = {}

    def get(self, username: str) -> str:
        if username not in self._tokens:
            self._tokens[username] = mint_token(username)
        return self._tokens[username]
