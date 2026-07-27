"""Regressioni per i fix di sicurezza dell'audit 2026-07.

Ogni test qui fissa un comportamento che PRIMA era permissivo: se uno di
questi torna verde "al contrario", il buco corrispondente e' riaperto.
Vedi docs/SECURITY_AUDIT_2026-07.it.md.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.core.service_registry import ServiceRegistryCore
from app.deps.app_env import require_admin_env
from app.services.cookie_auth import sign_token
from app.services.cookie_auth import verify_token


# --- Finding 1: service registry admin-only -------------------------------


def test_require_admin_env_rejects_non_admin():
    env = SimpleNamespace(
        user_session=SimpleNamespace(uid="u.mario", is_admin=False)
    )

    with pytest.raises(HTTPException) as exc:
        asyncio.run(require_admin_env(env))

    assert exc.value.status_code == 403
    assert "Admin" in str(exc.value.detail)


def test_require_admin_env_rejects_missing_session():
    """Sessione assente = non admin (fail-closed, non AttributeError)."""
    env = SimpleNamespace(user_session=None)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(require_admin_env(env))

    assert exc.value.status_code == 403


def test_require_admin_env_allows_admin():
    env = SimpleNamespace(
        user_session=SimpleNamespace(uid="u.admin", is_admin=True)
    )

    assert asyncio.run(require_admin_env(env)) is env


def test_service_registry_router_is_admin_gated():
    """Il gate deve stare sul router: questi endpoint scrivono via ORM
    diretto, quindi `model_groups_rule` non li copre."""
    from app.api import service_registry_router as mod

    dep_calls = [d.dependency for d in mod.router.dependencies]
    assert require_admin_env in dep_calls


@pytest.mark.parametrize(
    "bad_path",
    ["/etc", "/tmp/evil", "../../etc", "services/../../etc"],
)
def test_service_registry_source_dir_rejects_escaping_path(bad_path):
    """`source_path` finisce in `cwd` di `docker compose`: niente path
    assoluti ne' risalite."""
    core = ServiceRegistryCore(env=None)

    with pytest.raises(ValueError, match="source_path"):
        core._source_dir(
            {"source_path": bad_path}, project_root=Path("/srv/app")
        )


def test_service_registry_source_dir_accepts_relative_path():
    core = ServiceRegistryCore(env=None)

    result = core._source_dir(
        {"source_path": "services/mail_sender"},
        project_root=Path("/srv/app"),
    )

    assert result == Path("/srv/app/services/mail_sender")


def test_service_registry_up_maps_bad_path_to_400():
    """Un record scritto prima del vincolo deve dare 400, non 500."""
    import asyncio as _asyncio

    from app.api import service_registry_router as mod

    class _Svc:
        async def up_registered_service(self, code, build=True):
            raise ValueError("source_path must be a relative path")

    with pytest.raises(HTTPException) as exc:
        _asyncio.run(mod.up_service("mail_sender", _Svc(), True))

    assert exc.value.status_code == 400


# --- Finding 2: SSRF / esfiltrazione global_params -------------------------


def test_remote_select_csrf_exemption_removed():
    from app.deps import app_env

    exempt = app_env._READ_ONLY_POST_CSRF_EXEMPT_PATHS
    assert "/get_remote_data_select" not in exempt
    assert "/get_remote_select" not in exempt


def test_remote_fetch_has_bounded_timeout_and_no_redirects():
    """`timeout=None` teneva un worker occupato per sempre.

    `follow_redirects=False` e' gia' il default di httpx: qui e'
    esplicitato perche' un 302 sposterebbe l'header custom (che porta un
    segreto) su un altro host, quindi il default non deve cambiare
    silenziosamente sotto di noi.
    """
    import inspect

    from app.services import remote_service

    src = inspect.getsource(remote_service._fetch_remote_data)
    assert "timeout=None" not in src
    assert "follow_redirects=False" in src
    assert remote_service._REMOTE_FETCH_TIMEOUT_SECONDS > 0


# --- Finding 6: audience del token propagata al verificatore --------------


def test_build_ozon_cfg_propagates_token_audience(monkeypatch):
    from app.deps import app_env

    settings = SimpleNamespace(
        app_code="mci",
        token_audience="ozon-backend",
        keycloak_jwks_url="https://kc.example/certs",
        keycloak_issuer="https://kc.example/realms/backend",
        keycloak_token_endpoint="https://kc.example/token",
        keycloak_client_id="backend-web",
        keycloak_client_secret="s3cret",
        ozon_env_cfg=lambda: {"app_code": "mci"},
    )

    cfg = app_env._build_ozon_cfg(settings)

    assert cfg["token_audience"] == "ozon-backend"


