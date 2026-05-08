import asyncio
import base64
from datetime import datetime
from datetime import timedelta
from datetime import timezone
import json
from types import SimpleNamespace

from fastapi import Response
from starlette.requests import Request

from app.core.session import AppSession
from app.deps import app_env


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


class _SessionResult:
    def __init__(self, fail: bool = False, msg: str = ""):
        self.fail = fail
        self.msg = msg


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


class _FakeEngine:
    def __init__(self, collections):
        self.collections = collections

    def get_collection(self, name):
        return self.collections[name]


class _FakeOzonEnv:
    def __init__(
        self,
        user_app_code: str = "legacy",
        user_session=None,
        user_docs=None,
    ):
        self.params = {"current_session_token": "admin-token"}
        self.config_system = {"ozon_admin_token": "admin-token"}
        self.user_session = user_session or AppSession(
            uid="legacy-user",
            token="ok-token",
            app_code=user_app_code,
            expire_datetime=datetime.now(),
        )
        self.session_app_calls = 0
        self.db = SimpleNamespace(
            engine=_FakeEngine(
                {
                    "user": _FakeCollection(user_docs),
                }
            )
        )
        self.orm = SimpleNamespace(
            app_settings=SimpleNamespace(
                upload_folder="/uploads",
                session_expire_hours=12,
                tz="Europe/Rome",
            ),
            add_static_model=_noop_add_static_model,
        )
        self.upload_folder = ""

    async def session_app(self):
        self.session_app_calls += 1
        return _SessionResult()


async def _noop_add_static_model(name, model_class):
    pass


class _FakeService:
    def __init__(self, env):
        self.session = env.user_session


def test_build_ozon_cfg_ignores_admin_env_token(monkeypatch):
    _base = {
        "app_code": "mci",
        "mongo_user": "user",
        "mongo_pass": "pass",
        "mongo_url": "mongodb://localhost:27017",
        "mongo_db": "test",
        "mongo_replica": "",
        "models_folder": "/models",
        "backend_interface": "db",
    }
    monkeypatch.setattr(
        app_env,
        "settings",
        SimpleNamespace(
            **_base,
            ozon_env_cfg=lambda: _base.copy(),
            keycloak_jwks_url="",
            keycloak_issuer="",
            keycloak_token_endpoint="",
            keycloak_client_id="",
            keycloak_client_secret="",
        ),
    )

    cfg = app_env._build_ozon_cfg()

    assert cfg["app_code"] == "mci"
    assert "ozon_admin_token" not in cfg


_FAKE_SETTINGS = SimpleNamespace(
    auth_mode="token",
    auth_cookie_name="session",
    session_secret="test-secret",
    auth_cookie_max_age=3600,
    csrf_cookie_name="csrf",
    app_code="mci",
    keycloak_remote_user_header="x-remote-user",
    token_header="Authorization",
    auth_cookie_samesite="lax",
    cookie_secure=False,
)


def test_get_authed_env_sets_token_in_params(monkeypatch):
    monkeypatch.setattr(app_env, "settings", _FAKE_SETTINGS)

    ozon_env = _FakeOzonEnv(user_app_code="mci")
    response = Response()
    request = _build_request("/action/list")

    result = asyncio.run(
        app_env.get_authed_env("Bearer ok-token", ozon_env, request, response)
    )

    assert ozon_env.params["current_token"] == "ok-token"
    assert "ozon_admin_token" not in ozon_env.params
    assert "ozon_admin_token" not in ozon_env.config_system
    assert ozon_env.session_app_calls == 1
    assert result is ozon_env


def test_get_authed_env_removes_admin_token_from_params(monkeypatch):
    monkeypatch.setattr(app_env, "settings", _FAKE_SETTINGS)

    ozon_env = _FakeOzonEnv(user_app_code="mci")
    ozon_env.params["ozon_admin_token"] = "should-be-removed"
    ozon_env.config_system["ozon_admin_token"] = "should-be-removed"
    response = Response()
    request = _build_request("/models/distinct")

    asyncio.run(
        app_env.get_authed_env("Bearer ok-token", ozon_env, request, response)
    )

    assert "ozon_admin_token" not in ozon_env.params
    assert "ozon_admin_token" not in ozon_env.config_system


def test_get_authed_env_calls_session_app_once(monkeypatch):
    monkeypatch.setattr(app_env, "settings", _FAKE_SETTINGS)

    exp = int((datetime.now(timezone.utc) + timedelta(minutes=5)).timestamp())
    sso_token = _fake_jwt_with_exp(exp)
    ozon_env = _FakeOzonEnv(user_app_code="mci")
    response = Response()
    request = _build_request(
        "/get_session",
        headers=[(b"authorization", f"Bearer {sso_token}".encode("utf-8"))],
    )

    asyncio.run(
        app_env.get_authed_env(f"Bearer {sso_token}", ozon_env, request, response)
    )

    assert ozon_env.session_app_calls == 1
    assert ozon_env.params["current_token"] == sso_token
