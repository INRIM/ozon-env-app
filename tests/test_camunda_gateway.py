from __future__ import annotations

from fastapi.testclient import TestClient
import httpx

from app.app import app
from app.app_settings import EnvSettings
from app.deps.app_env import get_authed_env
from app.deps.app_env import get_service
from app.services.camunda import Camunda8Gateway
from app.services.camunda import _sdk_value


class FakeService:
    def __init__(self):
        self.calls = []

    async def start_camunda_gateway_process(
        self,
        process_key,
        payload=None,
        *,
        update_data=False,
        process_model="",
    ):
        self.calls.append(
            ("start", process_key, payload or {}, update_data, process_model)
        )
        return {
            "stato": {"status": "started"},
            "variables": payload or {},
            "process_id": "proc-1",
        }

    async def get_camunda_gateway_status(self, process_id):
        self.calls.append(("status", process_id))
        return {
            "stato": {"status": "running", "process_id": process_id},
            "variables": {},
        }

    async def complete_camunda_gateway_task(
        self,
        process_id,
        payload=None,
        *,
        decision="",
    ):
        self.calls.append(("complete", process_id, payload or {}, decision))
        variables = dict((payload or {}).get("var", {}))
        if decision == "approved":
            variables["approved"] = True
        return {
            "stato": {"status": "completed", "decision": decision},
            "variables": variables,
        }

    async def complete_many_camunda_gateway_tasks(
        self, payload=None, *, decision=""
    ):
        self.calls.append(("complete_many", payload or {}, decision))
        rec_names = (payload or {}).get("rec_names") or []
        return {
            "stato": {
                "status": "ok",
                "decision": decision,
                "total": len(rec_names),
                "completed": len(rec_names),
            },
            "results": [
                {"rec_name": rn, "status": "ok"} for rn in rec_names
            ],
        }


class MissingProcessService(FakeService):
    async def start_camunda_gateway_process(
        self,
        process_key,
        payload=None,
        *,
        update_data=False,
        process_model="",
    ):
        raise LookupError(
            f"Camunda process definition '{process_key}' is not deployed"
        )


def test_camunda_gateway_router_endpoints():
    fake = FakeService()
    app.dependency_overrides[get_authed_env] = lambda: object()
    app.dependency_overrides[get_service] = lambda: fake
    try:
        client = TestClient(app)

        started = client.post(
            "/gateway/camunda/start/approve_request",
            json={"amount": 10},
        )
        started_with_update = client.post(
            "/gateway/camunda/start/approve_request?update-data=true",
            json={"rec_name": "req-1", "amount": 12},
        )
        started_for_model = client.post(
            "/gateway/camunda/start/request/approve_request?update_data=true",
            json={"rec_name": "req-2", "amount": 14},
        )
        status = client.get("/gateway/camunda/status/proc-1")
        approved = client.post(
            "/gateway/camunda/action/proc-1/approved",
            json={"var": {"x": 1}},
        )

        assert started.status_code == 200
        assert started.json()["process_id"] == "proc-1"
        assert started_with_update.status_code == 200
        assert started_for_model.status_code == 200
        assert status.status_code == 200
        assert status.json()["stato"]["status"] == "running"
        assert approved.status_code == 200
        assert approved.json()["variables"] == {"x": 1, "approved": True}
        assert fake.calls == [
            ("start", "approve_request", {"amount": 10}, False, ""),
            (
                "start",
                "approve_request",
                {"rec_name": "req-1", "amount": 12},
                True,
                "",
            ),
            (
                "start",
                "approve_request",
                {"rec_name": "req-2", "amount": 14},
                True,
                "request",
            ),
            ("status", "proc-1"),
            ("complete", "proc-1", {"var": {"x": 1}}, "approved"),
        ]
    finally:
        app.dependency_overrides.clear()


def test_camunda_gateway_router_returns_404_when_process_is_not_deployed():
    app.dependency_overrides[get_authed_env] = lambda: object()
    app.dependency_overrides[get_service] = lambda: MissingProcessService()
    try:
        client = TestClient(app)

        response = client.post(
            "/gateway/camunda/start/Test_Process",
            json={"rec_name": "req-1"},
        )

        assert response.status_code == 404
        assert "not deployed" in response.json()["detail"]
    finally:
        app.dependency_overrides.clear()


