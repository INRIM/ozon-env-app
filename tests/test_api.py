import json
from typing import Any

from fastapi.testclient import TestClient
from pydantic import BaseModel

from app.api import routes as api_routes
from app.app import app
from app.deps.app_env import get_authed_env
from app.deps.app_env import get_ozon_env
from app.deps.app_env import get_service
from app.services.common import ResponseObject, ResponseObjectData

# --- MOCK MODELS ---


class FakeComponentRecord:
    def __init__(self, rec_name: str):
        self.rec_name = rec_name
        self.components = [{"key": "name", "type": "textfield"}]


def fake_make_response_object(model=None, mode="list", data=None, **kwargs):
    """Crea un VERO ResponseObject ma bypassa i limiti di Pydantic per i mock"""
    content_data = {
        "mode": mode,
        "data": data if data is not None else [],
        "model": getattr(model, "model_name", "") if model else "component",
        "editable": True,
        "rec_name": kwargs.get("rec_name", ""),
    }

    if "columns" in kwargs:
        content_data["columns"] = kwargs["columns"]
    if "total_count" in kwargs:
        content_data["total_count"] = kwargs["total_count"]

    envelope_data = {"content": content_data, "fail": False, "message": ""}

    envelope = ResponseObject.model_validate(envelope_data)

    # Aggiungiamo campi extra necessari alla logica di mock aggirando i vincoli Pydantic
    envelope.__dict__["query"] = kwargs.get("query", {})
    envelope.__dict__["batch_size"] = kwargs.get("batch_size", 500)
    envelope.__dict__["fields"] = kwargs.get("fields", [])
    envelope.__dict__["fields_obfuscate"] = kwargs.get("fields_obfuscate", [])

    return envelope


class FakeService:
    def __init__(self, instance_id: int):
        self.instance_id = instance_id

    async def get_models(self, query: dict = None):
        return ["customer", "order"]

    async def compo_by_name(self, model: str, name: str):
        record = FakeComponentRecord(name)
        return fake_make_response_object(
            model=None,
            mode="form",
            data={
                "components": record.components,
                "rec_name": record.rec_name,
            },
        )

    async def list_records(
        self,
        model_name: str,
        query: dict,
        order: str,
        skip: int,
        limit: int,
        resp_stream: bool = False,
        batch_size: int = 500,
    ):
        return fake_make_response_object(
            model=None,
            mode="list_stream" if resp_stream else "list",
            data=[],
            columns={"name": "Name"},
            query=query,
            total_count=57,
        )

    async def stream_record(
        self, envelope: ResponseObject, order: str, skip: int, limit: int
    ):
        # Restituiamo una lista semplice
        return [
            {
                "model": getattr(envelope.content, "model", "test"),
                "query": getattr(envelope, "query", {}),
                "order": order,
                "skip": skip,
                "limit": limit,
                "instance_id": self.instance_id,
                "_id": f"{self.instance_id:08d}",
            }
        ]

    async def load_record(self, model: str, rec_name: str):
        if rec_name == "not-found":
            return None
        return fake_make_response_object(
            model=None,
            mode="form",
            rec_name=rec_name,
            data={
                "id": "00000001",
                "rec_name": rec_name,
                "instance_id": self.instance_id,
            },
        )

    async def upsert(self, model: str, payload: dict, rec_name: str = ""):
        if rec_name == "mario":
            return None  # Simula un errore/mismatch
        return fake_make_response_object(
            model=None,
            mode="form",
            rec_name=rec_name,
            data={
                "status": payload.get("status"),
                "qty": payload.get("qty"),
                "rec_name": rec_name,
            },
        )


class FakeUserSession:
    def __init__(self, instance_id: int):
        self.token = f"ok-token-{instance_id}"
        self.user_uid = f"U-{instance_id}"
        self.app_code = "test-app"


class FakeOzonEnv:
    def __init__(self, instance_id: int):
        self.user_session = FakeUserSession(instance_id)


class _UnserializableValue:
    pass


class FailingJsonSession(BaseModel):
    token: str = "fallback-token"
    user_uid: str = "U-fallback"
    app_code: str = "test-app"
    raw: object = _UnserializableValue()


class OzonEnvFactory:
    def __init__(self):
        self.calls = 0

    async def dep(self):
        self.calls += 1
        yield FakeOzonEnv(self.calls)


