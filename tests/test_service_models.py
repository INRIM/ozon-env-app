import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import BaseModel
from pydantic import Field

from app.core.OzonEnvApp import _DEFAULT_MODELS_GROUPS_NON_SYS
from app.core.OzonEnvApp import _DEFAULT_MODELS_RESTRICTED_FIELDS
from app.services.service import Service

_DEFAULT_ACL_PROPERTIES = {
    "models_groups": _DEFAULT_MODELS_GROUPS_NON_SYS,
    "models_restricted_fields": _DEFAULT_MODELS_RESTRICTED_FIELDS,
}


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
        self.file_dump_mode = ""

    def set_file_dump_mode(self, mode: str):
        self.file_dump_mode = mode

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
    def __init__(self, data_model: str, rows=None):
        super().__init__(data_model, rows=rows)
        self.last_upsert_data = None
        self.upserted_data = []

    async def upsert(
        self,
        data=None,
        rec_name="",
        data_value=None,
        trnf_config=None,
        fields_parser=None,
    ):
        record = dict(data or {})
        self.last_upsert_data = record.copy()
        self.upserted_data.append(record.copy())
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


class _ComponentMenuDashboardEnv(_ComponentHookEnv):
    def __init__(self, rows=None, action_rows=None):
        super().__init__(rows=rows)
        self._models.update(
            {
                "action": _UpsertModel("action", rows=action_rows or []),
                "menu_group": _UpsertModel("menu_group"),
            }
        )


class _MailServerOutSchema(BaseModel):
    rec_name: str = ""
    port: str | None = Field("", title="Port")


class _MailServerOutEnv(_MissingModelEnv):
    def __init__(self):
        super().__init__()
        model = _UpsertModel("mail_server_out")
        model.model = _MailServerOutSchema
        self._models = {"mail_server_out": model}

    def get(self, model_name: str):
        return self._models.get(model_name)


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
    env = _AliasEnv()
    service = Service(env)

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
    assert env.get("user").file_dump_mode == ""


def test_get_model_uses_optional_file_dump_mode_from_settings(monkeypatch):
    env = _AliasEnv()
    service = Service(env)
    monkeypatch.setattr(
        "app.services.service.get_env_settings",
        lambda: SimpleNamespace(model_file_dump_mode="url"),
    )

    model = service._get_model("User")

    assert model is env.get("user")
    assert model.file_dump_mode == "url"


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


def test_component_upsert_create_menu_dashboard_generates_defaults_from_payload():
    env = _ComponentHookEnv()
    service = Service(env)
    synced = []

    async def fake_make_default_actions(schema):
        synced.append(schema.copy())

    service._make_default_actions_for_component = fake_make_default_actions

    asyncio.run(
        service.upsert(
            model_name="component",
            data={
                "rec_name": "demo_component",
                "type": "resource",
                "create_menu_dashboard": True,
            },
            rec_name="demo_component",
        )
    )

    expected = {
        "rec_name": "demo_component",
        "type": "resource",
        "properties": _DEFAULT_ACL_PROPERTIES,
    }
    assert env.inserted_components == []
    assert synced == [expected]


def test_component_upsert_create_menu_dashboard_scopes_menu_group_by_apps():
    env = _ComponentMenuDashboardEnv()
    service = Service(env)

    asyncio.run(
        service.upsert(
            model_name="component",
            data={
                "rec_name": "demo_component",
                "type": "form",
                "title": "Demo Component",
                "create_menu_dashboard": True,
            },
            rec_name="demo_component",
        )
    )

    menu_group_model = env.get("menu_group")
    assert menu_group_model.upserted_data == [
        {
            "rec_name": "demo_component",
            "label": "Demo Component",
            "admin": False,
            "active": True,
            "deleted": 0,
            "apps": ["demo"],
        }
    ]
    assert "app_code" not in menu_group_model.upserted_data[0]


