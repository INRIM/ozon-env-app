"""Revoca sessioni via back-channel logout + emissione cookie di sessione."""

import time

import pytest

from app.services import session_revocation
from app.services.cookie_auth import refresh_token_max_age
from app.services.cookie_auth import session_cookie_max_age
from app.services.session_revocation import BackchannelLogoutError
from app.services.session_revocation import is_session_revoked
from app.services.session_revocation import purge_expired_revocations
from app.services.session_revocation import revoke_session
from app.services.session_revocation import verify_logout_token


def _match(doc, query):
    if not query:
        return True
    if "$or" in query:
        return any(_match(doc, item) for item in query["$or"])
    if "$and" in query:
        return all(_match(doc, item) for item in query["$and"])
    for key, expected in query.items():
        value = doc.get(key)
        if isinstance(expected, dict):
            if "$gt" in expected and not (value > expected["$gt"]):
                return False
            if "$lt" in expected and not (value < expected["$lt"]):
                return False
        elif value != expected:
            return False
    return True


class _FakeRecord(dict):
    """Record minimale: i test leggono solo il payload."""


class _FakeModel:
    def __init__(self):
        self.docs: list[dict] = []
        self.status = None

    async def upsert(self, data):
        for index, doc in enumerate(self.docs):
            if doc["rec_name"] == data["rec_name"]:
                self.docs[index] = dict(data)
                return _FakeRecord(data)
        self.docs.append(dict(data))
        return _FakeRecord(data)

    async def count(self, domain=None):
        return len([doc for doc in self.docs if _match(doc, domain or {})])

    async def find(self, domain=None):
        return [
            _FakeRecord(doc)
            for doc in self.docs
            if _match(doc, domain or {})
        ]

    async def remove(self, record):
        before = len(self.docs)
        self.docs = [
            doc for doc in self.docs if doc["rec_name"] != record["rec_name"]
        ]
        return len(self.docs) < before


class _FakeEnv:
    def __init__(self):
        self.model = _FakeModel()

    def get(self, name):
        assert name == session_revocation.REVOKED_MODEL
        return self.model


def _logout_claims(**overrides):
    claims = {
        "iss": "http://kc/realms/backend",
        "aud": "backend-web",
        "sid": "sid-1",
        "sub": "sub-1",
        "preferred_username": "user",
        "events": {session_revocation.BACKCHANNEL_LOGOUT_EVENT: {}},
    }
    claims.update(overrides)
    return claims


class _FakeAuthManager:
    def __init__(self, claims=None, error=None):
        self.claims = claims
        self.error = error

    async def verify(self, token, expected_client_id=""):
        if self.error:
            raise self.error
        return type("V", (), {"claims": self.claims})()


@pytest.fixture
def settings():
    from app.app_settings import get_env_settings

    return get_env_settings()


def _patch_manager(monkeypatch, claims=None, error=None):
    monkeypatch.setattr(
        session_revocation,
        "_logout_auth_manager",
        lambda _settings: _FakeAuthManager(claims, error),
    )


@pytest.mark.asyncio
async def test_verify_logout_token_accepts_valid_token(monkeypatch, settings):
    _patch_manager(monkeypatch, _logout_claims())
    claims = await verify_logout_token("token", settings)
    assert claims["sid"] == "sid-1"


@pytest.mark.asyncio
async def test_verify_logout_token_rejects_missing_event(monkeypatch, settings):
    """Un ID token normale ha la stessa firma e la stessa audience: senza il
    controllo su `events` varrebbe come ordine di logout."""
    _patch_manager(monkeypatch, _logout_claims(events={}))
    with pytest.raises(BackchannelLogoutError):
        await verify_logout_token("token", settings)


@pytest.mark.asyncio
async def test_verify_logout_token_rejects_nonce(monkeypatch, settings):
    _patch_manager(monkeypatch, _logout_claims(nonce="n"))
    with pytest.raises(BackchannelLogoutError):
        await verify_logout_token("token", settings)


@pytest.mark.asyncio
async def test_verify_logout_token_rejects_without_sid_and_sub(
    monkeypatch, settings
):
    _patch_manager(monkeypatch, _logout_claims(sid="", sub=""))
    with pytest.raises(BackchannelLogoutError):
        await verify_logout_token("token", settings)


@pytest.mark.asyncio
async def test_verify_logout_token_rejects_bad_signature(monkeypatch, settings):
    _patch_manager(monkeypatch, error=ValueError("bad signature"))
    with pytest.raises(BackchannelLogoutError):
        await verify_logout_token("token", settings)


@pytest.mark.asyncio
async def test_revoke_by_sid_blocks_only_that_session():
    env = _FakeEnv()
    await revoke_session(env, _logout_claims(sid="sid-1"))

    assert await is_session_revoked(env, {"sid": "sid-1", "sub": "sub-1"})
    assert not await is_session_revoked(env, {"sid": "sid-2", "sub": "sub-1"})


@pytest.mark.asyncio
async def test_revoke_by_sid_is_idempotent():
    """Keycloak ritenta il POST su timeout: la stessa riga va riscritta,
    non duplicata."""
    env = _FakeEnv()
    await revoke_session(env, _logout_claims(sid="sid-1"))
    await revoke_session(env, _logout_claims(sid="sid-1"))
    assert len(env.model.docs) == 1


@pytest.mark.asyncio
async def test_revoke_by_sub_blocks_only_tokens_issued_before():
    """Revoca "tutte le sessioni": non deve invalidare il login successivo
    dello stesso utente."""
    env = _FakeEnv()
    await revoke_session(env, _logout_claims(sid="", sub="sub-1"))
    revoked_at = env.model.docs[0]["revoked_at"]

    old_token = {"sid": "sid-old", "sub": "sub-1", "iat": revoked_at - 10}
    new_token = {"sid": "sid-new", "sub": "sub-1", "iat": revoked_at + 10}
    assert await is_session_revoked(env, old_token)
    assert not await is_session_revoked(env, new_token)


