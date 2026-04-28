import asyncio
import base64
from datetime import datetime
from datetime import timedelta
from datetime import timezone
import json
from types import SimpleNamespace

from fastapi import Response
from ozonenv.core.BaseModels import Session
from starlette.requests import Request

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
        session_docs=None,
    ):
        self.params = {"current_session_token": "admin-token"}
        self.config_system = {"ozon_admin_token": "admin-token"}
        self.user_session = user_session or Session(
            uid="legacy-user",
            token="ok-token",
            app_code=user_app_code,
            expire_datetime=datetime.now(),
        )
        self.session_app_calls = 0
        self.db = SimpleNamespace(
            engine=_FakeEngine(
                {
                    "session": _FakeCollection(session_docs),
                    "user": _FakeCollection(user_docs),
                }
            )
        )
        self.orm = SimpleNamespace(
            app_settings=SimpleNamespace(
                upload_folder="/uploads",
                session_expire_hours=12,
                tz="Europe/Rome",
            )
        )
        self.upload_folder = ""

    async def session_app(self):
        self.session_app_calls += 1
        return _SessionResult()


class _FakeService:
    def __init__(self, env):
        self.session = env.user_session


def test_build_ozon_cfg_ignores_admin_env_token(monkeypatch):
    monkeypatch.setattr(
        app_env,
        "settings",
        SimpleNamespace(
            app_code="mci",
            mongo_user="user",
            mongo_pass="pass",
            mongo_url="mongodb://localhost:27017",
            mongo_db="test",
            mongo_replica="",
            models_folder="/models",
        ),
    )

    cfg = app_env._build_ozon_cfg()

    assert cfg["app_code"] == "mci"
    assert "ozon_admin_token" not in cfg


def test_client_session_sync_app_code_on_get_session(monkeypatch):
    monkeypatch.setattr(
        app_env,
        "settings",
        SimpleNamespace(
            auth_mode="token",
            keycloak_remote_user_header="x-remote-user",
        ),
    )
    monkeypatch.setattr(app_env, "_build_ozon_cfg", lambda: {"app_code": "mci"})
    monkeypatch.setattr(app_env, "Service", _FakeService)

    ozon_env = _FakeOzonEnv(user_app_code="legacy")
    response = Response()
    request = _build_request("/get_session")

    result = asyncio.run(
        app_env.client_session(
            "Bearer ok-token", ozon_env, request, response
        )
    )

    assert result.app_code == "mci"
    assert ozon_env.params["current_session_token"] == "ok-token"
    assert "ozon_admin_token" not in ozon_env.params
    assert "ozon_admin_token" not in ozon_env.config_system
    assert ozon_env.user_session.app_code == "mci"
    assert ozon_env.user_session.uid == "legacy-user"
    assert len(ozon_env.db.engine.get_collection("session").replace_calls) == 1
    assert ozon_env.session_app_calls == 1


def test_client_session_sync_app_code_on_other_paths(monkeypatch):
    monkeypatch.setattr(
        app_env,
        "settings",
        SimpleNamespace(
            auth_mode="token",
            keycloak_remote_user_header="x-remote-user",
        ),
    )
    monkeypatch.setattr(app_env, "_build_ozon_cfg", lambda: {"app_code": "mci"})
    monkeypatch.setattr(app_env, "Service", _FakeService)

    ozon_env = _FakeOzonEnv(user_app_code="legacy")
    response = Response()
    request = _build_request("/models/distinct")

    result = asyncio.run(
        app_env.client_session(
            "Bearer ok-token", ozon_env, request, response
        )
    )

    assert result.app_code == "mci"
    assert ozon_env.params["current_session_token"] == "ok-token"
    assert "ozon_admin_token" not in ozon_env.params
    assert "ozon_admin_token" not in ozon_env.config_system
    assert ozon_env.user_session.app_code == "mci"
    assert ozon_env.user_session.uid == "legacy-user"
    assert len(ozon_env.db.engine.get_collection("session").replace_calls) == 1
    assert ozon_env.session_app_calls == 1


def test_client_session_builds_keycloak_session_from_trusted_header(monkeypatch):
    monkeypatch.setattr(
        app_env,
        "settings",
        SimpleNamespace(
            auth_mode="keycloak",
            keycloak_remote_user_header="x-remote-user",
            token_header="Authorization",
            keycloak_token_endpoint="https://kc.example/token",
            keycloak_client_id="backend-web",
            keycloak_client_secret="secret",
        ),
    )
    monkeypatch.setattr(app_env, "_build_ozon_cfg", lambda: {"app_code": "mci"})
    monkeypatch.setattr(app_env, "Service", _FakeService)

    ozon_env = _FakeOzonEnv(
        user_session=None,
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
    response = Response()
    exp = int((datetime.now(timezone.utc) + timedelta(minutes=5)).timestamp())
    sso_token = _fake_jwt_with_exp(exp)
    request = _build_request(
        "/get_session",
        headers=[
            (b"x-remote-user", b"kc.user"),
            (b"authorization", f"Bearer {sso_token}".encode("utf-8")),
            (b"x-refresh-token", b"refresh-kc-token"),
        ],
    )

    result = asyncio.run(
        app_env.client_session(
            None,
            ozon_env,
            request,
            response,
        )
    )

    session_collection = ozon_env.db.engine.get_collection("session")

    assert result.app_code == "mci"
    assert ozon_env.params["current_session_token"] == result.api_key
    assert result.service.session.uid == "kc.user"
    assert result.service.session.full_name == "Keycloak User"
    assert result.service.session.is_admin is True
    assert result.service.session.sso_token == sso_token
    assert result.service.session.sso_refresh == "refresh-kc-token"
    assert result.service.session.sso_expire is not None
    assert len(session_collection.replace_calls) == 1
    assert session_collection.replace_calls[0]["payload"]["user"]["uid"] == "kc.user"
    assert session_collection.replace_calls[0]["payload"]["sso_refresh"] == "refresh-kc-token"
    assert "app_code=mci" in response.headers["set-cookie"]
