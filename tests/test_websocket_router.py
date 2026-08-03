import pytest
from fastapi import WebSocketDisconnect
from fastapi.testclient import TestClient

from app.app import app
from app.api.websocket_router import WsActionResult
from app.api.websocket_router import get_ws_action_runner
from app.api import websocket_router
from app.deps.app_env import WsAuthError
from app.services.common import ResponseObjectData
from app.services.cookie_auth import sign_token


class FakeRunner:
    """Runner WS senza ozon-env: auth canned + payload canned."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.auth_calls: list[dict] = []

    async def authenticate(self, token, app_code) -> str:
        self.auth_calls.append({"token": token, "app_code": app_code})
        if token == "bad" or not token:
            raise WsAuthError("Invalid session")
        return "u.test"

    async def run(self, token, app_code, action_name, rec_name, data):
        self.calls.append(
            {
                "action_name": action_name,
                "rec_name": rec_name,
                "data": data,
                "app_code": app_code,
            }
        )
        if action_name == "boom":
            raise RuntimeError("kaboom")
        if action_name == "fail_action":
            return WsActionResult(
                payload=ResponseObjectData(
                    mode="form",
                    data={"status": "error", "message": "azione fallita"},
                    model="action",
                ),
                next_action_url="",
            )
        payload = dict(data)
        if rec_name:
            payload["rec_name"] = rec_name
        return WsActionResult(
            payload=ResponseObjectData(
                mode="form", data=payload, rec_name=rec_name, model="action"
            ),
            next_action_url=f"/action/list_x/{rec_name}" if rec_name else "",
        )


def _client(runner: FakeRunner) -> TestClient:
    app.dependency_overrides[get_ws_action_runner] = lambda: runner
    return TestClient(app)


def test_ws_action_completed():
    runner = FakeRunner()
    client = _client(runner)
    try:
        with client.websocket_connect("/ws/actions?app_code=app1") as ws:
            ws.send_json({"type": "auth", "token": "ok"})
            ws.send_json(
                {
                    "request_id": "req1",
                    "action_name": "submit_calendar",
                    "rec_name": "task_123",
                    "data": {"form": {"a": 1}},
                }
            )
            ack = ws.receive_json()
            assert ack["request_id"] == "req1"
            assert ack["status"] == "running"

            final = ws.receive_json()
            assert final["request_id"] == "req1"
            assert final["type"] == "action_status"
            assert final["status"] == "completed"
            assert final["data"]["next_action_url"] == "/action/list_x/task_123"
            assert final["data"]["result"]["data"]["rec_name"] == "task_123"
    finally:
        app.dependency_overrides.clear()

    assert runner.calls[0]["action_name"] == "submit_calendar"
    assert runner.calls[0]["app_code"] == "app1"


def test_ws_action_authenticates_with_session_cookie():
    runner = FakeRunner()
    client = _client(runner)
    cookie_token = "cookie-session-token"
    client.cookies.set(
        websocket_router.settings.auth_cookie_name,
        sign_token(cookie_token, websocket_router.settings.session_secret),
    )
    origin = websocket_router.settings.external_base_url.rstrip("/")
    try:
        with client.websocket_connect(
            "/ws/actions",
            headers={"origin": origin},
        ) as ws:
            ws.send_json(
                {
                    "request_id": "cookie-1",
                    "action_name": "submit_calendar",
                    "data": {"form": {"a": 1}},
                }
            )
            assert ws.receive_json()["status"] == "running"
            assert ws.receive_json()["status"] == "completed"
    finally:
        app.dependency_overrides.clear()

    assert runner.auth_calls == [
        {
            "token": cookie_token,
            "app_code": websocket_router.settings.app_code,
        }
    ]


def test_ws_action_removes_legacy_payload_credentials():
    runner = FakeRunner()
    client = _client(runner)
    try:
        with client.websocket_connect("/ws/actions") as ws:
            ws.send_json({"type": "auth", "token": "ok"})
            ws.send_json(
                {
                    "request_id": "legacy-auth",
                    "action_name": "submit_calendar",
                    "data": {
                        "value": 1,
                        "authtoken": "legacy-secret",
                        "authToken": "legacy-secret-2",
                        "auth_token": "legacy-secret-3",
                    },
                }
            )
            assert ws.receive_json()["status"] == "running"
            assert ws.receive_json()["status"] == "completed"
    finally:
        app.dependency_overrides.clear()

    assert runner.calls[0]["data"] == {"value": 1}


def test_ws_action_error_payload():
    runner = FakeRunner()
    client = _client(runner)
    try:
        with client.websocket_connect("/ws/actions") as ws:
            ws.send_json({"type": "auth", "token": "ok"})
            ws.send_json(
                {"request_id": "r2", "action_name": "fail_action", "data": {}}
            )
            assert ws.receive_json()["status"] == "running"
            final = ws.receive_json()
            assert final["status"] == "error"
            assert final["message"] == "azione fallita"
    finally:
        app.dependency_overrides.clear()


def test_ws_action_runtime_exception():
    runner = FakeRunner()
    client = _client(runner)
    try:
        with client.websocket_connect("/ws/actions") as ws:
            ws.send_json({"type": "auth", "token": "ok"})
            ws.send_json({"request_id": "r3", "action_name": "boom", "data": {}})
            assert ws.receive_json()["status"] == "running"
            final = ws.receive_json()
            assert final["status"] == "error"
            assert "kaboom" in final["message"]
    finally:
        app.dependency_overrides.clear()


def test_ws_missing_action_name():
    runner = FakeRunner()
    client = _client(runner)
    try:
        with client.websocket_connect("/ws/actions") as ws:
            ws.send_json({"type": "auth", "token": "ok"})
            ws.send_json({"request_id": "r4", "data": {}})
            resp = ws.receive_json()
            assert resp["status"] == "error"
            assert "action_name" in resp["message"]
    finally:
        app.dependency_overrides.clear()
    assert runner.calls == []


def test_ws_auth_failure_closes():
    runner = FakeRunner()
    client = _client(runner)
    try:
        with client.websocket_connect("/ws/actions") as ws:
            ws.send_json({"type": "auth", "token": "bad"})
            with pytest.raises(WebSocketDisconnect):
                ws.receive_json()
    finally:
        app.dependency_overrides.clear()


def test_ws_auth_missing_message_closes():
    """Nessun cookie e primo messaggio non e' un auth valido -> chiusa."""
    runner = FakeRunner()
    client = _client(runner)
    try:
        with client.websocket_connect("/ws/actions") as ws:
            ws.send_json({"type": "not_auth"})
            with pytest.raises(WebSocketDisconnect):
                ws.receive_json()
    finally:
        app.dependency_overrides.clear()


