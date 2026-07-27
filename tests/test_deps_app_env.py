import asyncio
import base64
from copy import copy
from datetime import datetime
from datetime import timedelta
from datetime import timezone
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi import Response
from starlette.requests import Request

from app.core.session import AppSession
from app.deps import app_env


def _build_request(
    path: str,
    method: str = "GET",
    headers: list[tuple[bytes, bytes]] | None = None,
    query_string: str = "",
) -> Request:
    raw_path = path.encode()
    if query_string:
        raw_path = f"{path}?{query_string}".encode()
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": raw_path,
        "query_string": query_string.encode(),
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


class _FakeGroupUsersModel:
    """Minimal group_users model stand-in (group/users/app_code rows)."""

    def __init__(self, rows=None):
        self.rows = list(rows or [])

    def get_domain(self, query):
        return query

    async def find(self, domain, limit=0):
        return [row.copy() for row in self.rows]


class _FakeOzonEnv:
    def __init__(
        self,
        user_app_code: str = "legacy",
        runtime_app_code: str | None = None,
        user_session=None,
        user_docs=None,
        group_users=None,
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
                app_code=runtime_app_code or user_app_code,
                upload_folder="/uploads",
                session_expire_hours=12,
                tz="Europe/Rome",
            ),
            add_static_model=_noop_add_static_model,
        )
        self._group_users = _FakeGroupUsersModel(group_users)
        self.upload_folder = ""
        self.models = {}

    async def session_app(self):
        self.session_app_calls += 1
        return _SessionResult()

    def get(self, name: str):
        if name == "group_users":
            return self._group_users
        raise ValueError(f"Unknown model: {name}")


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


def test_resolve_request_app_code_prefers_query_over_cookie_and_default(monkeypatch):
    source_settings = copy(_FAKE_SETTINGS)
    source_settings.app_code = "nob-test"
    request = _build_request(
        "/get_session",
        headers=[(b"cookie", b"app_code=from-cookie")],
        query_string="app_code=persona",
    )

    app_code = app_env._resolve_request_app_code(request, source_settings)

    assert app_code == "persona"


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


def test_get_authed_env_sets_is_admin_from_group_users(monkeypatch):
    """is_admin/groups sourced live from group_users, not setting_app.admins."""
    monkeypatch.setattr(app_env, "settings", _FAKE_SETTINGS)

    ozon_env = _FakeOzonEnv(
        user_app_code="mci",
        group_users=[
            {
                "group": "admin",
                "users": ["legacy-user"],
                "app_code": "mci",
                "active": True,
                "deleted": 0,
            }
        ],
    )
    response = Response()
    request = _build_request("/action/list")

    asyncio.run(
        app_env.get_authed_env("Bearer ok-token", ozon_env, request, response)
    )

    assert ozon_env.user_session.is_admin is True
    assert "admin" in ozon_env.user_session.user["groups"]


def test_get_authed_env_is_not_admin_without_group_users_membership(monkeypatch):
    monkeypatch.setattr(app_env, "settings", _FAKE_SETTINGS)

    ozon_env = _FakeOzonEnv(user_app_code="mci")
    response = Response()
    request = _build_request("/action/list")

    asyncio.run(
        app_env.get_authed_env("Bearer ok-token", ozon_env, request, response)
    )

    assert ozon_env.user_session.is_admin is False


def test_get_authed_env_uses_runtime_app_code_for_session_and_cookie(monkeypatch):
    monkeypatch.setattr(app_env, "settings", _FAKE_SETTINGS)

    ozon_env = _FakeOzonEnv(
        user_app_code="legacy",
        runtime_app_code="persona",
    )
    response = Response()
    request = _build_request("/get_session", query_string="app_code=persona")

    asyncio.run(
        app_env.get_authed_env("Bearer ok-token", ozon_env, request, response)
    )

    assert ozon_env.user_session.app_code == "persona"
    app_code_cookie = next(
        header
        for header in response.headers.getlist("set-cookie")
        if header.startswith("app_code=persona")
    )
    assert "HttpOnly" not in app_code_cookie


