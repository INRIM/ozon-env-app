import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.services.service import Service


class _Status:
    fail = False
    msg = ""


class _Schema:
    @staticmethod
    def schema():
        return {"components": []}

    @staticmethod
    def filter_keys():
        return {}


class _ListModel:
    def __init__(self, data_model: str, rows=None):
        self.data_model = data_model
        self.status = _Status()
        self.model = _Schema()
        self.table_columns = {"rec_name": "Name"}
        self.rows = list(rows or [])
        self.last_domain = None

    def get_domain(self, query):
        self.last_domain = query
        return query

    async def count(self, domain):
        return len(self.rows)

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
        return list(self.rows)

    async def by_name(self, name):
        for row in self.rows:
            if row.get("rec_name") == name:
                return row
        return {}

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
        return list(self.rows)


class _UpsertModel(_ListModel):
    async def upsert(
        self,
        data=None,
        rec_name="",
        data_value=None,
        trnf_config=None,
        fields_parser=None,
    ):
        record = dict(data or {})
        if rec_name:
            record["rec_name"] = rec_name
        return record


class _MissingModelEnv:
    def __init__(self):
        self.user_session = SimpleNamespace(
            app_code="demo",
            is_admin=False,
            uid="u1",
            user={"uid": "u1"},
        )
        self.orm = SimpleNamespace(
            app_settings=SimpleNamespace(
                module_name="demo",
                version="1.0.0",
                logo_img_url="",
                admins=[],
            )
        )

    def get(self, model_name: str):
        return None


class _AliasEnv(_MissingModelEnv):
    def __init__(self):
        super().__init__()
        self._models = {
            "user": _ListModel("user", rows=[{"rec_name": "john"}]),
        }

    def get(self, model_name: str):
        return self._models.get(model_name)


class _ComponentHookEnv(_MissingModelEnv):
    def __init__(self, rows=None):
        super().__init__()
        self._models = {
            "component": _UpsertModel("component", rows=rows or []),
        }
        self.inserted_components = []

    def get(self, model_name: str):
        return self._models.get(model_name)

    async def insert_update_component(self, schema):
        self.inserted_components.append(schema.copy())


def test_list_records_missing_model_raises_http_404():
    service = Service(_MissingModelEnv())

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            service.list_records(
                model_name="missing",
                query={"active": True},
                order="rec_name:asc",
                skip=0,
                limit=10,
            )
        )

    assert exc.value.status_code == 404
    assert exc.value.detail == "Model 'missing' not found"


def test_upsert_missing_model_raises_http_404():
    service = Service(_MissingModelEnv())

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            service.upsert(
                model_name="missing",
                data={"rec_name": "demo"},
                rec_name="demo",
            )
        )

    assert exc.value.status_code == 404
    assert exc.value.detail == "Model 'missing' not found"


def test_load_record_missing_model_raises_http_404():
    service = Service(_MissingModelEnv())

    with pytest.raises(HTTPException) as exc:
        asyncio.run(service.load_record("missing", "demo"))

    assert exc.value.status_code == 404
    assert exc.value.detail == "Model 'missing' not found"


def test_list_records_resolves_title_case_model_name():
    service = Service(_AliasEnv())

    response = asyncio.run(
        service.list_records(
            model_name="User",
            query={"active": True},
            order="rec_name:asc",
            skip=0,
            limit=10,
        )
    )

    assert response.content.model == "user"
    assert response.content.total_count == 1
    assert response.content.data == [{"rec_name": "john"}]


def test_component_upsert_does_not_sync_runtime_by_default():
    env = _ComponentHookEnv()
    service = Service(env)
    synced = []

    async def fake_make_default_actions(schema):
        synced.append(schema.copy())

    service._make_default_actions_for_component = fake_make_default_actions

    asyncio.run(
        service.upsert(
            model_name="component",
            data={"rec_name": "demo_component", "type": "resource"},
            rec_name="demo_component",
        )
    )

    assert env.inserted_components == []
    assert synced == []


def test_component_upsert_syncs_runtime_without_generating_defaults_by_default():
    env = _ComponentHookEnv()
    service = Service(env)
    synced = []

    async def fake_make_default_actions(schema):
        synced.append(schema.copy())

    service._make_default_actions_for_component = fake_make_default_actions

    asyncio.run(
        service.upsert(
            model_name="component",
            data={"rec_name": "demo_component", "type": "resource"},
            rec_name="demo_component",
            sync_component_runtime=True,
        )
    )

    expected = {"rec_name": "demo_component", "type": "resource"}
    assert env.inserted_components == [expected]
    assert synced == []


def test_component_upsert_generates_defaults_only_on_insert():
    env = _ComponentHookEnv()
    service = Service(env)
    synced = []

    async def fake_make_default_actions(schema):
        synced.append(schema.copy())

    service._make_default_actions_for_component = fake_make_default_actions

    asyncio.run(
        service.upsert(
            model_name="component",
            data={"rec_name": "demo_component", "type": "resource"},
            rec_name="demo_component",
            sync_component_runtime=True,
            generate_component_defaults=True,
        )
    )

    expected = {"rec_name": "demo_component", "type": "resource"}
    assert env.inserted_components == [expected]
    assert synced == [expected]


def test_component_upsert_does_not_generate_defaults_on_update():
    env = _ComponentHookEnv(
        rows=[{"rec_name": "demo_component", "type": "resource"}]
    )
    service = Service(env)
    synced = []

    async def fake_make_default_actions(schema):
        synced.append(schema.copy())

    service._make_default_actions_for_component = fake_make_default_actions

    asyncio.run(
        service.upsert(
            model_name="component",
            data={"rec_name": "demo_component", "type": "resource", "title": "Updated"},
            rec_name="demo_component",
            sync_component_runtime=True,
            generate_component_defaults=True,
        )
    )

    expected = {
        "rec_name": "demo_component",
        "type": "resource",
        "title": "Updated",
    }
    assert env.inserted_components == [expected]
    assert synced == []


def test_component_upsert_skips_runtime_sync_for_builder_temporary_name():
    env = _ComponentHookEnv()
    service = Service(env)
    synced = []

    async def fake_make_default_actions(schema):
        synced.append(schema.copy())

    service._make_default_actions_for_component = fake_make_default_actions

    asyncio.run(
        service.upsert(
            model_name="component",
            data={"rec_name": "component.7f919aeea2d745adb7f357a0a849a84f", "type": "resource"},
            rec_name="component.7f919aeea2d745adb7f357a0a849a84f",
            sync_component_runtime=True,
            generate_component_defaults=True,
        )
    )

    assert env.inserted_components == []
    assert synced == []