def test_ws_token_in_query_string_is_ignored(monkeypatch):
    """Il fallback in query string e' stato rimosso: un token li' non
    autentica piu' — deve arrivare come primo messaggio WS. Timeout
    accorciato per non far durare il test 10s reali."""
    import app.api.websocket_router as wsr

    monkeypatch.setattr(wsr, "WS_AUTH_TIMEOUT_SECONDS", 0.05)
    runner = FakeRunner()
    client = _client(runner)
    try:
        with client.websocket_connect("/ws/actions?token=ok") as ws:
            with pytest.raises(WebSocketDisconnect):
                ws.receive_json()
    finally:
        app.dependency_overrides.clear()


def test_ws_origin_rejected(monkeypatch):
    import app.api.websocket_router as wsr

    monkeypatch.setattr(wsr.settings, "ws_allowed_origins", "https://ok.app")
    runner = FakeRunner()
    client = _client(runner)
    try:
        with client.websocket_connect(
            "/ws/actions", headers={"origin": "https://evil.app"}
        ) as ws:
            with pytest.raises(WebSocketDisconnect):
                ws.receive_json()
    finally:
        app.dependency_overrides.clear()


def test_ws_origin_allowed(monkeypatch):
    import app.api.websocket_router as wsr

    monkeypatch.setattr(wsr.settings, "ws_allowed_origins", "https://ok.app")
    runner = FakeRunner()
    client = _client(runner)
    try:
        with client.websocket_connect(
            "/ws/actions", headers={"origin": "https://ok.app"}
        ) as ws:
            ws.send_json({"type": "auth", "token": "ok"})
            ws.send_json({"request_id": "ro", "action_name": "x", "data": {}})
            assert ws.receive_json()["status"] == "running"
            assert ws.receive_json()["status"] == "completed"
    finally:
        app.dependency_overrides.clear()
