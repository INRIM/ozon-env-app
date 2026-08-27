from __future__ import annotations

import asyncio
import json
from datetime import date
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from bson import Decimal128
import pytest

from app.core.service_manager import ServiceManagerCore
from app.services.service import Service


class FakeModel:
    def __init__(self, rows):
        self.rows = {row["rec_name"]: row.copy() for row in rows}

    async def by_name(self, rec_name):
        return self.rows.get(rec_name, {})


class FakeEnv:
    def __init__(self):
        self.models = {
            "ext_service": FakeModel(
                [
                    {
                        "rec_name": "camunda_local",
                        "title": "Camunda Local",
                        "endpoint": "http://camunda",
                        "status": "active",
                        "tipo": "camunda",
                    }
                ]
            ),
            "ext_service_process": FakeModel(
                [
                    {
                        "rec_name": "approve_request",
                        "parent": "camunda_local",
                        "model": "request",
                        "tenant_id": "tenant-a",
                        "business_key": "bk-1",
                        "variables": '{"seed": true}',
                    }
                ]
            ),
        }

    def get(self, model_name):
        return self.models.get(model_name)


class FakeGateway:
    def __init__(self):
        self.started = []
        self.completed = []

    async def start_process_raw(self, *, process_id, variables):
        self.started.append({"process_id": process_id, "variables": variables})
        return "proc-123"

    async def complete_task(self, *, process_instance_key, variables):
        self.completed.append(
            {
                "process_instance_key": process_instance_key,
                "variables": variables,
            }
        )

    async def process_status(self, process_instance_key):
        return {
            "status": "running",
            "process_id": process_instance_key,
            "tasks": [{"id": "task-1"}],
            "variables": {"x": 1},
        }


def test_service_manager_start_camunda_merges_process_and_payload_variables():
    manager = ServiceManagerCore(FakeEnv())
    gateway = FakeGateway()

    result = asyncio.run(
        manager.start_camunda_process(
            "approve_request",
            {"variables": {"payload": 2}},
            gateway=gateway,
        )
    )

    assert result["process_id"] == "proc-123"
    assert result["stato"]["status"] == "started"
    assert gateway.started == [
        {
            "process_id": "approve_request",
            "variables": {
                "seed": True,
                "payload": 2,
                "tenant_id": "tenant-a",
                "business_key": "bk-1",
                "model": "request",
            },
        }
    ]


def test_service_manager_start_camunda_variables_are_json_safe():
    manager = ServiceManagerCore(FakeEnv())
    gateway = FakeGateway()

    result = asyncio.run(
        manager.start_camunda_process(
            "approve_request",
            {
                "created": datetime(2026, 6, 20, 9, 30),
                "day": date(2026, 6, 20),
                "amount": Decimal("12.34"),
                "bson_amount": Decimal128("45.67"),
                "items": (datetime(2026, 6, 20, 10, 0),),
            },
            gateway=gateway,
        )
    )

    variables = result["variables"]
    json.dumps(variables)
    # il form e' annidato sotto il nome del model (process.model == "request")
    doc = variables["request"]
    assert doc["created"] == "2026-06-20T09:30:00"
    assert doc["day"] == "2026-06-20"
    assert doc["amount"] == "12.34"
    assert doc["bson_amount"] == "45.67"
    assert doc["items"] == ["2026-06-20T10:00:00"]