def test_camunda_router_batch_endpoints():
    fake = FakeService()
    app.dependency_overrides[get_authed_env] = lambda: object()
    app.dependency_overrides[get_service] = lambda: fake
    try:
        client = TestClient(app)
        body = {"model": "test_request", "rec_names": ["r1", "r2"]}
        approve = client.post(
            "/gateway/camunda/action/approve_many", json=body
        )
        refuse = client.post(
            "/gateway/camunda/action/refuse_many", json=body
        )
        complete = client.post("/gateway/camunda/complete_many", json=body)

        assert approve.status_code == 200
        assert approve.json()["stato"]["total"] == 2
        assert refuse.status_code == 200
        assert complete.status_code == 200
        # ogni endpoint delega al batch col decision giusto
        assert ("complete_many", body, "approved") in fake.calls
        assert ("complete_many", body, "refused") in fake.calls
        assert ("complete_many", body, "") in fake.calls
    finally:
        app.dependency_overrides.clear()


def test_camunda_router_resolves_process_id_from_payload():
    fake = FakeService()
    app.dependency_overrides[get_authed_env] = lambda: object()
    app.dependency_overrides[get_service] = lambda: fake
    try:
        client = TestClient(app)
        # niente process_id nel path: si legge dal form del payload
        complete = client.post(
            "/gateway/camunda/complete",
            json={"form": {"process_id": "proc-42"}, "var": {"x": 1}},
        )
        approved = client.post(
            "/gateway/camunda/action/approved",
            json={"form": {"process_id": "proc-99"}},
        )
        assert complete.status_code == 200
        assert approved.status_code == 200
        assert (
            "complete",
            "proc-42",
            {"form": {"process_id": "proc-42"}, "var": {"x": 1}},
            "",
        ) in fake.calls
        assert (
            "complete",
            "proc-99",
            {"form": {"process_id": "proc-99"}},
            "approved",
        ) in fake.calls
    finally:
        app.dependency_overrides.clear()


def _to_dict(value):
    # variables passate al SDK sono model con to_dict(); normalizza per gli assert.
    if hasattr(value, "to_dict"):
        return value.to_dict()
    return value


class _SdkResult:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _FakeSdkClient:
    """Client SDK fake (sync, context manager) iniettato nel gateway."""

    def __init__(self, calls):
        self._calls = calls

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def create_process_instance(self, *, data):
        self._calls.append(
            (
                "create",
                data.process_definition_id,
                _to_dict(data.variables),
                _sdk_value(getattr(data, "tenant_id", None)),
            )
        )
        return _SdkResult(process_instance_key="proc-1")

    def search_user_tasks(self, *, data):
        flt = data.filter_
        self._calls.append(
            (
                "search",
                flt.process_instance_key,
                flt.state,
                _sdk_value(getattr(flt, "tenant_id", None)),
            )
        )
        return _SdkResult(
            items=[_SdkResult(user_task_key="task-9", element_id="resp_see")]
        )

    def complete_user_task(self, task_key, *, data):
        self._calls.append(("complete", task_key, _to_dict(data.variables)))

    def search_variables(self, *, data, truncate_values=True):
        # value JSON-encoded come Camunda 8
        import json as _json

        return _SdkResult(
            items=[
                _SdkResult(name="last_task", value=_json.dumps("sed_message_approved")),
                _SdkResult(
                    name="sed_message_approved",
                    value=_json.dumps(
                        {"next_action": "redirect", "next_page": "list_x"}
                    ),
                ),
            ]
        )


def test_get_process_variables_parses_json():
    import asyncio

    settings = EnvSettings(
        app_code="demo",
        CAMUNDA_TASKLIST_URL="http://camunda:8080",
        CAMUNDA_ENABLED=True,
    )
    gateway = Camunda8Gateway(
        settings, sdk_client_factory=lambda: _FakeSdkClient([])
    )
    vars_ = asyncio.run(gateway.get_process_variables("proc-1"))
    assert vars_["last_task"] == "sed_message_approved"
    assert vars_["sed_message_approved"] == {
        "next_action": "redirect",
        "next_page": "list_x",
    }


def test_camunda_in_process_gateway_uses_sdk():
    """Il gateway usa il SDK Camunda (qui fake): start/search/complete via SDK,
    niente HTTP manuale."""
    calls = []
    settings = EnvSettings(
        app_code="demo",
        CAMUNDA_TASKLIST_URL="http://camunda:8080",
        CAMUNDA_ENABLED=True,
    )
    gateway = Camunda8Gateway(
        settings, sdk_client_factory=lambda: _FakeSdkClient(calls)
    )

    import asyncio

    process_id = asyncio.run(
        gateway.start_process_raw(process_id="demo_process", variables={"x": 1})
    )
    asyncio.run(
        gateway.complete_task(
            process_instance_key="proc-1", variables={"done": True}
        )
    )
    status = asyncio.run(gateway.process_status("proc-1"))

    assert process_id == "proc-1"
    assert status["status"] == "running"
    assert status["tasks"] == [
        {
            "user_task_key": "task-9",
            "element_id": "resp_see",
            "process_instance_key": None,
        }
    ]
    kinds = [c[0] for c in calls]
    assert kinds == ["create", "search", "complete", "search"]
    assert ("create", "demo_process", {"x": 1}, None) in calls
    assert ("complete", "task-9", {"done": True}) in calls