@pytest.mark.asyncio
async def test_is_session_revoked_false_without_claims():
    env = _FakeEnv()
    assert not await is_session_revoked(env, {})


@pytest.mark.asyncio
async def test_purge_removes_only_expired_rows():
    env = _FakeEnv()
    now = time.time()
    env.model.docs = [
        {"rec_name": "sid-old", "sid": "a", "expire_at": now - 1},
        {"rec_name": "sid-live", "sid": "b", "expire_at": now + 3600},
    ]
    removed = await purge_expired_revocations(env)
    assert removed == 1
    assert [doc["rec_name"] for doc in env.model.docs] == ["sid-live"]


def test_cookie_max_age_capped_by_refresh_lifetime():
    """Cookie a 24h + sessione SSO a 30' = 401 a meta' lavoro senza che il
    client sappia perche'."""
    assert session_cookie_max_age(86400, {"refresh_expires_in": 1800}) == 1800
    assert session_cookie_max_age(600, {"refresh_expires_in": 1800}) == 600


def test_cookie_max_age_falls_back_to_configured_when_unknown():
    assert session_cookie_max_age(86400, {}) == 86400
    assert session_cookie_max_age(86400, "opaque-token") == 86400
    assert refresh_token_max_age({"refresh_token": "opaque"}) == 0


def test_logout_redirect_is_made_absolute():
    """Keycloak rifiuta con 400 un `post_logout_redirect_uri` relativo: col
    default "/" il logout finiva su una pagina di errore di Keycloak."""
    from app.app_settings import EnvSettings

    relative = EnvSettings(
        EXTERNAL_BASE_URL="http://localhost:4200", LOGOUT_REDIRECT_URL="/"
    )
    assert relative.logout_redirect_absolute_url == "http://localhost:4200/"

    absolute = EnvSettings(
        EXTERNAL_BASE_URL="http://localhost:4200",
        LOGOUT_REDIRECT_URL="https://portal.test/bye",
    )
    assert absolute.logout_redirect_absolute_url == "https://portal.test/bye"


def test_internal_logout_endpoint_uses_internal_hostname():
    """La revoca del refresh token e' server->Keycloak: deve passare per
    l'hostname interno come token/jwks, non per quello del browser."""
    from app.app_settings import EnvSettings

    configured = EnvSettings(
        KEYCLOAK_SERVER_URL_INTERNAL="http://keycloak:8080",
        KEYCLOAK_SERVER_URL_PUBLIC="http://localhost:8081",
        KEYCLOAK_REALM="backend",
    )
    assert configured.keycloak_logout_endpoint_internal.startswith(
        "http://keycloak:8080/"
    )
    assert configured.keycloak_logout_endpoint.startswith(
        "http://localhost:8081/"
    )


class _RouteEnv(_FakeEnv):
    """Env come lo vede l'endpoint: `revoked_session` NON e' registrato
    finche' non lo registra `register_static_models`."""

    def __init__(self):
        super().__init__()
        self.registered = False

    def get(self, name):
        if not self.registered:
            # Esattamente il comportamento che ha prodotto il 500 in
            # produzione: `get_ozon_env` non registra i model statici.
            return None
        return super().get(name)


def _backchannel_client(monkeypatch, env, claims=None):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import auth_routes
    from app.deps.app_env import get_ozon_env

    async def _register(target_env):
        target_env.registered = True

    monkeypatch.setattr(auth_routes, "register_static_models", _register)
    monkeypatch.setattr(
        auth_routes,
        "verify_logout_token",
        _async_return(claims if claims is not None else _logout_claims()),
    )

    app = FastAPI()
    app.include_router(auth_routes.router)
    app.dependency_overrides[get_ozon_env] = lambda: env
    return TestClient(app)


def _async_return(value):
    async def _call(*args, **kwargs):
        return value

    return _call


def test_backchannel_logout_route_registers_static_models(monkeypatch):
    """Regressione: l'endpoint gira su `get_ozon_env`, che non registra i
    model statici — senza `register_static_models` il revoke esplodeva con
    500 AttributeError su None."""
    env = _RouteEnv()
    client = _backchannel_client(monkeypatch, env)

    response = client.post(
        "/auth/backchannel-logout", data={"logout_token": "jwt"}
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert [doc["sid"] for doc in env.model.docs] == ["sid-1"]


def test_backchannel_logout_route_rejects_invalid_token(monkeypatch):
    from app.api import auth_routes

    env = _RouteEnv()
    client = _backchannel_client(monkeypatch, env)

    async def _raise(*args, **kwargs):
        raise BackchannelLogoutError("nope")

    monkeypatch.setattr(auth_routes, "verify_logout_token", _raise)

    response = client.post(
        "/auth/backchannel-logout", data={"logout_token": "jwt"}
    )

    assert response.status_code == 400
    assert env.model.docs == []


def test_backchannel_logout_route_purges_expired_rows(monkeypatch):
    env = _RouteEnv()
    env.registered = True
    env.model.docs.append(
        {
            "rec_name": "sid-stale",
            "sid": "stale",
            "sub": "",
            "expire_at": time.time() - 10,
        }
    )
    client = _backchannel_client(monkeypatch, env)

    response = client.post(
        "/auth/backchannel-logout", data={"logout_token": "jwt"}
    )

    assert response.status_code == 200
    assert "sid-stale" not in [doc["rec_name"] for doc in env.model.docs]