def test_service_start_camunda_update_data_saves_form_and_process_id(
    monkeypatch,
):
    service = object.__new__(Service)
    upsert_calls = []

    class FakeManager:
        def __init__(self):
            self.started = []

        async def load_process(self, process_key):
            return None, SimpleNamespace(rec_name=process_key, model="request")

        async def start_camunda_process(
            self, process_key, payload, *, gateway
        ):
            self.started.append(
                {
                    "process_key": process_key,
                    "payload": payload,
                    "gateway": gateway,
                }
            )
            return {
                "stato": {"status": "started"},
                "variables": payload,
                "process_id": "proc-123",
            }

    fake_manager = FakeManager()
    service.service_manager = fake_manager
    service._camunda_gateway = lambda: "gateway-client"

    async def fake_upsert(model_name, data, rec_name="", **kwargs):
        upsert_calls.append(
            {
                "model_name": model_name,
                "data": data.copy(),
                "rec_name": rec_name,
            }
        )
        saved = data.copy()
        saved["amount"] = 12
        saved["status"] = "saved"
        return SimpleNamespace(content=SimpleNamespace(data=saved))

    service.upsert = fake_upsert

    # Gate ACL sullo start (privilegi di scrittura sul model del processo):
    # qui interessa il flusso, non i permessi -> tutto concesso.
    async def fake_model_group_access(model_name):
        return {
            "read": True,
            "create": True,
            "update": True,
            "delete": True,
            "export": True,
        }

    service._get_model_group_access = fake_model_group_access

    async def fake_record_rules(model_name, **kwargs):
        return []

    service._get_record_rules = fake_record_rules

    class FakeRecordModel:
        status = SimpleNamespace(fail=False, msg="")
        model = SimpleNamespace(schema=lambda: {"rec_name": "request"})
        data_model = "request"

    service._get_model = lambda model_name: FakeRecordModel()
    # wait disabilitato -> nessun settle: si prende il ramo "form col processo
    # avviato" (process_id/process_status nella response).
    monkeypatch.setattr(
        "app.services.service.get_env_settings",
        lambda: SimpleNamespace(
            camunda_complete_wait_seconds=0,
            camunda_poll_interval_seconds=0.1,
        ),
    )

    result = asyncio.run(
        service.start_camunda_gateway_process(
            "approve_request",
            {"rec_name": "req-1", "amount": 10},
            update_data=True,
            process_model="request",
        )
    )

    assert fake_manager.started == [
        {
            "process_key": "approve_request",
            "payload": {
                "rec_name": "req-1",
                "amount": 12,
                "status": "saved",
            },
            "gateway": "gateway-client",
        }
    ]
    assert upsert_calls == [
        {
            "model_name": "request",
            "data": {"rec_name": "req-1", "amount": 10},
            "rec_name": "req-1",
        },
        {
            "model_name": "request",
            "data": {
                "rec_name": "req-1",
                "amount": 12,
                "status": "saved",
                "process_id": "proc-123",
            },
            "rec_name": "req-1",
        },
    ]
    assert result.content.mode == "form"
    assert result.content.process_id == "proc-123"
    assert result.content.process_status == "started"
    assert result.content.data["process_id"] == "proc-123"


def test_camunda_after_start_response_uses_history_variables(monkeypatch):
    service = object.__new__(Service)

    class FakeGateway:
        def __init__(self):
            self.history_calls = []

        async def wait_until_settled(
            self,
            process_id,
            *,
            timeout_seconds,
            interval_seconds,
        ):
            return {"settled": True, "reason": "completed", "variables": {}}

        async def get_process_history_variables(self, process_id):
            self.history_calls.append(process_id)
            return {
                "last_task": "ckeck_user",
                "ckeck_user": {
                    "error": True,
                    "msg": "utente non autorizzato",
                    "model": "test_request",
                },
            }

    class FakeRecordModel:
        status = SimpleNamespace(fail=False, msg="")
        model = SimpleNamespace(schema=lambda: {"rec_name": "test_request"})
        data_model = "test_request"

    gateway = FakeGateway()
    service._camunda_gateway = lambda: gateway
    service._get_model = lambda model_name: FakeRecordModel()
    monkeypatch.setattr(
        "app.services.service.get_env_settings",
        lambda: SimpleNamespace(
            camunda_complete_wait_seconds=5,
            camunda_poll_interval_seconds=0.1,
        ),
    )

    response = asyncio.run(
        service._camunda_after_start_response(
            "proc-123", model="test_request", rec_name="req-1"
        )
    )

    assert gateway.history_calls == ["proc-123"]
    assert response.content.mode == "form"
    assert response.fail is True
    assert response.message == "utente non autorizzato"
    assert response.content.process_status == "error"


def _make_service_with_actions(existing_actions):
    """Service stub: `_get_action_record` ritorna truthy solo per le action in
    `existing_actions` (verifica esistenza prima di costruire l'URL)."""
    service = object.__new__(Service)

    async def fake_get_action_record(name):
        return object() if name in existing_actions else None

    service._get_action_record = fake_get_action_record
    return service