@pytest.mark.parametrize(
    "path",
    [
        "/list/customer",
        "/models/distinct",
    ],
)
def test_get_authed_env_skips_csrf_for_read_only_post_routes(
    monkeypatch, path
):
    monkeypatch.setattr(app_env, "settings", _FAKE_SETTINGS)

    signed_session = app_env.sign_token("ok-token", _FAKE_SETTINGS.session_secret)
    request = _build_request(
        path,
        method="POST",
        headers=[(b"cookie", f"session={signed_session}".encode("utf-8"))],
    )
    response = Response()
    ozon_env = _FakeOzonEnv(user_app_code="mci")

    result = asyncio.run(app_env.get_authed_env(None, ozon_env, request, response))

    assert result is ozon_env
    assert ozon_env.params["current_token"] == "ok-token"
    assert ozon_env.session_app_calls == 1


@pytest.mark.parametrize(
    "path",
    [
        "/get_remote_data_select",
        "/get_remote_select",
    ],
)
def test_get_authed_env_requires_csrf_for_remote_select_routes(
    monkeypatch, path
):
    """Le select remote non sono piu' esenti da CSRF.

    Restano letture, ma fanno partire una richiesta HTTP in uscita dal
    server verso un endpoint esterno: non hanno titolo per l'esenzione
    pensata per le POST-usate-come-GET.
    """
    monkeypatch.setattr(app_env, "settings", _FAKE_SETTINGS)

    signed_session = app_env.sign_token(
        "ok-token", _FAKE_SETTINGS.session_secret
    )
    request = _build_request(
        path,
        method="POST",
        headers=[(b"cookie", f"session={signed_session}".encode("utf-8"))],
    )
    response = Response()
    ozon_env = _FakeOzonEnv(user_app_code="mci")

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            app_env.get_authed_env(None, ozon_env, request, response)
        )

    assert exc.value.status_code == 403
    assert "CSRF" in str(exc.value.detail)


def test_get_authed_env_requires_csrf_for_cookie_write_routes(monkeypatch):
    monkeypatch.setattr(app_env, "settings", _FAKE_SETTINGS)

    signed_session = app_env.sign_token("ok-token", _FAKE_SETTINGS.session_secret)
    request = _build_request(
        "/record/customer/CUS001",
        method="POST",
        headers=[(b"cookie", f"session={signed_session}".encode("utf-8"))],
    )
    response = Response()
    ozon_env = _FakeOzonEnv(user_app_code="mci")

    with pytest.raises(HTTPException) as exc:
        asyncio.run(app_env.get_authed_env(None, ozon_env, request, response))

    assert exc.value.status_code == 403
    assert exc.value.detail == "CSRF validation failed"


def test_static_models_implement_basic_model_hooks_used_by_init_model():
    # OzonModel.init_model() chiama .file_fields()/.tranform_data_value()/
    # .model_depends() sulla classe statica quando si registra un model via
    # add_static_model. Se una classe statica estende CoreModel invece di
    # BasicModel, file_fields() manca e la registrazione esplode con
    # AttributeError (visto con MailTemplate: bug reale, mascherato per anni
    # da un .py stale in models_folder che teneva add_static_model in no-op).
    for _, model_class in app_env._STATIC_MODELS:
        assert callable(getattr(model_class, "file_fields", None)), (
            f"{model_class.__name__} must extend BasicModel (file_fields missing)"
        )
        model_class.file_fields()
        model_class.tranform_data_value()
        model_class.model_depends()


def test_register_static_models_clears_stale_dynamic_entry_before_registering():
    class _StaleModel:
        pass

    class _FakeOrm:
        def __init__(self, env):
            self._env = env
            self.calls = []

        async def add_static_model(self, name, model_class):
            # replica la guardia reale di ozon-env: no-op se il nome e'
            # gia' presente in env.models.
            self.calls.append((name, name in self._env.models))
            if name not in self._env.models:
                self._env.models[name] = model_class

    class _FakeEnv:
        def __init__(self):
            self.models = {"mail_template": _StaleModel()}
            self.orm = None

    env = _FakeEnv()
    env.orm = _FakeOrm(env)

    asyncio.run(app_env._register_static_models(env))

    # per ogni model statico, la entry preesistente va rimossa PRIMA della
    # chiamata ad add_static_model, altrimenti il no-op guard di ozon-env
    # lascerebbe in piedi la classe stale (dinamica) invece di quella statica.
    for name, was_present_when_called in env.orm.calls:
        assert was_present_when_called is False

    from app.core.models import MailTemplate

    assert env.models["mail_template"] is MailTemplate
