import asyncio
from pathlib import Path
from types import SimpleNamespace

from app.core.OzonEnvApp import AppOzonEnv
from app.core.OzonEnvApp import AppOzonOrm
from app.core.OzonEnvApp import is_runtime_model_name
from app.core.OzonEnvApp import _DEFAULT_MODELS_GROUPS_NON_SYS
from app.core.OzonEnvApp import _DEFAULT_MODELS_RESTRICTED_FIELDS

_DEFAULT_ACL_PROPERTIES = {
    "models_groups": _DEFAULT_MODELS_GROUPS_NON_SYS,
    "models_restricted_fields": _DEFAULT_MODELS_RESTRICTED_FIELDS,
}


class _FakeComponentModel:
    def __init__(self, existing=None):
        self.existing = existing
        self.inserted = []
        self.updated = []

    async def load(self, query):
        if self.existing and query.get("rec_name") == self.existing.get(
            "rec_name"
        ):
            return self.existing.copy()
        return None

    async def new(self, data):
        return dict(data)

    async def insert(self, record):
        self.inserted.append(record.copy())
        return record

    async def update(self, record):
        self.updated.append(record.copy())
        return record


class _FakeOrm:
    def __init__(self):
        self.added = []
        self.updated = []

    async def add_model(self, model_name):
        self.added.append(model_name)

    async def update_model(self, schema, component):
        self.updated.append(
            {
                "schema": dict(schema),
                "component": dict(component),
            }
        )


class _RuleCollection:
    def __init__(self):
        self.deleted = []
        self.inserted = []

    async def delete_many(self, query):
        self.deleted.append(query)

    async def insert_many(self, rows):
        self.inserted.extend(rows)


class _RuleEngine:
    def __init__(self):
        self.groups = _RuleCollection()
        self.fields = _RuleCollection()

    def get_collection(self, name):
        if name == "model_groups_rule":
            return self.groups
        if name == "model_fields_rule":
            return self.fields
        raise AssertionError(f"unexpected collection {name}")


class _FakeComponentCollection:
    def __init__(self, distinct_values):
        self.distinct_values = list(distinct_values)

    async def distinct(self, field_name, query):
        return list(self.distinct_values)


class _FakeDbEngine:
    def __init__(self, collection_names, distinct_values):
        self.collection_names = list(collection_names)
        self.component_collection = _FakeComponentCollection(distinct_values)

    async def list_collection_names(self, filter=None):
        return list(self.collection_names)

    def get_collection(self, name):
        if name != "component":
            raise AssertionError(f"unexpected collection {name}")
        return self.component_collection


class _FakeEnv:
    def __init__(self, tmp_path: Path, collection_names, distinct_values):
        self.lang = "it"
        self.db = SimpleNamespace(
            engine=_FakeDbEngine(collection_names, distinct_values)
        )
        self.config_system = {}
        self.models = {}
        self.models_folder = str(tmp_path / "models")
        self.app_code = "demo"


def test_normalize_component_properties_defaults_non_sys_record():
    from app.core.OzonEnvApp import normalize_component_properties

    schema = {"rec_name": "customer", "type": "resource"}
    normalize_component_properties(schema)

    assert schema["properties"]["models_groups"] == _DEFAULT_MODELS_GROUPS_NON_SYS
    assert (
        schema["properties"]["models_restricted_fields"]
        == _DEFAULT_MODELS_RESTRICTED_FIELDS
    )


def test_normalize_component_properties_defaults_sys_record():
    from app.core.OzonEnvApp import _DEFAULT_MODELS_GROUPS_SYS
    from app.core.OzonEnvApp import normalize_component_properties

    schema = {"rec_name": "action", "type": "resource", "sys": True}
    normalize_component_properties(schema)

    assert schema["properties"]["models_groups"] == _DEFAULT_MODELS_GROUPS_SYS
    assert (
        schema["properties"]["models_restricted_fields"]
        == _DEFAULT_MODELS_RESTRICTED_FIELDS
    )


def test_normalize_component_properties_skips_identity_models():
    from app.core.OzonEnvApp import normalize_component_properties

    for rec_name in ("user", "groups", "group_users"):
        schema = {"rec_name": rec_name, "type": "resource", "sys": True}
        normalize_component_properties(schema)
        assert "models_groups" not in schema["properties"]
        assert "models_restricted_fields" not in schema["properties"]


def test_normalize_component_properties_does_not_override_existing_rules():
    from app.core.OzonEnvApp import normalize_component_properties

    custom_rules = {"rules": [{"groups": ["custom"], "actions": {}}]}
    schema = {
        "rec_name": "customer",
        "type": "resource",
        "properties": {"models_groups": custom_rules},
    }
    normalize_component_properties(schema)

    assert schema["properties"]["models_groups"] == custom_rules
    assert (
        schema["properties"]["models_restricted_fields"]
        == _DEFAULT_MODELS_RESTRICTED_FIELDS
    )


