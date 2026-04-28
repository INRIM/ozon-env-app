import asyncio
import base64
from datetime import datetime
from datetime import timedelta
from datetime import timezone
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
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


class _FakeEngine:
    def __init__(self, collections):
        self.collections = collections

    def get_collection(self, name):
        return self.collections[name]


class _FakeOzonEnv:
    def __init__(self, user_docs=None, session_docs=None):
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
        self.user_session = None
        self.session_token = ""
        self.upload_folder = ""


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
        session_docs=[],
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

    persisted = env.db.engine.get_collection("session").replace_calls[0]["payload"]
    assert session.sso_token == access_token
    assert session.sso_refresh == "refresh-1"
    assert session.sso_expire is not None
    assert persisted["sso_refresh"] == "refresh-1"
    assert session.uid == "kc.user"


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
    assert len(env.db.engine.get_collection("session").replace_calls) == 1


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
