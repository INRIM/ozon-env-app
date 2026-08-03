from fastapi.testclient import TestClient

from app.app import app
from app.deps.app_env import get_authed_env
from app.deps.app_env import get_service
from app.services.common import ResponseObjectData


class FakeActionService:
    async def service_get_menu(self, parent: str = "") -> ResponseObjectData:
        return ResponseObjectData(
            mode="menu",
            data=[
                {
                    "group_id": parent,
                    "title": "Main",
                    "buttons": [{"label": "Open", "content": "/action/open"}],
                }
            ],
        )

    async def service_get_dashboard(self, parent: str = "") -> ResponseObjectData:
        return ResponseObjectData(
            mode="card",
            data=[{"group_id": parent, "title": "Dashboard", "buttons": []}],
        )

    async def service_get_layout(self, name: str = "") -> ResponseObjectData:
        return ResponseObjectData(
            mode="layout",
            data={
                "layout": name or "standard",
                "schema": {"rec_name": name or "standard", "type": "layout"},
                "menu": [],
            },
        )

    async def service_handle_action_get(
            self,
            action_name: str,
            rec_name: str = "",
            query: dict | None = None,
            order: str = "",
            skip: int = 0,
            limit: int = 100,
    ) -> ResponseObjectData:
        if action_name == "missing_action":
            return ResponseObjectData(
                mode="action",
                data={
                    "status": "error",
                    "message": "Action 'missing_action' not found",
                },
                model="action",
            )
        if rec_name:
            return ResponseObjectData(
                mode="form",
                data={"action": action_name, "rec_name": rec_name, "query": query or {}},
                rec_name=rec_name,
                model="action",
            )
        return ResponseObjectData(
            mode="list",
            data=[{"action": action_name, "query": query or {}, "order": order, "skip": skip, "limit": limit}],
            model="action",
        )

    async def service_handle_action_post(
            self,
            action_name: str,
            data: dict,
            rec_name: str = "",
    ) -> ResponseObjectData:
        payload = data.copy()
        if rec_name:
            payload["rec_name"] = rec_name
        return ResponseObjectData(mode="form", data=payload, rec_name=rec_name, model="action")

    async def service_handle_action_delete(
            self,
            action_name: str,
            rec_name: str,
            data: dict | None = None,
    ) -> ResponseObjectData:
        return ResponseObjectData(
            mode="action",
            data={
                "status": "ok",
                "action_name": action_name,
                "rec_name": rec_name,
                "deleted": True,
                "payload": data or {},
            },
            rec_name=rec_name,
            model="action",
        )

    async def service_get_next_action_redirect(
            self,
            curr_action: str,
            rec_name: str = "",
    ) -> str:
        if curr_action == "no_next":
            return ""
        base = "/action/submit_action"
        if rec_name:
            return f"{base}/{rec_name}"
        return base


async def _fake_authed_env():
    pass


async def _dep():
    yield FakeActionService()


def _client() -> TestClient:
    app.dependency_overrides[get_authed_env] = _fake_authed_env
    app.dependency_overrides[get_service] = _dep
    return TestClient(app)


def test_action_menu_endpoint():
    client = _client()
    res = client.get("/action/menu", headers={"Authorization": "Bearer ok-token"})
    assert res.status_code == 200
    body = res.json()
    assert body["fail"] is False
    assert body["content"]["mode"] == "menu"
    assert isinstance(body["content"]["data"], list)
    app.dependency_overrides.clear()


