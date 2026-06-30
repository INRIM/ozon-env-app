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


def test_service_start_camunda_update_data_saves_form_and_process_id():
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
    assert result["process_id"] == "proc-123"
    assert result["form"]["process_id"] == "proc-123"


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
    assert response.content.data == {
        "status": "error",
        "message": "utente non autorizzato",
    }


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