def test_resolve_start_redirect_rule1_update_data_opens_saved_form():
    service = _make_service_with_actions({"form_form_test_request"})
    settle = {
        "reason": "user_task",
        "variables": {
            "last_task": "ckeck_user",
            "ckeck_user": {
                "next_action": "redirect",
                "next_page": "self",
                "model": "test_request",
                "rec_name": "test_request.abc",
                "update_data": True,
            },
        },
    }
    url = asyncio.run(
        service._resolve_start_redirect(
            settle,
            model="test_request",
            rec_name="test_request.abc",
            update_data=True,
            has_payload=True,
        )
    )
    assert url == "/action/form_form_test_request/test_request.abc"


def test_resolve_start_redirect_rule2_payload_no_update_data_self():
    service = _make_service_with_actions(set())
    settle = {
        "reason": "user_task",
        "variables": {
            "last_task": "ckeck_user",
            "ckeck_user": {
                "next_action": "redirect",
                "next_page": "self",
                "model": "test_request",
                "update_data": False,
            },
        },
    }
    url = asyncio.run(
        service._resolve_start_redirect(
            settle,
            model="test_request",
            rec_name="",
            update_data=False,
            has_payload=True,
        )
    )
    assert url == "self"


def test_resolve_start_redirect_rule3_no_payload_user_task_new_form():
    service = _make_service_with_actions({"new_test_request"})
    settle = {
        "reason": "user_task",
        "variables": {"model": "test_request"},
    }
    url = asyncio.run(
        service._resolve_start_redirect(
            settle,
            model="",
            rec_name="",
            update_data=False,
            has_payload=False,
        )
    )
    assert url == "/action/new_test_request"


def test_resolve_start_redirect_explicit_next_page_verbatim():
    service = _make_service_with_actions(set())
    settle = {
        "reason": "user_task",
        "variables": {
            "last_task": "ckeck_user",
            "ckeck_user": {
                "next_action": "redirect",
                "next_page": "list_test_request",
            },
        },
    }
    url = asyncio.run(
        service._resolve_start_redirect(
            settle, model="test_request", has_payload=True
        )
    )
    assert url == "list_test_request"


def test_resolve_start_redirect_missing_action_falls_back_self():
    # update_data + rec_name ma la action form_form_* non esiste -> "self"
    # (mai un URL morto).
    service = _make_service_with_actions(set())
    settle = {
        "reason": "user_task",
        "variables": {
            "last_task": "ckeck_user",
            "ckeck_user": {
                "next_page": "self",
                "model": "test_request",
                "rec_name": "test_request.abc",
                "update_data": True,
            },
        },
    }
    url = asyncio.run(
        service._resolve_start_redirect(
            settle,
            model="test_request",
            rec_name="test_request.abc",
            update_data=True,
            has_payload=True,
        )
    )
    assert url == "self"


def test_service_manager_complete_decision_payload():
    manager = ServiceManagerCore(FakeEnv())
    gateway = FakeGateway()

    result = asyncio.run(
        manager.complete_camunda_task(
            "proc-123",
            {"form": {"amount": 10}, "user": {"uid": "u1"}, "var": {"x": 1}},
            gateway=gateway,
            decision="refused",
        )
    )

    assert result["stato"]["decision"] == "refused"
    assert gateway.completed[0]["variables"] == {
        "x": 1,
        "form": {"amount": 10},
        "user": {"uid": "u1"},
        "approved": False,
        "refused": True,
    }


def test_service_manager_status_wraps_gateway_payload():
    manager = ServiceManagerCore(FakeEnv())
    gateway = FakeGateway()

    result = asyncio.run(manager.camunda_status("proc-123", gateway=gateway))

    assert result == {
        "stato": {
            "status": "running",
            "process_id": "proc-123",
            "tasks": [{"id": "task-1"}],
            "variables": {"x": 1},
        },
        "variables": {"x": 1},
    }


def test_service_manager_missing_process_raises_lookup_error():
    manager = ServiceManagerCore(FakeEnv())

    with pytest.raises(LookupError):
        asyncio.run(
            manager.start_camunda_process(
                "missing",
                {},
                gateway=FakeGateway(),
            )
        )