def test_camunda_gateway_not_deployed_maps_to_lookup_error():
    import asyncio

    class _RaisingClient(_FakeSdkClient):
        def create_process_instance(self, *, data):
            raise RuntimeError("404 process definition not found")

    settings = EnvSettings(
        app_code="demo",
        CAMUNDA_TASKLIST_URL="http://camunda:8080",
        CAMUNDA_ENABLED=True,
    )
    gateway = Camunda8Gateway(
        settings, sdk_client_factory=lambda: _RaisingClient([])
    )

    try:
        asyncio.run(
            gateway.start_process_raw(process_id="missing", variables={})
        )
        raise AssertionError("expected LookupError")
    except LookupError as exc:
        assert "not deployed" in str(exc)


def test_camunda_gateway_sdk_config_rest_address_and_auth():
    # base senza /v2 -> aggiunge /v2; auth disabilitata -> strategy NONE.
    settings = EnvSettings(
        app_code="demo",
        CAMUNDA_TASKLIST_URL="http://camunda:8080",
        CAMUNDA_ENABLED=True,
        CAMUNDA_AUTH_ENABLED=False,
    )
    cfg = Camunda8Gateway(settings)._sdk_configuration()
    assert cfg["CAMUNDA_REST_ADDRESS"] == "http://camunda:8080/v2"
    assert cfg["CAMUNDA_AUTH_STRATEGY"] == "NONE"

    # base gia' con /v2 -> non duplica; auth abilitata -> OAUTH.
    settings_v2 = EnvSettings(
        app_code="demo",
        CAMUNDA_TASKLIST_URL="http://tasklist/v2",
        CAMUNDA_ENABLED=True,
        CAMUNDA_AUTH_ENABLED=True,
    )
    cfg_v2 = Camunda8Gateway(settings_v2)._sdk_configuration()
    assert cfg_v2["CAMUNDA_REST_ADDRESS"] == "http://tasklist/v2"
    assert cfg_v2["CAMUNDA_AUTH_STRATEGY"] == "OAUTH"


class _WaitSdkClient:
    """Client fake per wait_until_settled: sequenza di stati + task."""

    def __init__(self, *, states, task_sequences):
        # states: lista di stati process restituiti in ordine
        # task_sequences: lista di liste task (user_task_key) in ordine
        self._states = list(states)
        self._tasks = list(task_sequences)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_process_instance(self, key):
        state = self._states.pop(0) if self._states else "ACTIVE"
        return _SdkResult(state=state)

    def search_user_tasks(self, *, data):
        items = self._tasks.pop(0) if self._tasks else []
        return _SdkResult(
            items=[_SdkResult(user_task_key=k, element_id="x") for k in items]
        )


def _wait_gateway(client):
    settings = EnvSettings(
        app_code="demo",
        CAMUNDA_TASKLIST_URL="http://camunda:8080",
        CAMUNDA_ENABLED=True,
    )
    return Camunda8Gateway(settings, sdk_client_factory=lambda: client)


def test_wait_until_settled_returns_on_new_user_task():
    import asyncio

    # external task in volo: 1° giro nessun task nuovo, 2° giro appare task-new
    client = _WaitSdkClient(
        states=["ACTIVE", "ACTIVE"],
        task_sequences=[["old-task"], ["old-task", "new-task"]],
    )
    res = asyncio.run(
        _wait_gateway(client).wait_until_settled(
            "proc-1",
            exclude_task_key="old-task",
            timeout_seconds=5,
            interval_seconds=0.01,
        )
    )
    assert res["settled"] is True
    assert res["reason"] == "user_task"
    assert [t["user_task_key"] for t in res["tasks"]] == ["new-task"]


def test_wait_until_settled_returns_on_completed():
    import asyncio

    client = _WaitSdkClient(states=["COMPLETED"], task_sequences=[[]])
    res = asyncio.run(
        _wait_gateway(client).wait_until_settled(
            "proc-1", timeout_seconds=5, interval_seconds=0.01
        )
    )
    assert res["settled"] is True
    assert res["reason"] == "completed"


def test_wait_until_settled_timeout():
    import asyncio

    # sempre ACTIVE, nessun task nuovo -> timeout
    client = _WaitSdkClient(
        states=["ACTIVE"] * 50, task_sequences=[[]] * 50
    )
    res = asyncio.run(
        _wait_gateway(client).wait_until_settled(
            "proc-1", timeout_seconds=0.05, interval_seconds=0.01
        )
    )
    assert res["settled"] is False
    assert res["reason"] == "timeout"
