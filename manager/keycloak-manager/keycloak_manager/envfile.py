from __future__ import annotations

from kc_provision import patch_dotenv


def token_url(server_url: str, realm: str) -> str:
    return (
        f"{server_url.rstrip('/')}/realms/{realm}"
        "/protocol/openid-connect/token"
    )


def build_env_vars(
    *,
    prefix: str,
    server_url: str,
    realm: str,
    m2m_client_id: str,
    m2m_secret: str,
    app_audience: str,
) -> dict[str, str]:
    """Env var del consumer. `prefix` configurabile (vuoto -> OAUTH_*; es
    `SCHEDULER` -> SCHEDULER_OAUTH_*). `app_audience` = clientId dell'app
    (client-audience): stesso valore in OAUTH_AUDIENCE e TOKEN_AUDIENCE
    (invariante audience)."""
    p = f"{prefix.rstrip('_')}_" if prefix else ""
    return {
        f"{p}OAUTH_TOKEN_URL": token_url(server_url, realm),
        f"{p}OAUTH_CLIENT_ID": m2m_client_id,
        f"{p}OAUTH_CLIENT_SECRET": m2m_secret,
        f"{p}OAUTH_AUDIENCE": app_audience,
        # Enforcement lato app: mappare su OZON_TOKEN_AUDIENCE. Abilitare SOLO
        # dopo che TUTTI i client emettono l'aud (altrimenti 401 a tappeto).
        "TOKEN_AUDIENCE": app_audience,
    }


def write_env_file(path: str, env_vars: dict[str, str]) -> None:
    """Scrive/aggiorna kc-env.var (upsert idempotente per chiave)."""
    patch_dotenv(path, env_vars)
