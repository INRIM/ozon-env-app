import json
import types

from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import BaseModel

from app.api import routes as api_routes
from app.app import app
from app.app_settings import EnvSettings
from app.core.models import AttachmentScanStatus
from app.deps.app_env import get_authed_env
from app.deps.app_env import get_ozon_env
from app.deps.app_env import get_service
from app.services.common import ResponseObject
from app.services.service import Service

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
    def __init__(self, instance_id: int, factory=None):
        self.instance_id = instance_id
        self.factory = factory

    async def get_models(self, query: dict = None):
        return ["customer", "order"]

    async def get_select_options(self, key: str, curr_model: str):
        if self.factory is not None:
            self.factory.last_select_options_call = {
                "key": key,
                "curr_model": curr_model,
            }
        return [{"label": "One", "value": "1"}]

    async def run_calendar_task(self, rec_name: str, payload: dict = None):
        if self.factory is not None:
            self.factory.last_calendar_run_call = {
                "rec_name": rec_name,
                "payload": dict(payload or {}),
            }
        return {
            "status": "ok",
            "rec_name": rec_name,
            "task": "update_model_access",
            "task_record_name": "",
            "run_id": (payload or {}).get("run_id", ""),
            "started_at": "2026-06-15T10:00:00+02:00",
            "finished_at": "2026-06-15T10:00:01+02:00",
            "message": "",
        }

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

    async def upsert(
        self,
        model: str,
        payload: dict,
        rec_name: str = "",
        sync_component_runtime: bool = False,
        generate_component_defaults: bool = False,
    ):
        if self.factory is not None:
            self.factory.last_upsert_call = {
                "model": model,
                "payload": payload.copy(),
                "rec_name": rec_name,
                "sync_component_runtime": sync_component_runtime,
                "generate_component_defaults": generate_component_defaults,
            }
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
        self.user_data = {"language": "it"}
        self.user = {"uid": self.user_uid}

    def get_dict(self) -> dict:
        return dict(self.__dict__)


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

    def get_dict(self) -> dict:
        # esclude il campo non serializzabile (come il vecchio encode fallback).
        return self.model_dump(exclude={"raw"})


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
        self.last_upsert_call = None
        self.last_calendar_run_call = None
        self.last_select_options_call = None

    async def dep(self):
        self.calls += 1
        yield FakeService(self.calls, self)

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