def test_component_upsert_normalizes_empty_string_boolean_fields():
    env = _ComponentHookEnv()
    service = Service(env)

    asyncio.run(
        service.upsert(
            model_name="component",
            data={"rec_name": "demo_component", "type": "resource", "sys": ""},
            rec_name="demo_component",
        )
    )

    component_model = env.get("component")
    assert component_model.last_upsert_data["sys"] is False


def test_upsert_does_not_normalize_scalar_values_for_non_component_models():
    env = _MailServerOutEnv()
    service = Service(env)

    asyncio.run(
        service.upsert(
            model_name="mail_server_out",
            data={"rec_name": "smtp", "port": 465},
            rec_name="smtp",
        )
    )

    mail_server_model = env.get("mail_server_out")
    assert mail_server_model.last_upsert_data["port"] == 465


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

    expected = {
        "rec_name": "demo_component",
        "type": "resource",
        "properties": _DEFAULT_ACL_PROPERTIES,
    }
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

    expected = {
        "rec_name": "demo_component",
        "type": "resource",
        "properties": _DEFAULT_ACL_PROPERTIES,
    }
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
        "properties": _DEFAULT_ACL_PROPERTIES,
    }
    assert env.inserted_components == [expected]
    assert synced == []


def test_component_upsert_create_menu_dashboard_generates_defaults_on_update():
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
            data={
                "rec_name": "demo_component",
                "type": "resource",
                "title": "Updated",
                "create_menu_dashboard": "true",
            },
            rec_name="demo_component",
            sync_component_runtime=True,
            generate_component_defaults=False,
        )
    )

    expected = {
        "rec_name": "demo_component",
        "type": "resource",
        "title": "Updated",
        "properties": _DEFAULT_ACL_PROPERTIES,
    }
    assert env.inserted_components == [expected]
    assert synced == [expected]


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


def test_make_default_actions_adds_user_and_operator_groups_for_non_sys_component():
    class FakeActionModel:
        def __init__(self):
            self.upserts = []

        async def find(self, *args, **kwargs):
            # Return some mocked action templates
            return [
                SimpleNamespace(
                    rec_name="list_action",
                    model="action",
                    sys=True,
                    deleted=0,
                    list_query="{}",
                    context_button_mode="",
                    action_type="list",
                    get_dict=lambda: {
                        "rec_name": "list_action",
                        "model": "action",
                        "sys": True,
                        "deleted": 0,
                        "list_query": "{}",
                        "context_button_mode": "",
                        "action_type": "list",
                    }
                )
            ]

        async def upsert(self, data, rec_name):
            self.upserts.append(data)
            return None

    class FakeMenuModel:
        async def count(self, *args, **kwargs):
            return 1

    class FakeEnv(_MissingModelEnv):
        def __init__(self):
            super().__init__()
            self.action_model = FakeActionModel()
            self.menu_model = FakeMenuModel()

        def get(self, name):
            if name == "action":
                return self.action_model
            if name == "menu_group":
                return self.menu_model
            return None

    env = FakeEnv()
    service = Service(env)
    
    # 1. Non-sys component -> should add user and operator to action groups
    schema_non_sys = {
        "rec_name": "customer",
        "type": "resource",
        "title": "Customer",
        "sys": False
    }
    
    asyncio.run(service._make_default_actions_for_component(schema_non_sys))
    
    assert len(env.action_model.upserts) == 1
    assert env.action_model.upserts[0]["groups"] == ["user", "operator"]
    assert env.action_model.upserts[0]["user_function"] == "user"

    # Reset upserts
    env.action_model.upserts.clear()

    # 2. Sys component -> should not add user and operator to groups
    schema_sys = {
        "rec_name": "customer",
        "type": "resource",
        "title": "Customer",
        "sys": True
    }
    
    asyncio.run(service._make_default_actions_for_component(schema_sys))
    
    assert len(env.action_model.upserts) == 1
    assert "groups" not in env.action_model.upserts[0] or env.action_model.upserts[0]["groups"] != ["user", "operator"]

