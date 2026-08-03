import asyncio
import base64
from datetime import datetime
from datetime import timedelta
from datetime import timezone
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from ozonenv.core.auth import TokenVerificationError
from starlette.requests import Request

from app.core.session import AppSession
from app.services import session_auth


def _build_request(
    path: str,
    headers: list[tuple[bytes, bytes]] | None = None,
) -> Request:
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": headers or [],
        "client": ("testclient", 1234),
        "server": ("testserver", 80),
        "root_path": "",
    }
    return Request(scope)


def _fake_jwt_with_exp(exp: int) -> str:
    header = {"alg": "none", "typ": "JWT"}
    payload = {"exp": exp}

    def _encode(value: dict) -> str:
        raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("utf-8").rstrip("=")

    return f"{_encode(header)}.{_encode(payload)}."


def _match_query(document, query):
    if not query:
        return True
    if "$and" in query:
        return all(_match_query(document, item) for item in query["$and"])
    if "$or" in query:
        return any(_match_query(document, item) for item in query["$or"])
    return all(document.get(key) == value for key, value in query.items())


class _FakeCollection:
    def __init__(self, docs=None):
        self.docs = list(docs or [])
        self.replace_calls = []

    async def find_one(self, query):
        for doc in self.docs:
            if _match_query(doc, query):
                return doc.copy()
        return None

    async def replace_one(self, query, payload, upsert=False):
        self.replace_calls.append(
            {"query": query.copy(), "payload": payload.copy(), "upsert": upsert}
        )
        for index, doc in enumerate(self.docs):
            if _match_query(doc, query):
                self.docs[index] = payload.copy()
                break
        else:
            self.docs.append(payload.copy())
        return SimpleNamespace(acknowledged=True)


class _FakeUserRecord:
    """Minimal CoreModel stand-in for ORM load/upsert returns."""

    def __init__(self, data: dict):
        self._data = data.copy()

    def get_dict(self) -> dict:
        return self._data.copy()

    def model_dump(self, mode="python") -> dict:
        return self._data.copy()


class _FakeUserModel:
    """Minimal ORM model stand-in for the 'user' collection."""

    def __init__(self, collection: "_FakeCollection"):
        self._coll = collection
        self.status = SimpleNamespace(fail=False)

    async def load(self, query: dict):
        doc = await self._coll.find_one(query)
        if doc:
            return _FakeUserRecord(doc)
        return None

    async def upsert(self, data: dict):
        rec_name = data.get("rec_name") or data.get("uid", "")
        await self._coll.replace_one(
            {"rec_name": rec_name},
            data.copy(),
            upsert=True,
        )
        return _FakeUserRecord(data)


class _FakeEngine:
    def __init__(self, collections):
        self.collections = collections

    def get_collection(self, name):
        return self.collections[name]


class _FakeGroupUsersModel:
    """Minimal group_users model stand-in (group/users/app_code rows)."""

    def __init__(self, rows=None):
        self.rows = list(rows or [])

    def get_domain(self, query):
        return query

    async def find(self, domain, limit=0):
        return [row.copy() for row in self.rows]


class _FakeOzonEnv:
    def __init__(self, user_docs=None, group_users=None):
        self._user_coll = _FakeCollection(user_docs)
        self.db = SimpleNamespace(
            engine=_FakeEngine({"user": self._user_coll})
        )
        self.orm = SimpleNamespace(
            app_settings=SimpleNamespace(
                upload_folder="/uploads",
                session_expire_hours=12,
                tz="Europe/Rome",
            )
        )
        self._group_users = _FakeGroupUsersModel(group_users)
        self.user_session = None
        self.session_token = ""
        self.upload_folder = ""

    def get(self, name: str):
        if name == "user":
            return _FakeUserModel(self._user_coll)
        if name == "group_users":
            return self._group_users
        raise ValueError(f"Unknown model: {name}")


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        keycloak_remote_user_header="x-remote-user",
        token_header="Authorization",
        keycloak_token_endpoint="https://kc.example/token",
        keycloak_client_id="backend-web",
        keycloak_client_secret="secret",
    )


def test_session_to_app_session_returns_app_session():
    source = AppSession(
        uid="legacy-user",
        token="tok-1",
        app_code="legacy",
        expire_datetime=datetime.now(timezone.utc) + timedelta(hours=1),
    )

    session = session_auth.session_to_app_session(source, "mci")

    assert isinstance(session, AppSession)
    assert session.uid == "legacy-user"
    assert session.app_code == "mci"
    assert session.sso_token == ""
    assert session.sso_refresh == ""


