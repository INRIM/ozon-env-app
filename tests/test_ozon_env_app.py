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


class _PassthroughValidatedRecord:
    def __init__(self, data):
        self._data = dict(data)

    def get_dict(self, exclude=None):
        exclude = exclude or set()
        return {k: v for k, v in self._data.items() if k not in exclude}


class _PassthroughModel:
    """Fake per model_groups_rule/model_fields_rule: model_rules_sync usa
    env.get(name).new(data=row) per validare/normalizzare la riga (via ORM
    dynamic reale) invece delle vecchie classi statiche ModelGroupsRule/
    ModelFieldsRule — qui basta un round-trip senza validazione tipi."""

    async def new(self, data):
        return _PassthroughValidatedRecord(data)


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


def test_insert_update_component_preserves_acl_properties_on_partial_save():
    """Regressione: un save che tocca solo altre chiavi di properties (es.
    un editor report/rheader/rfooter che ricostruisce properties da zero)
    non deve cancellare models_groups/models_restricted_fields gia'
    impostate — OzonModel.update() fa $set su 'properties' come blocco
    atomico (diff a livello di field, non merge chiave-per-chiave), quindi
    senza il fix la nuova properties (senza quella chiave) sovrascrive
    tutto (bug reale osservato su 'user')."""
    existing = {
        "rec_name": "user",
        "sys": True,
        "properties": {
            "models_restricted_fields": {
                "fields_rule": {
                    "resticted_fields": ["codicefiscale"],
                    "allowed_groups": [
                        {"groups": ["gdpr"], "actions": {"read": True}}
                    ],
                },
                "record_rulse": [],
            },
            "rheader": "1",
        },
    }
    env = AppOzonEnv(cfg={"app_code": "demo"})
    component_model = _FakeComponentModel(existing=existing)
    orm = _FakeOrm()
    env.models = {"component": component_model}
    env.orm = orm

    # save successivo, non correlato: tocca solo "report", non passa
    # models_restricted_fields.
    schema = {
        "rec_name": "user",
        "sys": True,
        "properties": {
            "rheader": "1",
            "report": "<p>nuovo report</p>",
        },
    }
    asyncio.run(env.insert_update_component(schema))

    assert len(component_model.updated) == 1
    saved_properties = component_model.updated[0]["properties"]
    assert saved_properties["report"] == "<p>nuovo report</p>"
    assert saved_properties["models_restricted_fields"] == existing["properties"][
        "models_restricted_fields"
    ]


def test_insert_update_component_does_not_restore_acl_properties_when_explicitly_sent():
    """Se il payload di save PORTA models_restricted_fields (anche vuoto
    esplicito), il fix non deve sovrascriverlo con il valore vecchio —
    solo l'assenza della chiave triggera il ripristino."""
    existing = {
        "rec_name": "user",
        "sys": True,
        "properties": {
            "models_restricted_fields": {"fields_rule": {"resticted_fields": ["x"]}},
        },
    }
    env = AppOzonEnv(cfg={"app_code": "demo"})
    component_model = _FakeComponentModel(existing=existing)
    orm = _FakeOrm()
    env.models = {"component": component_model}
    env.orm = orm

    schema = {
        "rec_name": "user",
        "sys": True,
        "properties": {"models_restricted_fields": {}},
    }
    asyncio.run(env.insert_update_component(schema))

    assert component_model.updated[0]["properties"]["models_restricted_fields"] == {}


def test_insert_update_component_syncs_model_rules_on_save():
    env = AppOzonEnv(cfg={"app_code": "demo"})
    component_model = _FakeComponentModel()
    orm = _FakeOrm()
    rule_engine = _RuleEngine()
    orm.app_settings = SimpleNamespace(app_code="demo")
    orm.db = SimpleNamespace(engine=rule_engine)
    env.models = {
        "component": component_model,
        "model_groups_rule": _PassthroughModel(),
        "model_fields_rule": _PassthroughModel(),
    }
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
                # "fields_rule" e' config vecchia (rule_type="fields",
                # ritirato in favore di Layer 3/ozon-env): presente per
                # provare che viene ignorata, non produce piu' righe.
                "fields_rule": {
                    "resticted_fields": ["salary"],
                    "allowed_groups": [
                        {"groups": ["dpo"], "actions": {"read": True}}
                    ],
                },
                "record_rulse": [
                    {
                        "filters": {"owner_uid": {"$eq": {"var": "user.uid"}}},
                        "actions": {"read": True, "update": True},
                    }
                ],
            },
        },
    }

    asyncio.run(env.insert_update_component(schema))

    assert rule_engine.groups.deleted == [{"app_code": "demo", "model": "document"}]
    assert rule_engine.fields.deleted == [{"app_code": "demo", "model": "document"}]
    assert rule_engine.groups.inserted[0]["rec_name"] == "mgr.demo.document.manager"
    assert rule_engine.fields.inserted[0]["rec_name"] == "mfr.demo.document.record.0"
    assert rule_engine.fields.inserted[0]["rule_type"] == "record"