def test_calendar_task_run_endpoint():
    factory = Factory()
    client = make_client(factory)
    response = client.post(
        "/client/run/calendar_tasks/update_model_access",
        headers={"Authorization": "Bearer ok-token"},
        json={
            "run_id": "run-1",
            "scheduled_time": "2026-06-15T10:00:00+02:00",
            "trigger": "scheduler",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["rec_name"] == "update_model_access"
    assert factory.last_calendar_run_call == {
        "rec_name": "update_model_access",
        "payload": {
            "run_id": "run-1",
            "scheduled_time": "2026-06-15T10:00:00+02:00",
            "trigger": "scheduler",
        },
    }
    app.dependency_overrides.clear()


def test_get_session():
    factory = Factory()
    client = make_client(factory)
    response = client.get(
        "/get_session", headers={"Authorization": "Bearer ok-token"}
    )
    assert response.status_code == 200
    body = response.json()
    # token/sso_token/sso_refresh/claims non devono mai comparire nella
    # risposta di /get_session (vedi docs/SECURITY_KEYCLOAK_TOKEN_ANALYSIS.it.md).
    assert "token" not in body
    assert "sso_token" not in body
    assert "sso_refresh" not in body
    assert "claims" not in body
    assert body["user_uid"] == "U-1"
    assert body["uid"] == "U-1"
    assert body["username"] == "U-1"
    assert body["authenticated"] is True
    assert body["app_code"] == "test-app"
    assert body["user"]["uid"] == "U-1"
    assert body["user"]["username"] == "U-1"
    assert body["user"]["user_data"] == {"language": "it"}


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
    assert "token" not in body
    assert body["uid"] == "U-fallback"
    assert body["username"] == "U-fallback"
    assert body["authenticated"] is True
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


def test_list_records_non_stream_returns_json(monkeypatch):
    factory = Factory()
    monkeypatch.setattr(
        api_routes, "make_response_object", fake_make_response_object
    )
    client = make_client(factory)

    response = client.post(
        "/list/customer?stream=false",
        headers={"Authorization": "Bearer ok-token"},
        json={
            "query": {"active": True},
            "order": "-created_at",
            "skip": 5,
            "limit": 2,
        },
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    body = response.json()
    assert body["content"]["mode"] == "list"
    assert body["content"]["total_count"] == 57
    assert body["content"]["data"] == []


def test_list_records_accepts_stringified_json_body(monkeypatch):
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
        "/list/component",
        headers={"Authorization": "Bearer ok-token"},
        json='{"order":"","skip":0,"limit":1000000,"query":{"type":"resource"}}',
    )

    assert response.status_code == 200
    lines = [line for line in response.text.splitlines() if line.strip()]
    envelope = json.loads(lines[0])
    row = json.loads(lines[1])
    assert envelope["content"]["mode"] == "list_stream"
    assert row["query"] == {"type": "resource"}
    assert row["limit"] == 1000000


def test_list_records_accepts_double_encoded_json_body(monkeypatch):
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
        "/list/component",
        headers={"Authorization": "Bearer ok-token"},
        json='"{\\"order\\":\\"\\",\\"skip\\":0,\\"limit\\":1000000,\\"query\\":{\\"type\\":\\"resource\\"}}"',
    )

    assert response.status_code == 200
    lines = [line for line in response.text.splitlines() if line.strip()]
    row = json.loads(lines[1])
    assert row["query"] == {"type": "resource"}
    assert row["limit"] == 1000000


def test_list_records_accepts_text_plain_json_body(monkeypatch):
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
        "/list/component",
        headers={
            "Authorization": "Bearer ok-token",
            "Content-Type": "text/plain",
        },
        data='{"order":"","skip":0,"limit":1000000,"query":{"type":"resource"}}',
    )

    assert response.status_code == 200
    lines = [line for line in response.text.splitlines() if line.strip()]
    row = json.loads(lines[1])
    assert row["query"] == {"type": "resource"}
    assert row["limit"] == 1000000