@pytest.mark.parametrize(
    "header_name",
    sorted(
        session_auth.OAUTH2_PROXY_IDENTITY_HEADER_FAMILY
        - {"x-remote-user"}
    ),
)
def test_only_configured_identity_header_is_trusted(header_name):
    request = _build_request(
        "/get_session",
        headers=[(header_name.encode("ascii"), b"spoofed.user")],
    )

    with pytest.raises(HTTPException) as exc_info:
        session_auth._extract_remote_user(request, _settings())

    assert exc_info.value.status_code == 401


def test_configured_identity_header_is_trusted():
    request = _build_request(
        "/get_session",
        headers=[(b"x-remote-user", b"trusted.user")],
    )

    assert session_auth._extract_remote_user(request, _settings()) == "trusted.user"


def test_build_keycloak_session_from_tokens_maps_invalid_token_to_401():
    class RejectingEnv:
        def __init__(self):
            self.params = {}

        async def session_app(self):
            raise TokenVerificationError("Audience doesn't match")

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            session_auth.build_keycloak_session_from_tokens(
                ozon_env=RejectingEnv(),
                settings=_settings(),
                app_code="nob-test",
                token={"access_token": "invalid"},
            )
        )

    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "Audience doesn't match"


def test_build_keycloak_session_reads_sso_tokens_from_headers():
    env = _FakeOzonEnv(
        user_docs=[
            {
                "uid": "kc.user",
                "full_name": "Keycloak User",
                "mail": "kc.user@example.org",
                "is_admin": True,
                "active": True,
                "deleted": 0,
            }
        ],
    )
    exp = int((datetime.now(timezone.utc) + timedelta(minutes=5)).timestamp())
    access_token = _fake_jwt_with_exp(exp)
    request = _build_request(
        "/get_session",
        headers=[
            (b"x-remote-user", b"kc.user"),
            (b"authorization", f"Bearer {access_token}".encode("utf-8")),
            (b"x-refresh-token", b"refresh-1"),
        ],
    )

    session = asyncio.run(
        session_auth.build_keycloak_session(
            ozon_env=env,
            request=request,
            settings=_settings(),
            app_code="mci",
        )
    )

    persisted = env.db.engine.get_collection("user").replace_calls[0]["payload"]
    assert session.sso_token == access_token
    assert session.sso_refresh == "refresh-1"
    assert session.sso_expire is not None
    # token/sso_token/sso_refresh sono `exclude=True` su AppSession (vedi
    # docs/SECURITY_KEYCLOAK_TOKEN_ANALYSIS.it.md): il bundle Keycloak
    # raw resta disponibile in memoria per la request corrente
    # (session.sso_refresh sopra) ma non viene piu' scritto nel
    # documento `user` da questo path.
    assert "sso_refresh" not in persisted
    assert "sso_token" not in persisted
    assert "token" not in persisted
    assert session.uid == "kc.user"


def test_build_keycloak_session_reuses_existing_token():
    """Existing token in user record is preserved across requests."""
    env = _FakeOzonEnv(
        user_docs=[
            {
                "uid": "kc.user",
                "token": "existing-uuid-token",
                "full_name": "Keycloak User",
                "active": True,
                "deleted": 0,
            }
        ],
    )
    request = _build_request(
        "/get_session",
        headers=[(b"x-remote-user", b"kc.user")],
    )

    session = asyncio.run(
        session_auth.build_keycloak_session(
            ozon_env=env,
            request=request,
            settings=_settings(),
            app_code="mci",
        )
    )

    assert session.token == "existing-uuid-token"


def test_build_keycloak_session_generates_token_when_none_exists():
    env = _FakeOzonEnv(
        user_docs=[
            {
                "uid": "new.user",
                "active": True,
                "deleted": 0,
            }
        ],
    )
    request = _build_request(
        "/get_session",
        headers=[(b"x-remote-user", b"new.user")],
    )

    session = asyncio.run(
        session_auth.build_keycloak_session(
            ozon_env=env,
            request=request,
            settings=_settings(),
            app_code="mci",
        )
    )

    assert session.token
    assert len(session.token) == 36  # UUID format


def test_build_keycloak_session_creates_user_when_not_found():
    """User not in DB → auto-created with is_admin=False (not in admins)."""
    env = _FakeOzonEnv(user_docs=[])
    request = _build_request(
        "/get_session",
        headers=[(b"x-remote-user", b"ghost.user")],
    )

    session = asyncio.run(
        session_auth.build_keycloak_session(
            ozon_env=env,
            request=request,
            settings=_settings(),
            app_code="mci",
        )
    )

    assert session.uid == "ghost.user"
    assert session.is_admin is False
    assert len(env.db.engine.get_collection("user").replace_calls) >= 1