def test_service_manager_seed_files_define_models_actions_and_process_name():
    components = json.loads(
        Path("app/base/schema/components.json").read_text()
    )
    actions = json.loads(Path("app/base/data/action.json").read_text())
    menu = json.loads(Path("app/base/data/menu_group.json").read_text())

    component_names = {item["rec_name"] for item in components}
    action_names = {item["rec_name"] for item in actions}
    menu_names = {item["rec_name"] for item in menu}
    action_schema = next(
        item for item in components if item["rec_name"] == "action"
    )

    assert {"ext_service", "ext_service_process"}.issubset(component_names)
    assert "integrations" in menu_names
    assert {
        "list_ext_service",
        "form_form_ext_service",
        "submit_ext_service",
        "delete_ext_service",
        "new_ext_service_process",
        "copy_ext_service_process",
    }.issubset(action_names)

    serialized_action_schema = json.dumps(action_schema)
    serialized_actions = json.dumps(actions)
    assert "process_name_to_complete" not in serialized_action_schema
    assert "process_name_to_complete" not in serialized_actions
    assert "process_name" in serialized_action_schema


def test_group_users_save_action_is_visible_in_form_context():
    actions = json.loads(Path("app/base/data/action.json").read_text())
    submit_group_users = next(
        item for item in actions if item["rec_name"] == "submit_group_users"
    )

    assert submit_group_users["model"] == "group_users"
    assert submit_group_users["action_type"] == "save"
    assert "form" in submit_group_users["context_button_mode"]


def test_groups_save_action_is_visible_in_form_context():
    """submit_groups aveva context_button_mode vuoto (sia in data_value che
    top-level): _get_context_actions scarta l'action se action_mode non e'
    in context_button_mode, quindi il bottone Salva non compariva mai sul
    form 'groups'."""
    actions = json.loads(Path("app/base/data/action.json").read_text())
    submit_groups = next(
        item for item in actions if item["rec_name"] == "submit_groups"
    )

    assert submit_groups["model"] == "groups"
    assert submit_groups["action_type"] == "save"
    assert "form" in submit_groups["context_button_mode"]
    assert submit_groups["data_value"]["context_button_mode"] == "Form"


def test_sys_schemas_have_default_acl_properties_excluding_identity_layer():
    """I record sys esistenti in components.json (esclusa identity layer:
    groups/group_users/model_groups_rule/model_fields_rule, accesso solo
    admin) devono avere models_groups/models_restricted_fields di default
    gia' valorizzati — coerenti con quanto normalize_component_properties
    inietta per i nuovi record.

    "user" e' identity layer ma NON e' escluso come gli altri: porta
    volutamente fields_rule/record_rules (GDPR codicefiscale, ownership)
    configurati per l'enforcement ACL — vedi [[acl-groups-models-groups-
    property]]. Gli altri quattro restano puramente admin-only."""
    from app.core.OzonEnvApp import IDENTITY_MODEL_NAMES
    from app.core.OzonEnvApp import _DEFAULT_MODELS_GROUPS_SYS
    from app.core.OzonEnvApp import _DEFAULT_MODELS_RESTRICTED_FIELDS

    components = json.loads(
        Path("app/base/schema/components.json").read_text()
    )
    sys_components = [c for c in components if c.get("sys") is True]
    assert sys_components

    for component in sys_components:
        rec_name = component["rec_name"]
        properties = component.get("properties") or {}
        if rec_name == "user":
            continue
        if rec_name in IDENTITY_MODEL_NAMES:
            assert "models_groups" not in properties, rec_name
            assert "models_restricted_fields" not in properties, rec_name
            continue
        assert properties.get("models_groups") == _DEFAULT_MODELS_GROUPS_SYS, rec_name
        assert (
            properties.get("models_restricted_fields")
            == _DEFAULT_MODELS_RESTRICTED_FIELDS
        ), rec_name


# --- ACL sui path camunda -------------------------------------------------


def _camunda_acl_service(access, *, record_rules=None, is_sys_model=True):
    """Service reale (senza __init__) coi soli lookup ACL stubbati: i gate
    esercitati sono quelli di produzione."""
    service = object.__new__(Service)

    async def _model_access(model_name):
        return dict(access)

    async def _record_rules(model_name, **kwargs):
        return list(record_rules or [])

    async def _is_sys(model_name):
        return is_sys_model

    service._get_model_group_access = _model_access
    service._get_record_rules = _record_rules
    service._is_sys_model = _is_sys
    service._resolve_query_json_logic_vars = lambda data: data
    return service