def test_is_runtime_model_name_rejects_builder_temporary_names():
    assert is_runtime_model_name("nullaOstaBandiRequest") is True
    assert (
        is_runtime_model_name(
            "component_40c6976d968c4966800a0667521fde47"
        )
        is True
    )
    assert (
        is_runtime_model_name(
            "component.40c6976d968c4966800a0667521fde47"
        )
        is False
    )
    assert is_runtime_model_name("") is False


def test_insert_update_component_skips_runtime_sync_for_builder_temporary_name():
    env = AppOzonEnv(cfg={"app_code": "demo"})
    component_model = _FakeComponentModel()
    orm = _FakeOrm()
    env.models = {"component": component_model}
    env.orm = orm

    asyncio.run(
        env.insert_update_component(
            {
                "rec_name": "component.40c6976d968c4966800a0667521fde47",
                "type": "form",
            }
        )
    )

    assert component_model.inserted == [
        {
            "rec_name": "component.40c6976d968c4966800a0667521fde47",
            "type": "form",
            "properties": _DEFAULT_ACL_PROPERTIES,
        }
    ]
    assert orm.added == []
    assert orm.updated == []


def test_insert_update_component_keeps_runtime_sync_for_valid_component_name():
    env = AppOzonEnv(cfg={"app_code": "demo"})
    component_model = _FakeComponentModel()
    orm = _FakeOrm()
    env.models = {"component": component_model}
    env.orm = orm

    asyncio.run(
        env.insert_update_component(
            {
                "rec_name": "nullaOstaBandiRequest",
                "type": "form",
            }
        )
    )

    assert component_model.inserted == [
        {
            "rec_name": "nullaOstaBandiRequest",
            "type": "form",
            "properties": _DEFAULT_ACL_PROPERTIES,
        }
    ]
    assert orm.added == ["nullaOstaBandiRequest"]
    assert orm.updated == []


def test_get_collections_names_filters_non_runtime_component_names(tmp_path):
    env = _FakeEnv(
        tmp_path=tmp_path,
        collection_names=["component", "settings", "nullaOstaBandiRequest"],
        distinct_values=[
            "nullaOstaBandiRequest",
            "component.40c6976d968c4966800a0667521fde47",
            "component_40c6976d968c4966800a0667521fde47",
        ],
    )
    orm = AppOzonOrm(env)

    model_names = asyncio.run(orm.get_collections_names())

    assert model_names == [
        "component",
        "settings",
        "nullaOstaBandiRequest",
        "component_40c6976d968c4966800a0667521fde47",
    ]


def test_import_module_model_ignores_invalid_runtime_name(tmp_path):
    env = _FakeEnv(
        tmp_path=tmp_path,
        collection_names=[],
        distinct_values=[],
    )
    orm = AppOzonOrm(env)

    asyncio.run(
        orm.import_module_model("component.40c6976d968c4966800a0667521fde47")
    )

    assert (
        "component.40c6976d968c4966800a0667521fde47"
        not in orm.orm_static_models_map
    )


def test_normalize_component_properties_dict():
    from app.core.OzonEnvApp import normalize_component_properties

    # Case 1: Dict with query (dict) and Orderby (string)
    schema = {
        "rec_name": "testComponent",
        "properties": {
            "query": {"active": True},
            "Orderby": "list_order:asc",
        }
    }
    normalize_component_properties(schema)
    assert schema["properties"]["queryformeditable"] == '{"active": true}'
    assert schema["properties"]["sort"] == "list_order:asc"
    assert schema["properties"]["query"] == {"active": True}
    assert schema["properties"]["Orderby"] == "list_order:asc"

    # Case 2: Dict with query (string) and orderby (string, lowercase)
    schema2 = {
        "rec_name": "testComponent",
        "properties": {
            "query": "{}",
            "orderby": "rec_name:desc",
        }
    }
    normalize_component_properties(schema2)
    assert schema2["properties"]["queryformeditable"] == "{}"
    assert schema2["properties"]["sort"] == "rec_name:desc"

    # Case 3: Dict without query or Orderby
    schema3 = {
        "rec_name": "testComponent",
        "properties": {
            "rheader": "1",
        }
    }
    normalize_component_properties(schema3)
    assert "queryformeditable" not in schema3["properties"]
    assert "sort" not in schema3["properties"]


def test_normalize_component_properties_string():
    from app.core.OzonEnvApp import normalize_component_properties

    schema = {
        "rec_name": "testComponent",
        "properties": '{"query": {"active": true}, "Orderby": "list_order:asc"}'
    }
    normalize_component_properties(schema)
    assert isinstance(schema["properties"], dict)
    assert schema["properties"]["queryformeditable"] == '{"active": true}'
    assert schema["properties"]["sort"] == "list_order:asc"