def test_build_keycloak_session_sets_is_admin_from_admin_group_users():
    """User uid in group_users 'admin' group (app_code=mci) → is_admin=True."""
    env = _FakeOzonEnv(
        user_docs=[],
        group_users=[
            {
                "group": "admin",
                "users": ["admin.user"],
                "app_code": "mci",
                "active": True,
                "deleted": 0,
            }
        ],
    )
    settings_admin = SimpleNamespace(
        keycloak_remote_user_header="x-remote-user",
        token_header="Authorization",
        keycloak_token_endpoint="https://kc.example/token",
        keycloak_client_id="backend-web",
        keycloak_client_secret="secret",
    )
    request = _build_request(
        "/get_session",
        headers=[(b"x-remote-user", b"admin.user")],
    )

    session = asyncio.run(
        session_auth.build_keycloak_session(
            ozon_env=env,
            request=request,
            settings=settings_admin,
            app_code="mci",
        )
    )

    assert session.uid == "admin.user"
    assert session.is_admin is True


def test_ensure_sso_token_fresh_refreshes_when_expiring(monkeypatch):
    env = _FakeOzonEnv()
    old = _fake_jwt_with_exp(
        int((datetime.now(timezone.utc) + timedelta(seconds=5)).timestamp())
    )
    new = _fake_jwt_with_exp(
        int((datetime.now(timezone.utc) + timedelta(minutes=10)).timestamp())
    )
    session = AppSession(
        uid="kc.user",
        token="internal",
        app_code="mci",
        expire_datetime=datetime.now(timezone.utc) + timedelta(hours=1),
        sso_token=old,
        sso_refresh="refresh-1",
        sso_expire=datetime.now(timezone.utc) + timedelta(seconds=5),
    )

    async def _fake_refresh(settings, refresh_token):
        assert refresh_token == "refresh-1"
        return {
            "access_token": new,
            "refresh_token": "refresh-2",
            "expires_in": 600,
        }

    monkeypatch.setattr(session_auth, "_refresh_keycloak_tokens", _fake_refresh)

    updated = asyncio.run(
        session_auth.ensure_sso_token_fresh(
            ozon_env=env,
            settings=_settings(),
            session=session,
            refresh_margin_seconds=60,
        )
    )

    assert updated.sso_token == new
    assert updated.sso_refresh == "refresh-2"
    assert updated.sso_expire is not None
    assert len(env.db.engine.get_collection("user").replace_calls) == 1


def test_ensure_sso_token_fresh_fails_when_expired_without_refresh():
    env = _FakeOzonEnv()
    expired = AppSession(
        uid="kc.user",
        token="internal",
        app_code="mci",
        expire_datetime=datetime.now(timezone.utc) + timedelta(hours=1),
        sso_token="tok",
        sso_refresh="",
        sso_expire=datetime.now(timezone.utc) - timedelta(seconds=1),
    )

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            session_auth.ensure_sso_token_fresh(
                ozon_env=env,
                settings=_settings(),
                session=expired,
                refresh_margin_seconds=60,
            )
        )

    assert exc.value.status_code == 401


# --- /auth/callback: diagnosticabilita' del 502 ---------------------------


class _FakeResponse:
    """Risposta httpx minimale per _oauth_error."""

    def __init__(self, payload=None, text=""):
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def test_oauth_error_extracts_code_and_description():
    """Il 502 deve dire PERCHE': `invalid_grant` (redirect_uri/realm
    sbagliati) e `invalid_client` (secret sbagliato) hanno fix opposti,
    ma prima erano indistinguibili — il corpo veniva scartato."""
    from app.api.auth_routes import _oauth_error

    error, description = _oauth_error(
        _FakeResponse(
            {
                "error": "invalid_grant",
                "error_description": "Code not valid",
            }
        )
    )

    assert error == "invalid_grant"
    assert description == "Code not valid"


def test_oauth_error_falls_back_to_text_when_not_json():
    from app.api.auth_routes import _oauth_error

    error, description = _oauth_error(
        _FakeResponse(payload=None, text="502 Bad Gateway from proxy")
    )

    assert error == ""
    assert "Bad Gateway" in description


def test_oauth_error_truncates_long_description():
    from app.api.auth_routes import _oauth_error

    _, description = _oauth_error(
        _FakeResponse({"error": "x", "error_description": "A" * 5000})
    )

    assert len(description) == 200
