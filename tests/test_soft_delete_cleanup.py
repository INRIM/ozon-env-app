import time
from types import SimpleNamespace
from typing import Any

import pytest

from app.services.action_runtime import ActionRuntime
from app.services.service import Service
from app.services.common import ResponseObjectData


class DummyRecord:
    def __init__(self, data: dict):
        self.__dict__.update(data)
        self.deleted = data.get("deleted", 0)
        self.active = data.get("active", True)
        self.rec_name = data.get("rec_name", "")

    def set_to_delete(self, timestamp: int):
        self.deleted = timestamp
        self.active = False

    def get_dict(self) -> dict:
        return self.__dict__.copy()


class DummyModel:
    def __init__(self, model_name: str, records: list[dict] = None):
        self.model_name = model_name
        self.virtual = False
        self.records = [DummyRecord(r) for r in (records or [])]
        self.removed_domains = []
        self.updates = []
        # nativo ozon-env: now + delete_record_after_days giorni.
        self.delete_record_after_days = 1

    async def set_to_delete(self, record):
        import time

        record.set_to_delete(
            int(time.time() + self.delete_record_after_days * 86400)
        )
        return await self.update(record)

    async def by_name(self, name: str):
        for rec in self.records:
            if getattr(rec, "rec_name", None) == name:
                return rec
        return None

    async def update(self, record: Any):
        self.updates.append(record)
        for idx, rec in enumerate(self.records):
            if getattr(rec, "rec_name", None) == getattr(record, "rec_name", None):
                self.records[idx] = record
                break
        else:
            self.records.append(record)
        return record

    async def remove_all(self, domain: dict) -> int:
        self.removed_domains.append(domain)
        count = 0
        remaining = []
        for rec in self.records:
            deleted_val = getattr(rec, "deleted", 0)
            if "$gt" in domain.get("deleted", {}) and "$lte" in domain.get("deleted", {}):
                gt = domain["deleted"]["$gt"]
                lte = domain["deleted"]["$lte"]
                if deleted_val > gt and deleted_val <= lte:
                    count += 1
                    continue
            remaining.append(rec)
        self.records = remaining
        return count


class DummyAppSettings:
    def __init__(self, delete_retention_hours: int = 24):
        self.delete_retention_hours = delete_retention_hours


class DummyOrm:
    def __init__(self, delete_retention_hours: int = 24):
        self.app_settings = DummyAppSettings(delete_retention_hours)


class DummyEnv:
    def __init__(self, models: dict):
        self.models = models
        self.orm = DummyOrm()
        self.user_session = SimpleNamespace(app_code="demo")

    def get(self, model_name: str):
        return self.models.get(model_name)

    async def get_collections_names(self) -> list[str]:
        return list(self.models.keys())


class DummyService:
    def __init__(self, env: DummyEnv):
        self.env = env
        self.session = SimpleNamespace(is_admin=True, is_public=False, app_code="demo")
        self.upserts = []

    async def load_record(self, model: str, rec_name: str):
        model_obj = self.env.get(model)
        if model_obj:
            rec = await model_obj.by_name(rec_name)
            if rec:
                return SimpleNamespace(content=ResponseObjectData(mode="form", data=rec.get_dict()))
        return None

    async def upsert(
        self,
        model: str,
        payload: dict,
        rec_name: str = "",
        sync_component_runtime: bool = False,
        generate_component_defaults: bool = False,
    ):
        self.upserts.append({
            "model": model,
            "payload": payload,
            "rec_name": rec_name,
        })
        model_obj = self.env.get(model)
        record = DummyRecord(payload)
        await model_obj.update(record)
        return SimpleNamespace(content=ResponseObjectData(mode="form", data=payload))


@pytest.mark.asyncio
async def test_action_runtime_handle_post_delete_writes_timestamp():
    # Setup action record for deletion
    action_data = {
        "rec_name": "delete_customer",
        "action_type": "delete",
        "model": "customer",
        "mode": "form",
        "type": "data",
        "next_action_name": "",
        "builder_enabled": False,
    }
    action_model = DummyModel("action", [action_data])

    # Setup customer record
    customer_data = {"rec_name": "cust-1", "name": "John Doe", "deleted": 0, "active": True}
    customer_model = DummyModel("customer", [customer_data])

    models = {"action": action_model, "customer": customer_model}
    env = DummyEnv(models)
    customer_model.delete_record_after_days = 3

    srv = DummyService(env)
    runtime = ActionRuntime(srv)

    # Trigger action delete via handle_post
    await runtime.handle_post(
        action_name="delete_customer",
        data={"rec_name": "cust-1"},
        rec_name="cust-1",
    )

    # Verify that the record is soft-deleted with a future timestamp
    cust_rec = await customer_model.by_name("cust-1")
    assert cust_rec is not None
    assert cust_rec.active is False
    now = int(time.time())
    assert cust_rec.deleted > now
    # nativo ozon-env: now + delete_record_after_days giorni
    expected_timestamp = now + 3 * 86400
    assert abs(cust_rec.deleted - expected_timestamp) < 5


@pytest.mark.asyncio
async def test_action_runtime_handle_delete_writes_timestamp():
    action_data = {
        "rec_name": "delete_customer",
        "action_type": "delete",
        "model": "customer",
        "mode": "form",
        "type": "data",
        "next_action_name": "",
        "builder_enabled": False,
    }
    action_model = DummyModel("action", [action_data])

    customer_data = {"rec_name": "cust-2", "name": "Jane Doe", "deleted": 0, "active": True}
    customer_model = DummyModel("customer", [customer_data])

    models = {"action": action_model, "customer": customer_model}
    env = DummyEnv(models)
    customer_model.delete_record_after_days = 2

    srv = DummyService(env)
    runtime = ActionRuntime(srv)

    # Trigger deletion directly via handle_delete
    await runtime.handle_delete(
        action_name="delete_customer",
        rec_name="cust-2",
    )

    cust_rec = await customer_model.by_name("cust-2")
    assert cust_rec is not None
    assert cust_rec.active is False
    now = int(time.time())
    assert cust_rec.deleted > now
    # nativo ozon-env: now + delete_record_after_days giorni
    expected_timestamp = now + 2 * 86400
    assert abs(cust_rec.deleted - expected_timestamp) < 5


@pytest.mark.asyncio
async def test_service_clean_expired_deleted_records():
    # Setup customer record, one expired, one future, one not deleted
    now = int(time.time())
    expired_timestamp = now - 100
    future_timestamp = now + 100

    r1 = {"rec_name": "expired", "deleted": expired_timestamp, "active": False}
    r2 = {"rec_name": "future", "deleted": future_timestamp, "active": False}
    r3 = {"rec_name": "active_rec", "deleted": 0, "active": True}

    customer_model = DummyModel("customer", [r1, r2, r3])

    models = {"customer": customer_model}
    env = DummyEnv(models)
    
    # Real Service object can be instantiated with DummyEnv!
    service = Service(env)

    # Execute cleanup
    deleted_count = await service.clean_expired_deleted_records()

    # Verify that only the expired record was deleted
    assert deleted_count == 1
    assert len(customer_model.records) == 2
    
    remaining_names = [getattr(r, "rec_name") for r in customer_model.records]
    assert "expired" not in remaining_names
    assert "future" in remaining_names
    assert "active_rec" in remaining_names