class Factory:
    def __init__(self):
        self.calls = 0
        self._ozon_factory = OzonEnvFactory()

    async def dep(self):
        self.calls += 1
        yield FakeService(self.calls)

    async def fake_authed_env(self):
        pass


def make_client(factory: Factory):
    app.dependency_overrides[get_authed_env] = factory.fake_authed_env
    app.dependency_overrides[get_service] = factory.dep
    app.dependency_overrides[get_ozon_env] = factory._ozon_factory.dep
    return TestClient(app)


# --- TESTS ---


def test_models_distinct(monkeypatch):
    factory = Factory()
    monkeypatch.setattr(
        api_routes, "make_response_object", fake_make_response_object
    )
    client = make_client(factory)
    response = client.get(
        "/models/distinct", headers={"Authorization": "Bearer ok-token"}
    )
    assert response.status_code == 200
    data = response.json()["content"]["data"]
    assert "customer" in data
    assert "order" in data


def test_get_session():
    factory = Factory()
    client = make_client(factory)
    response = client.get(
        "/get_session", headers={"Authorization": "Bearer ok-token"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["token"] == "ok-token-1"
    assert body["user_uid"] == "U-1"
    assert body["app_code"] == "test-app"


def test_get_session_with_fallback_serializer():
    class FakeFailingOzonEnv:
        def __init__(self):
            self.user_session = FailingJsonSession()

    async def fake_authed_env():
        pass

    async def ozon_dep():
        yield FakeFailingOzonEnv()

    app.dependency_overrides[get_authed_env] = fake_authed_env
    app.dependency_overrides[get_ozon_env] = ozon_dep
    client = TestClient(app)
    response = client.get(
        "/get_session", headers={"Authorization": "Bearer ok-token"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["token"] == "fallback-token"
    assert body["user_uid"] == "U-fallback"
    assert body["app_code"] == "test-app"
    app.dependency_overrides.clear()


def test_post_models_distinct(monkeypatch):
    factory = Factory()
    monkeypatch.setattr(
        api_routes, "make_response_object", fake_make_response_object
    )
    client = make_client(factory)
    response = client.post(
        "/models/distinct",
        headers={"Authorization": "Bearer ok-token"},
        json={"data": {}, "properties": {"domain": {"active": True}}},
    )
    assert response.status_code == 200
    data = response.json()["content"]["data"]
    assert "customer" in data


def test_record_schema():
    factory = Factory()
    client = make_client(factory)
    response = client.get(
        "/record/customer", headers={"Authorization": "Bearer ok-token"}
    )
    assert response.status_code == 200
    assert response.json()["content"]["model"] == "component"
    assert "components" in response.json()["content"]["data"]


def test_list_records(monkeypatch):
    factory = Factory()
    monkeypatch.setattr(
        api_routes, "make_response_object", fake_make_response_object
    )

    # --- MOCK SPECIFICO PER EVITARE IL TAGLIO DEL BUFFER DEL TESTCLIENT ---
    async def fake_stream(data_cursor, meta):
        # Genera il primo blocco (Start Packet)
        yield f"{meta.model_dump_json()}\n"
        # Genera i blocchi successivi (Righe)
        for item in data_cursor:
            yield f"{json.dumps(item)}\n"

    monkeypatch.setattr(
        api_routes, "_stream_ndjson_with_start_packet", fake_stream
    )
    # ----------------------------------------------------------------------

    client = make_client(factory)

    response = client.post(
        "/list/customer",
        headers={"Authorization": "Bearer ok-token"},
        json={
            "query": {"active": True},
            "order": "-created_at",
            "skip": 5,
            "limit": 2,
        },
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/x-ndjson")
    assert response.headers["x-total-count"] == "57"

    lines = [line for line in response.text.splitlines() if line.strip()]
    assert len(lines) == 2  # Prima riga: Envelope, Seconda riga: Dati

    # 1. Start Packet (ResponseObject Envelope)
    envelope = json.loads(lines[0])
    assert "content" in envelope
    assert envelope["content"]["mode"] == "list_stream"
    assert envelope["content"]["total_count"] == 57

    # 2. Record Row
    row = json.loads(lines[1])
    assert row["query"] == {"active": True}
    assert row["order"] == "-created_at"


def test_list_records_total_count(monkeypatch):
    """Verifica esplicita del totale record su header + envelope NDJSON."""
    factory = Factory()
    monkeypatch.setattr(
        api_routes, "make_response_object", fake_make_response_object
    )

    async def fake_stream(data_cursor, meta):
        yield f"{meta.model_dump_json()}\n"
        for item in data_cursor:
            yield f"{json.dumps(item)}\n"

    monkeypatch.setattr(
        api_routes, "_stream_ndjson_with_start_packet", fake_stream
    )

    client = make_client(factory)
    response = client.post(
        "/list/customer",
        headers={"Authorization": "Bearer ok-token"},
        json={
            "query": {"active": True},
            "order": "-created_at",
            "skip": 0,
            "limit": 10,
        },
    )
    assert response.status_code == 200
    assert response.headers["x-total-count"] == "57"

    lines = [line for line in response.text.splitlines() if line.strip()]
    envelope = json.loads(lines[0])
    assert envelope["content"]["total_count"] == 57


def test_record_by_name():
    factory = Factory()
    client = make_client(factory)
    response = client.get(
        "/record/customer/john",
        headers={"Authorization": "Bearer ok-token"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["content"]["rec_name"] == "john"
    assert body["content"]["data"]["id"] == "00000001"


def test_record_by_name_not_found():
    factory = Factory()
    client = make_client(factory)
    response = client.get(
        "/record/customer/not-found",
        headers={"Authorization": "Bearer ok-token"},
    )
    assert response.status_code == 404


def test_update_record_by_name():
    factory = Factory()
    client = make_client(factory)
    response = client.post(
        "/record/customer/john",
        headers={"Authorization": "Bearer ok-token"},
        json={"status": "updated", "qty": 3},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["content"]["rec_name"] == "john"
    assert body["content"]["data"]["status"] == "updated"
    assert body["content"]["data"]["qty"] == 3


def test_update_record_rec_name_mismatch():
    factory = Factory()
    client = make_client(factory)
    response = client.post(
        "/record/customer/mario",
        headers={"Authorization": "Bearer ok-token"},
        json={"rec_name": "mario", "status": "updated"},
    )
    assert response.status_code == 404


def test_single_backend_instance_per_request(monkeypatch):
    factory = Factory()
    monkeypatch.setattr(
        api_routes, "make_response_object", fake_make_response_object
    )
    client = make_client(factory)

    response = client.get(
        "/models/distinct", headers={"Authorization": "Bearer ok-token"}
    )
    assert response.status_code == 200
    assert factory.calls == 1

    response = client.get(
        "/record/customer", headers={"Authorization": "Bearer ok-token"}
    )
    assert response.status_code == 200
    assert factory.calls == 2

    app.dependency_overrides.clear()


def test_get_remote_data_select(monkeypatch):
    factory = Factory()
    monkeypatch.setattr(
        api_routes, "make_response_object", fake_make_response_object
    )
    client = make_client(factory)

    async def fake_remote_data_select_response(
        service, url, path_value, header_key, header_value_key
    ):
        return [{"label": "One", "value": "1"}]

    monkeypatch.setattr(
        api_routes,
        "remote_data_select_response",
        fake_remote_data_select_response,
    )

    response = client.post(
        "/get_remote_select",
        headers={"Authorization": "Bearer ok-token"},
        json={"data": {"url": "https://example.org"}, "properties": {}},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["content"]["mode"] == "list"
    assert body["content"]["data"] == [{"label": "One", "value": "1"}]
    app.dependency_overrides.clear()


def test_openapi_documents_security_scheme_and_fixed_app_code():
    from app.app_settings import get_env_settings
    from app.services.session_auth import AUTH_MODE_KEYCLOAK, normalize_auth_mode

    current_auth_mode = normalize_auth_mode(get_env_settings().auth_mode)

    app.openapi_schema = None
    client = TestClient(app)

    response = client.get("/openapi.json")
    assert response.status_code == 200
    schema = response.json()

    security_scheme = schema["components"]["securitySchemes"]["APIKeyHeader"]
    if current_auth_mode == AUTH_MODE_KEYCLOAK:
        assert "Keycloak mode" in security_scheme["description"]
    else:
        assert "Single-token mode" in security_scheme["description"]

    get_session_schema = (
        schema["paths"]["/get_session"]["get"]["responses"]["200"]["content"][
            "application/json"
        ]["schema"]
    )
    assert "app_code" in get_session_schema["properties"]
    assert (
        "Fixed server app code"
        in get_session_schema["properties"]["app_code"]["description"]
    )