def test_action_layout_endpoint():
    client = _client()
    res = client.get(
        "/action/layout/standard",
        headers={"Authorization": "Bearer ok-token"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["content"]["mode"] == "layout"
    assert body["content"]["data"]["layout"] == "standard"
    app.dependency_overrides.clear()


def test_action_get_list_endpoint():
    client = _client()
    res = client.get(
        "/action/list_action",
        headers={"Authorization": "Bearer ok-token"},
        params={"query": "{}", "order": "-create_datetime", "skip": 2, "limit": 5},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["content"]["mode"] == "list"
    assert body["content"]["data"][0]["action"] == "list_action"
    app.dependency_overrides.clear()


def test_action_get_form_endpoint():
    client = _client()
    res = client.get(
        "/action/form_action/REC001",
        headers={"Authorization": "Bearer ok-token"},
        params={"query": "{}"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["content"]["mode"] == "form"
    assert body["content"]["rec_name"] == "REC001"
    app.dependency_overrides.clear()


def test_action_post_endpoint():
    client = _client()
    res = client.post(
        "/action/save_action/REC100",
        headers={"Authorization": "Bearer ok-token"},
        json={"status": "updated"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["content"]["mode"] == "form"
    assert body["content"]["data"]["status"] == "updated"
    assert body["content"]["data"]["rec_name"] == "REC100"
    app.dependency_overrides.clear()


def test_action_post_removes_legacy_payload_credentials():
    client = _client()
    res = client.post(
        "/action/save_action/REC100",
        headers={"Authorization": "Bearer transport-token"},
        json={
            "status": "updated",
            "authtoken": "legacy-secret",
            "authToken": "legacy-secret-2",
            "auth_token": "legacy-secret-3",
        },
    )

    assert res.status_code == 200
    data = res.json()["content"]["data"]
    assert data["status"] == "updated"
    assert "authtoken" not in data
    assert "authToken" not in data
    assert "auth_token" not in data
    app.dependency_overrides.clear()


def test_action_delete_endpoint():
    client = _client()
    res = client.request(
        "DELETE",
        "/action/delete_action/RECDEL",
        headers={"Authorization": "Bearer ok-token"},
        json={"reason": "manual"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["content"]["mode"] == "action"
    assert body["content"]["data"]["deleted"] is True
    assert body["content"]["data"]["rec_name"] == "RECDEL"
    app.dependency_overrides.clear()


def test_action_query_validation():
    client = _client()
    res = client.get(
        "/action/list_action",
        headers={"Authorization": "Bearer ok-token"},
        params={"query": "not-a-json"},
    )
    assert res.status_code == 422
    app.dependency_overrides.clear()


def test_action_dashboard_endpoint():
    client = _client()
    res = client.get(
        "/action/dashboard",
        headers={"Authorization": "Bearer ok-token"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["content"]["mode"] == "card"
    assert isinstance(body["content"]["data"], list)
    app.dependency_overrides.clear()


def test_action_error_is_exposed_in_response_object():
    client = _client()
    res = client.get(
        "/action/missing_action",
        headers={"Authorization": "Bearer ok-token"},
        params={"query": "{}"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["fail"] is True
    assert body["message"] == "Action 'missing_action' not found"
    assert body["content"]["mode"] == "action"
    assert body["content"]["data"]["status"] == "error"
    app.dependency_overrides.clear()


def test_action_next_action_redirect_with_rec_name():
    client = _client()
    res = client.get(
        "/action/next_action/form_action/REC001",
        headers={"Authorization": "Bearer ok-token"},
    )
    assert res.status_code == 200
    assert res.json() == {
        "mode": "redirect",
        "data": {
            "next_page": "/action/submit_action/REC001",
        },
    }
    app.dependency_overrides.clear()


def test_action_next_action_redirect_without_rec_name():
    client = _client()
    res = client.get(
        "/action/next_action/form_action",
        headers={"Authorization": "Bearer ok-token"},
    )
    assert res.status_code == 200
    assert res.json() == {
        "mode": "redirect",
        "data": {
            "next_page": "/action/submit_action",
        },
    }
    app.dependency_overrides.clear()


def test_action_next_action_redirect_no_target():
    client = _client()
    res = client.get(
        "/action/next_action/no_next/REC001",
        headers={"Authorization": "Bearer ok-token"},
        follow_redirects=False,
    )
    assert res.status_code == 204
    assert res.text == ""
    app.dependency_overrides.clear()