def test_build_ozon_cfg_token_audience_empty_keeps_check_disabled():
    """Nessuna audience configurata = comportamento invariato (verify_aud
    resta False), non un 401 a sorpresa sui deploy esistenti."""
    from app.deps import app_env

    settings = SimpleNamespace(
        app_code="mci",
        token_audience=None,
        keycloak_jwks_url="",
        keycloak_issuer="",
        keycloak_token_endpoint="",
        keycloak_client_id="",
        keycloak_client_secret="",
        ozon_env_cfg=lambda: {"app_code": "mci"},
    )

    cfg = app_env._build_ozon_cfg(settings)

    assert cfg["token_audience"] == ""


# --- Finding 7: CSWSH sull'handshake WebSocket ----------------------------


class _FakeWs:
    def __init__(self, origin: str = "", cookies: dict | None = None) -> None:
        self.headers = {"origin": origin} if origin else {}
        self.cookies = cookies or {}


def test_ws_origin_allowlist_falls_back_to_external_base_url(monkeypatch):
    from app.api import websocket_router as mod

    monkeypatch.setattr(
        mod,
        "settings",
        SimpleNamespace(
            ws_allowed_origins="",
            external_base_url="https://app.example.org/",
            auth_cookie_name="session",
        ),
    )

    assert mod._allowed_origins() == {"https://app.example.org"}


def test_ws_cookie_handshake_from_foreign_origin_is_refused(monkeypatch):
    from app.api import websocket_router as mod

    monkeypatch.setattr(
        mod,
        "settings",
        SimpleNamespace(
            ws_allowed_origins="",
            external_base_url="https://app.example.org",
            auth_cookie_name="session",
        ),
    )
    ws = _FakeWs(origin="https://evil.example", cookies={"session": "x"})

    assert mod._origin_allowed(ws, cookie_auth=True) is False


def test_ws_cookie_handshake_from_own_origin_is_allowed(monkeypatch):
    from app.api import websocket_router as mod

    monkeypatch.setattr(
        mod,
        "settings",
        SimpleNamespace(
            ws_allowed_origins="",
            external_base_url="https://app.example.org",
            auth_cookie_name="session",
        ),
    )
    ws = _FakeWs(origin="https://app.example.org", cookies={"session": "x"})

    assert mod._origin_allowed(ws, cookie_auth=True) is True


def test_ws_bearer_client_is_not_origin_filtered(monkeypatch):
    """Worker/CLI non hanno Origin e non sono esposti a CSWSH."""
    from app.api import websocket_router as mod

    monkeypatch.setattr(
        mod,
        "settings",
        SimpleNamespace(
            ws_allowed_origins="",
            external_base_url="https://app.example.org",
            auth_cookie_name="session",
        ),
    )
    ws = _FakeWs(origin="", cookies={})

    assert mod._origin_allowed(ws, cookie_auth=False) is True


def test_ws_cookie_handshake_refused_when_no_origin_configured(monkeypatch):
    """Fail-closed: senza allowlist ne' EXTERNAL_BASE_URL non si passa."""
    from app.api import websocket_router as mod

    monkeypatch.setattr(
        mod,
        "settings",
        SimpleNamespace(
            ws_allowed_origins="",
            external_base_url="",
            auth_cookie_name="session",
        ),
    )
    ws = _FakeWs(origin="https://app.example.org", cookies={"session": "x"})

    assert mod._origin_allowed(ws, cookie_auth=True) is False


# --- Finding 9: cookie di sessione cifrato, non solo firmato --------------


def test_session_cookie_payload_is_not_readable():
    """Il bundle keycloak non deve essere estraibile dal cookie."""
    secret = "unit-test-secret"
    bundle = {"access_token": "AT-abc", "refresh_token": "RT-xyz"}

    cookie = sign_token(bundle, secret)

    assert "RT-xyz" not in cookie
    assert "AT-abc" not in cookie
    assert "refresh_token" not in cookie


def test_session_cookie_roundtrip_preserves_payload():
    secret = "unit-test-secret"
    bundle = {"access_token": "AT-abc", "refresh_token": "RT-xyz"}

    assert verify_token(sign_token(bundle, secret), secret) == bundle


def test_session_cookie_roundtrip_preserves_string_payload():
    """Lo state OAuth2 e' una stringa, non un dict."""
    secret = "unit-test-secret"

    assert verify_token(sign_token("state-123", secret), secret) == "state-123"


def test_session_cookie_rejects_wrong_secret():
    cookie = sign_token({"access_token": "AT"}, "secret-a")

    assert verify_token(cookie, "secret-b") is None


def test_session_cookie_rejects_tampered_value():
    secret = "unit-test-secret"
    cookie = sign_token({"access_token": "AT"}, secret)
    suffix = "AAAA" if not cookie.endswith("AAAA") else "BBBB"
    tampered = cookie[:-4] + suffix

    assert verify_token(tampered, secret) is None


def test_session_cookie_rejects_expired_value():
    secret = "unit-test-secret"
    cookie = sign_token({"access_token": "AT"}, secret)

    assert verify_token(cookie, secret, max_age=0) is None


def test_session_cookie_rejects_garbage():
    assert verify_token("not-a-cookie", "unit-test-secret") is None
    assert verify_token("", "unit-test-secret") is None