def test_runtime_model_guard_skips_update_model_for_static_component(monkeypatch):
    from ozonenv.core.OzonOrm import OzonOrm

    calls = []

    async def fake_super_update_model(self, schema, component):
        calls.append(("update", schema.get("rec_name")))

    async def fake_super_add_model(self, model_name, virtual=False, data_model=""):
        calls.append(("add", model_name))

    monkeypatch.setattr(OzonOrm, "update_model", fake_super_update_model)
    monkeypatch.setattr(OzonOrm, "add_model", fake_super_add_model)

    orm = AppOzonOrm.__new__(AppOzonOrm)
    # `orm_static_models_map` contiene ANCHE model dynamic con solo un .py
    # cache in models_folder (es. model_fields_rule, popolato dal generico
    # stale-file-import di init_models()) — il guard deve ignorarlo e
    # guardare solo `app_static_model_names` (i VERI static, da
    # _STATIC_MODELS/app_env.py), altrimenti quei model dynamic non si
    # rigenererebbero mai da un save del component (bug reale osservato:
    # model_fields_rule/model_groups_rule bloccati per sempre).
    orm.orm_static_models_map = {
        "mail_template": object(),
        "model_fields_rule": object(),
    }
    orm.app_static_model_names = {"mail_template"}

    # Static VERO (in app_static_model_names): la base OzonOrm.update_model/
    # add_model NON va chiamata, altrimenti rimpiazza la classe Pydantic
    # statica con un model dinamico derivato dallo schema.
    asyncio.run(
        orm.update_model(
            {"rec_name": "mail_template"}, {"rec_name": "mail_template"}
        )
    )
    asyncio.run(orm.add_model("mail_template"))
    assert calls == []

    # model_fields_rule: presente in orm_static_models_map (cache .py) ma
    # NON in app_static_model_names -> deve rigenerarsi normalmente.
    asyncio.run(
        orm.update_model(
            {"rec_name": "model_fields_rule"}, {"rec_name": "model_fields_rule"}
        )
    )
    asyncio.run(orm.add_model("model_fields_rule"))
    assert calls == [
        ("update", "model_fields_rule"),
        ("add", "model_fields_rule"),
    ]
    calls.clear()

    # Model runtime "normale" (non statico, niente cache): il comportamento base resta invariato.
    asyncio.run(
        orm.update_model(
            {"rec_name": "customer"}, {"rec_name": "customer"}
        )
    )
    asyncio.run(orm.add_model("customer"))
    assert calls == [("update", "customer"), ("add", "customer")]


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

        def _resolve_query_json_logic_vars(self, data):
            return data

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


def test_action_runtime_fast_actions_config_populated_when_configured():
    from app.services.action_runtime import ActionRuntime
    from app.services.common import ResponseObjectData
    import types

    class FakeSchemaRecord:
        components = []
        properties = {}

    class FakeActionsFormRecord:
        components = [{"type": "button", "key": "start_process"}]

    class FakeFastActionsConfigRecord:
        actionsForm = "fast_actions_customers_form"

    class FakeAction:
        mode = "list"
        model = "customer"
        view_name = ""
        component_type = ""
        type = "data"
        list_query = ""
        query = ""
        listOrderString = ""
        action_type = "window"
        title = "Customers"

    class FakeFastActionsConfigModel:
        async def load(self, domain):
            if domain.get("model") == "list_customers":
                return FakeFastActionsConfigRecord()
            return None

    class FakeEnv:
        def get(self, model_name):
            assert model_name == "fast_actions_config"
            return FakeFastActionsConfigModel()

    class FakeService:
        env = FakeEnv()

        async def _get_component_record(self, name):
            if name == "fast_actions_customers_form":
                return FakeActionsFormRecord()
            return FakeSchemaRecord()

        def _parse_query_dict(self, val):
            import json
            return json.loads(val) if val else {}

        def _resolve_query_json_logic_vars(self, data):
            return data

        async def list_records(self, model_name, query, order, **kwargs):
            return types.SimpleNamespace(
                content=ResponseObjectData(
                    mode="list",
                    data=[],
                    model=model_name,
                    query=query,
                )
            )

    runtime = ActionRuntime(FakeService())

    async def fake_get_action_record(name):
        return FakeAction()

    async def fake_resolve_action_sequence(name, act):
        return {}

    async def fake_get_context_actions(*args, **kwargs):
        return []

    runtime.get_action_record = fake_get_action_record
    runtime._resolve_action_sequence = fake_resolve_action_sequence
    runtime._get_context_actions = fake_get_context_actions

    res = asyncio.run(
        runtime.handle_get(action_name="list_customers", query={})
    )

    assert res.fields["fast_actions"] == {
        "model": "customer",
        "schema": [{"type": "button", "key": "start_process"}],
        "fast_actions_model": "fast_actions_customers_form",
    }