def test_list_records_unknown_model_returns_404():
    class MissingModelEnv:
        def __init__(self):
            self.user_session = types.SimpleNamespace(
                app_code="test-app",
                is_admin=False,
                uid="u1",
                user={"uid": "u1"},
            )
            self.orm = types.SimpleNamespace(
                app_settings=types.SimpleNamespace(
                    module_name="demo",
                    version="1.0.0",
                    logo_img_url="",
                    admins=[],
                )
            )

        def get(self, model_name: str):
            return None

    async def fake_authed_env():
        pass

    async def service_dep():
        yield Service(MissingModelEnv())

    app.dependency_overrides[get_authed_env] = fake_authed_env
    app.dependency_overrides[get_service] = service_dep
    client = TestClient(app)

    response = client.post(
        "/list/missing",
        headers={"Authorization": "Bearer ok-token"},
        json={
            "query": {"active": True},
            "order": "-created_at",
            "skip": 0,
            "limit": 10,
        },
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Model 'missing' not found"
    app.dependency_overrides.clear()


def test_list_records_title_case_model_is_normalized():
    class Status:
        fail = False
        msg = ""

    class Schema:
        @staticmethod
        def schema():
            return {"components": []}

        @staticmethod
        def filter_keys():
            return {}

    class UserModel:
        data_model = "user"
        status = Status()
        model = Schema()
        table_columns = {"rec_name": "Name"}

        def get_domain(self, query):
            return query

        async def count(self, domain):
            return 1

        async def find(
            self,
            domain,
            sort="",
            skip=0,
            limit=0,
            pipeline_items=None,
            obfuscate_fields=None,
            fields=None,
        ):
            return [{"rec_name": "john"}]

        def stream_find(
            self,
            domain,
            sort="",
            skip=0,
            limit=0,
            pipeline_items=None,
            obfuscate_fields=None,
            fields=None,
            batch_size=0,
        ):
            return [{"rec_name": "john"}]

    class AliasModelEnv:
        def __init__(self):
            self.user_session = types.SimpleNamespace(
                app_code="test-app",
                is_admin=False,
                uid="u1",
                user={"uid": "u1"},
            )
            self.orm = types.SimpleNamespace(
                app_settings=types.SimpleNamespace(
                    module_name="demo",
                    version="1.0.0",
                    logo_img_url="",
                    admins=[],
                )
            )
            self._models = {"user": UserModel()}

        def get(self, model_name: str):
            return self._models.get(model_name)

    async def fake_authed_env():
        pass

    async def service_dep():
        yield Service(AliasModelEnv())

    app.dependency_overrides[get_authed_env] = fake_authed_env
    app.dependency_overrides[get_service] = service_dep
    client = TestClient(app)

    response = client.post(
        "/list/User",
        headers={"Authorization": "Bearer ok-token"},
        json={
            "query": {"active": True},
            "order": "rec_name:asc",
            "skip": 0,
            "limit": 10,
        },
    )

    assert response.status_code == 200
    lines = [line for line in response.text.splitlines() if line.strip()]
    envelope = json.loads(lines[0])
    row = json.loads(lines[1])
    assert envelope["content"]["model"] == "user"
    assert row["rec_name"] == "john"
    app.dependency_overrides.clear()


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


def test_import_component_syncs_without_generating_defaults():
    factory = Factory()
    client = make_client(factory)
    response = client.post(
        "/import/component",
        headers={"Authorization": "Bearer ok-token"},
        json={"rec_name": "demo_component", "type": "resource"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["content"]["rec_name"] == "demo_component"
    assert factory.last_upsert_call == {
        "model": "component",
        "payload": {"rec_name": "demo_component", "type": "resource"},
        "rec_name": "demo_component",
        "sync_component_runtime": True,
        "generate_component_defaults": False,
    }


def test_update_record_rec_name_mismatch():
    factory = Factory()
    client = make_client(factory)
    response = client.post(
        "/record/customer/mario",
        headers={"Authorization": "Bearer ok-token"},
        json={"rec_name": "mario", "status": "updated"},
    )
    assert response.status_code == 404


def test_client_attachment_upload_download_delete(monkeypatch, tmp_path):
    factory = Factory()
    monkeypatch.setattr(
        api_routes,
        "get_env_settings",
        lambda: EnvSettings(
            app_code="test-app",
            upload_root=tmp_path,
            clamav_enabled=False,
        ),
    )
    client = make_client(factory)

    upload = client.post(
        "/client/attachment",
        headers={"Authorization": "Bearer ok-token"},
        files={"file": ("hello.txt", b"hello world", "text/plain")},
    )

    assert upload.status_code == 200
    body = upload.json()
    assert body["storage"] == "url"
    assert body["name"] == "hello.txt"
    assert body["size"] == 11
    assert body["type"] == "text/plain"
    assert body["url"].startswith("/client/attachment/")
    assert "base64" not in body

    download = client.get(
        body["url"],
        headers={"Authorization": "Bearer ok-token"},
    )
    assert download.status_code == 200
    assert download.content == b"hello world"

    deleted = client.delete(
        body["url"],
        headers={"Authorization": "Bearer ok-token"},
    )
    assert deleted.status_code == 200
    assert deleted.json() == {"deleted": True, "id": body["id"]}

    missing = client.get(
        body["url"],
        headers={"Authorization": "Bearer ok-token"},
    )
    assert missing.status_code == 404


def test_client_attachment_upload_infected_destroys_file(monkeypatch, tmp_path):
    from app.services import antivirus as antivirus_module

    async def _fake_instream(self, buffer):
        return {"stream": ("FOUND", "Eicar-Test-Signature")}

    monkeypatch.setattr(
        antivirus_module.ClamdAsyncClient, "instream", _fake_instream
    )

    upload_root = tmp_path / "uploads"
    clamav_tmp_dir = tmp_path / "clamav-tmp"
    factory = Factory()
    monkeypatch.setattr(
        api_routes,
        "get_env_settings",
        lambda: EnvSettings(
            app_code="test-app",
            upload_root=upload_root,
            clamav_enabled=True,
            clamav_tmp_dir=clamav_tmp_dir,
        ),
    )
    client = make_client(factory)

    upload = client.post(
        "/client/attachment",
        headers={"Authorization": "Bearer ok-token"},
        files={"file": ("virus.txt", b"infected payload", "text/plain")},
    )

    assert upload.status_code == 422
    assert not upload_root.exists() or not any(upload_root.rglob("*"))
    assert not any(clamav_tmp_dir.glob("upload-*"))


def test_client_attachment_upload_clean_scan_stores_file(monkeypatch, tmp_path):
    from app.services import antivirus as antivirus_module

    async def _fake_instream(self, buffer):
        return {"stream": ("OK", None)}

    monkeypatch.setattr(
        antivirus_module.ClamdAsyncClient, "instream", _fake_instream
    )

    upload_root = tmp_path / "uploads"
    clamav_tmp_dir = tmp_path / "clamav-tmp"
    factory = Factory()
    monkeypatch.setattr(
        api_routes,
        "get_env_settings",
        lambda: EnvSettings(
            app_code="test-app",
            upload_root=upload_root,
            clamav_enabled=True,
            clamav_tmp_dir=clamav_tmp_dir,
        ),
    )
    client = make_client(factory)

    upload = client.post(
        "/client/attachment",
        headers={"Authorization": "Bearer ok-token"},
        files={"file": ("hello.txt", b"hello world", "text/plain")},
    )

    assert upload.status_code == 200
    body = upload.json()
    assert body["size"] == 11
    assert body["scan"]["status"] == str(AttachmentScanStatus.CLEAN)
    assert not any(clamav_tmp_dir.glob("upload-*"))


def test_client_record_attachment_download(monkeypatch, tmp_path):
    factory = Factory()
    monkeypatch.setattr(
        api_routes,
        "get_env_settings",
        lambda: EnvSettings(
            app_code="test-app",
            upload_root=tmp_path,
            clamav_enabled=False,
        ),
    )
    rec_name = "test_request.b68eb77950f8436ebc7e82b861e602be"
    filename = "Inrim-QuiIAM-OFFERTA v4-3cffd9b8-af92-453c-9024-dcf706286d72.pdf"
    folder = tmp_path / "test_request" / rec_name
    folder.mkdir(parents=True)
    (folder / filename).write_bytes(b"%PDF-1.4 demo")
    client = make_client(factory)

    response = client.get(
        f"/client/attachment/test_request/{rec_name}/{filename.replace(' ', '%20')}",
        headers={"Authorization": "Bearer ok-token"},
    )

    assert response.status_code == 200
    assert response.content == b"%PDF-1.4 demo"
    assert response.headers["content-type"] == "application/pdf"


def test_client_record_attachment_denied_when_record_not_readable(
    monkeypatch, tmp_path
):
    """IDOR: l'allegato eredita l'ACL del record che lo possiede.

    Il file esiste su disco e il path e' valido; l'unico motivo per cui
    la richiesta non deve passare e' che `load_record` (model_groups_rule
    + record_rulse + field ACL) non restituisce il record. Prima questa
    rotta non interrogava affatto l'ACL: bastava indovinare
    model/rec_name, che sono identificativi leggibili, non capability.
    """
    factory = Factory()
    monkeypatch.setattr(
        api_routes,
        "get_env_settings",
        lambda: EnvSettings(
            app_code="test-app",
            upload_root=tmp_path,
            clamav_enabled=False,
        ),
    )
    filename = "segreto.pdf"
    folder = tmp_path / "test_request" / "not-found"
    folder.mkdir(parents=True)
    (folder / filename).write_bytes(b"%PDF-1.4 riservato")
    client = make_client(factory)

    response = client.get(
        f"/client/attachment/test_request/not-found/{filename}",
        headers={"Authorization": "Bearer ok-token"},
    )

    assert response.status_code == 404
    assert b"riservato" not in response.content


def test_client_record_attachment_denied_when_acl_refuses_read(
    monkeypatch, tmp_path
):
    """Semantica reale del diniego ACL: `load_record` NON ritorna falsy,
    solleva HTTPException(404) (`service.py`, ramo `if not final_read`).

    Questo test fissa quel comportamento: se un domani `load_record`
    passasse a restituire il record con `readable=False` invece di
    sollevare, il gate `if not record` non basterebbe piu' e questo test
    diventerebbe rosso.
    """
    factory = Factory()
    monkeypatch.setattr(
        api_routes,
        "get_env_settings",
        lambda: EnvSettings(
            app_code="test-app",
            upload_root=tmp_path,
            clamav_enabled=False,
        ),
    )

    async def acl_denied_load_record(self, model: str, rec_name: str):
        raise HTTPException(
            status_code=404, detail=f"Record '{rec_name}' not found"
        )

    monkeypatch.setattr(
        FakeService, "load_record", acl_denied_load_record, raising=True
    )

    filename = "segreto.pdf"
    folder = tmp_path / "test_request" / "REQ-001"
    folder.mkdir(parents=True)
    (folder / filename).write_bytes(b"%PDF-1.4 riservato")
    client = make_client(factory)

    response = client.get(
        f"/client/attachment/test_request/REQ-001/{filename}",
        headers={"Authorization": "Bearer ok-token"},
    )

    assert response.status_code == 404
    assert b"riservato" not in response.content


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


def test_get_remote_select_refuses_client_supplied_url(monkeypatch):
    """SSRF: l'URL non puo' piu' arrivare dal body.

    Il ramo `data.url` fetchava un URL arbitrario lato server e ne
    restituiva il corpo al chiamante; peggio, `data.headerValueKey`
    finiva in `get_global_param()` (che non applica ACL), permettendo di
    far spedire il valore di qualunque record `global_params` come header
    verso un host scelto dal client. La config remota si risolve solo
    server-side dal component, via `key` + `curr_model`.
    """
    factory = Factory()
    monkeypatch.setattr(
        api_routes, "make_response_object", fake_make_response_object
    )
    client = make_client(factory)

    response = client.post(
        "/get_remote_select",
        headers={"Authorization": "Bearer ok-token"},
        json={
            "data": {
                "url": "http://169.254.169.254/latest/meta-data/",
                "headerKey": "X-Api-Key",
                "headerValueKey": "some_service_credentials",
            },
            "properties": {},
        },
    )

    assert response.status_code == 400
    assert "data.url" in response.json()["detail"]
    app.dependency_overrides.clear()


def test_get_remote_select_resolves_server_side_from_component(monkeypatch):
    """Il percorso legittimo (key + curr_model) continua a funzionare."""
    factory = Factory()
    monkeypatch.setattr(
        api_routes, "make_response_object", fake_make_response_object
    )
    client = make_client(factory)

    response = client.post(
        "/get_remote_select",
        headers={"Authorization": "Bearer ok-token"},
        json={"key": "supplier", "curr_model": "order", "properties": {}},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["content"]["mode"] == "list"
    assert body["content"]["data"] == [{"label": "One", "value": "1"}]
    assert factory.last_select_options_call == {
        "key": "supplier",
        "curr_model": "order",
    }
    app.dependency_overrides.clear()


def test_openapi_documents_security_scheme_and_request_app_code():
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
    assert "uid" in get_session_schema["properties"]
    assert "username" in get_session_schema["properties"]
    assert "authenticated" in get_session_schema["properties"]
    assert "token" not in get_session_schema["properties"]
    assert "sso_refresh" not in get_session_schema["properties"]
    assert (
        "Resolved app code for the current request"
        in get_session_schema["properties"]["app_code"]["description"]
    )