def test_insert_update_component_normalizes_properties():
    env = AppOzonEnv(cfg={"app_code": "demo"})
    component_model = _FakeComponentModel()
    orm = _FakeOrm()
    env.models = {"component": component_model}
    env.orm = orm

    schema = {
        "rec_name": "testComponent",
        "properties": {
            "query": {"deleted": 0},
            "Orderby": "rec_name:asc",
        }
    }
    asyncio.run(env.insert_update_component(schema))

    assert len(component_model.inserted) == 1
    inserted_schema = component_model.inserted[0]
    assert inserted_schema["properties"]["queryformeditable"] == '{"deleted": 0}'
    assert inserted_schema["properties"]["sort"] == "rec_name:asc"


def test_insert_update_component_syncs_model_rules_on_save():
    env = AppOzonEnv(cfg={"app_code": "demo"})
    component_model = _FakeComponentModel()
    orm = _FakeOrm()
    rule_engine = _RuleEngine()
    orm.app_settings = SimpleNamespace(app_code="demo")
    orm.db = SimpleNamespace(engine=rule_engine)
    env.models = {"component": component_model}
    env.orm = orm

    schema = {
        "rec_name": "document",
        "properties": {
            "models_groups": {
                "rules": [
                    {
                        "groups": ["manager"],
                        "actions": {"read": True, "update": True},
                    }
                ]
            },
            "models_restricted_fields": {
                "fields_rule": {
                    "resticted_fields": ["salary"],
                    "allowed_groups": [
                        {"groups": ["dpo"], "actions": {"read": True}}
                    ],
                },
                "record_rulse": [],
            },
        },
    }

    asyncio.run(env.insert_update_component(schema))

    assert rule_engine.groups.deleted == [{"app_code": "demo", "model": "document"}]
    assert rule_engine.fields.deleted == [{"app_code": "demo", "model": "document"}]
    assert rule_engine.groups.inserted[0]["rec_name"] == "mgr.demo.document.manager"
    assert rule_engine.fields.inserted[0]["rec_name"] == "mfr.demo.document.fields.dpo"


def test_normalize_order_space_and_plus():
    from app.services.service import _normalize_order
    assert _normalize_order("rec_name asc") == "rec_name:asc"
    assert _normalize_order("rec_name+asc") == "rec_name:asc"
    assert _normalize_order("rec_name desc") == "rec_name:desc"
    assert _normalize_order("rec_name+desc") == "rec_name:desc"
    assert _normalize_order("rec_name:desc") == "rec_name:desc"
    assert _normalize_order("-rec_name") == "rec_name:desc"
    assert _normalize_order("+rec_name") == "rec_name:asc"
    assert _normalize_order("rec_name desc, list_order asc") == "rec_name:desc,list_order:asc"
    assert _normalize_order("rec_name+desc,list_order+asc") == "rec_name:desc,list_order:asc"


def test_action_runtime_query_sort_override():
    from app.services.action_runtime import ActionRuntime
    from app.services.common import ResponseObjectData
    import types

    # Mock component record
    class FakeSchemaRecord:
        components = []
        properties = {
            "queryformeditable": '{"deleted": 0, "active": true}',
            "sort": "rec_name:asc",
        }

    # Mock action record
    class FakeAction:
        mode = "list"
        model = "documento"
        view_name = ""
        component_type = ""
        type = "data"
        list_query = '{"status": "approved"}'
        listOrderString = "create_datetime:desc"
        action_type = "window"
        title = "Action Title"

    class FakeService:
        async def _get_component_record(self, name):
            return FakeSchemaRecord()

        def _parse_query_dict(self, val):
            import json
            return json.loads(val) if val else {}

        async def list_records(self, model_name, query, order, **kwargs):
            return types.SimpleNamespace(
                content=ResponseObjectData(
                    mode="list",
                    data=[],
                    model=model_name,
                    query=query,
                )
            )

    srv = FakeService()
    runtime = ActionRuntime(srv)
    async def fake_get_action_record(name):
        return FakeAction()
    async def fake_resolve_action_sequence(name, act):
        return {}
    async def fake_get_context_actions(*args, **kwargs):
        return []
    runtime.get_action_record = fake_get_action_record
    runtime._resolve_action_sequence = fake_resolve_action_sequence
    runtime._get_context_actions = fake_get_context_actions

    # Case 1: Overrides configured on action
    res = asyncio.run(
        runtime.handle_get(
            action_name="test_action",
            query={},
        )
    )
    assert res.query == {"status": "approved"}
    assert res.sort == "create_datetime:desc"

    # Case 2: No overrides configured on action (falls back to component properties)
    FakeAction.list_query = ""
    FakeAction.query = ""
    FakeAction.listOrderString = ""
    res = asyncio.run(
        runtime.handle_get(
            action_name="test_action",
            query={},
        )
    )
    assert res.query == {"deleted": 0, "active": True}
    assert res.sort == "rec_name:asc"