def test_action_runtime_fast_actions_config_absent_when_not_configured():
    from app.services.action_runtime import ActionRuntime
    from app.services.common import ResponseObjectData
    import types

    class FakeSchemaRecord:
        components = []
        properties = {}

    class FakeAction:
        mode = "list"
        model = "customer"
        view_name = ""
        component_type = ""
        type = "data"
        list_query = ""
        query = ""
        listOrderString = ""
        action_type = "window"
        title = "Customers"

    class FakeFastActionsConfigModel:
        async def load(self, domain):
            return None

    class FakeEnv:
        def get(self, model_name):
            return FakeFastActionsConfigModel()

    class FakeService:
        env = FakeEnv()

        async def _get_component_record(self, name):
            return FakeSchemaRecord()

        def _parse_query_dict(self, val):
            import json
            return json.loads(val) if val else {}

        def _resolve_query_json_logic_vars(self, data):
            return data

        async def list_records(self, model_name, query, order, **kwargs):
            return types.SimpleNamespace(
                content=ResponseObjectData(
                    mode="list",
                    data=[],
                    model=model_name,
                    query=query,
                )
            )

    runtime = ActionRuntime(FakeService())

    async def fake_get_action_record(name):
        return FakeAction()

    async def fake_resolve_action_sequence(name, act):
        return {}

    async def fake_get_context_actions(*args, **kwargs):
        return []

    runtime.get_action_record = fake_get_action_record
    runtime._resolve_action_sequence = fake_resolve_action_sequence
    runtime._get_context_actions = fake_get_context_actions

    res = asyncio.run(
        runtime.handle_get(action_name="list_customers", query={})
    )

    assert "fast_actions" not in res.fields


def test_date_engine_app_resolve_relative_expr():
    from datetime import timedelta, timezone
    from app.core.OzonModelApp import DateEngineApp

    de = DateEngineApp()
    now = de.resolve_relative_expr("now")
    assert now.tzinfo is not None

    minus_3h = de.resolve_relative_expr("now-3h")
    assert abs((now - minus_3h) - timedelta(hours=3)) < timedelta(seconds=5)

    plus_3d_minus_3h = de.resolve_relative_expr("now+3d-3h")
    expected = now + timedelta(days=3) - timedelta(hours=3)
    assert abs(plus_3d_minus_3h - expected) < timedelta(seconds=5)

    assert de.resolve_relative_expr("not-a-date") is None
    assert de.resolve_relative_expr("now+3x") is None
    assert de.resolve_relative_expr("") is None


def test_service_resolve_query_json_logic_vars():
    from app.services.service import Service
    from app.core.OzonModelApp import DateEngineApp

    srv = Service.__new__(Service)
    srv.session = SimpleNamespace(uid="u123", app_code="demo")
    srv.date_engine = DateEngineApp()

    # user.* namespace resolves against the current session
    resolved = srv._resolve_query_json_logic_vars(
        {"owner_uid": {"var": "user.uid"}}
    )
    assert resolved == {"owner_uid": "u123"}

    # unresolved attribute falls back to the json-logic default value
    resolved = srv._resolve_query_json_logic_vars(
        {"x": {"var": ["user.missing_attr", "fallback"]}}
    )
    assert resolved == {"x": "fallback"}

    # unknown namespace with no default resolves to None
    resolved = srv._resolve_query_json_logic_vars(
        {"x": {"var": "data.whatever"}}
    )
    assert resolved == {"x": None}

    # now-based date range, nested inside $and/$gte/$lt
    resolved = srv._resolve_query_json_logic_vars(
        {
            "create_datetime": {
                "$and": [
                    {"$gte": {"var": "now-3h"}},
                    {"$lt": {"var": "now+3d-3h"}},
                ]
            }
        }
    )
    bounds = resolved["create_datetime"]["$and"]
    assert bounds[0]["$gte"] < bounds[1]["$lt"]