_READ_ONLY_ACCESS = {
    "read": True,
    "create": False,
    "update": False,
    "delete": False,
    "export": False,
}
_FULL_ACCESS = {key: True for key in _READ_ONLY_ACCESS}


def test_start_camunda_process_denied_without_model_write():
    """Un processo si avvia solo con privilegi di scrittura sul model su
    cui opera — anche senza update_data (nessun record scritto dallo start,
    ma il processo agisce comunque su quel model)."""
    from fastapi import HTTPException

    service = _camunda_acl_service(_READ_ONLY_ACCESS)
    started = []

    class FakeManager:
        async def load_process(self, process_key):
            return None, SimpleNamespace(rec_name=process_key, model="request")

        async def start_camunda_process(self, process_key, payload, *, gateway):
            started.append(process_key)
            return {"process_id": "p1", "stato": {}, "variables": {}}

    service.service_manager = FakeManager()
    service._camunda_gateway = lambda: "gateway-client"

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            service.start_camunda_gateway_process(
                "approve_request", {"rec_name": "req-1"}
            )
        )

    assert exc.value.status_code == 403
    assert exc.value.detail["operation"] == "process_start"
    assert started == []


def test_start_camunda_process_allowed_with_model_write():
    service = _camunda_acl_service(_FULL_ACCESS)
    started = []

    class FakeManager:
        async def load_process(self, process_key):
            return None, SimpleNamespace(rec_name=process_key, model="request")

        async def start_camunda_process(self, process_key, payload, *, gateway):
            started.append(process_key)
            return {
                "stato": {"status": "started"},
                "variables": payload,
                "process_id": "",
            }

    class _FakeRecordModel:
        status = SimpleNamespace(fail=False, msg="")
        model = SimpleNamespace(
            schema=lambda: {"components": [], "properties": {}},
            filter_keys=lambda: {},
        )
        data_model = "request"
        table_columns = {}

    service.service_manager = FakeManager()
    service._camunda_gateway = lambda: "gateway-client"
    service._get_model = lambda model_name: _FakeRecordModel()

    asyncio.run(
        service.start_camunda_gateway_process(
            "approve_request", {"rec_name": "req-1"}
        )
    )

    assert started == ["approve_request"]


def test_complete_many_denied_without_model_read():
    """Batch: il record viene letto grezzo e il contenuto finisce nelle
    variabili di processo — senza permesso di lettura non si legge."""
    no_read = {**_READ_ONLY_ACCESS, "read": False}
    service = _camunda_acl_service(no_read)
    reads = []

    def _get_model(model_name):
        class _M:
            async def by_name(self, rec_name):
                reads.append(rec_name)
                return SimpleNamespace(process_id="p1")

        return _M()

    service._get_model = _get_model

    response = asyncio.run(
        service.complete_many_camunda_gateway_tasks(
            {"model": "request", "rec_names": ["req-1", "req-2"]}
        )
    )

    assert reads == []
    assert response.fail is True
    assert [row["status"] for row in response.content.data] == [
        "error",
        "error",
    ]
    assert "Model ACL denied" in response.content.data[0]["message"]


def test_complete_many_denied_by_record_rules():
    owner_rule = {
        "app_code": "demo",
        "model": "request",
        "rule_type": "record",
        "group": "manager",
        "filters": {"owner_uid": {"$eq": "u1"}},
        "read": True,
        "create": False,
        "update": False,
        "delete": False,
        "active": True,
        "deleted": 0,
    }
    service = _camunda_acl_service(
        _FULL_ACCESS, record_rules=[owner_rule], is_sys_model=False
    )
    completed = []

    def _get_model(model_name):
        class _M:
            async def by_name(self, rec_name):
                return SimpleNamespace(
                    process_id="p1",
                    owner_uid="altro",
                    get_dict=lambda: {"rec_name": rec_name, "owner_uid": "altro"},
                )

        return _M()

    async def _complete(process_id, payload, decision=""):
        completed.append(process_id)

    service._get_model = _get_model
    service.complete_camunda_gateway_task = _complete

    response = asyncio.run(
        service.complete_many_camunda_gateway_tasks(
            {"model": "request", "rec_names": ["req-1"]}
        )
    )

    assert completed == []
    assert response.content.data[0]["status"] == "error"
    assert "Record ACL denied" in response.content.data[0]["message"]
