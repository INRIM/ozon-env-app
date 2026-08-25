import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import BaseModel
from pydantic import Field

from app.core.OzonEnvApp import _DEFAULT_MODELS_GROUPS_NON_SYS
from app.core.OzonEnvApp import _DEFAULT_MODELS_RESTRICTED_FIELDS
from app.ozon_env_acl import compile_field_acl_policies
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


class _StatusTrackingComponentModel(_ListModel):
    async def by_name(self, name):
        record = await super().by_name(name)
        if record:
            self.status.fail = False
            self.status.msg = ""
            return record
        self.status.fail = True
        self.status.msg = "Not found"
        return None


class _RuleCollection:
    def __init__(self):
        self.deleted = []
        self.inserted = []

    async def delete_many(self, query):
        self.deleted.append(query)

    async def insert_many(self, rows):
        self.inserted.extend(rows)


class _PassthroughValidatedRecord:
    def __init__(self, data):
        self._data = dict(data)

    def get_dict(self, exclude=None):
        exclude = exclude or set()
        return {k: v for k, v in self._data.items() if k not in exclude}


class _PassthroughModel:
    """Fake per model_groups_rule/model_fields_rule: model_rules_sync usa
    env.get(name).new(data=row) per validare la riga via ORM dynamic
    reale, non piu' le classi statiche ModelGroupsRule/ModelFieldsRule."""

    async def new(self, data):
        return _PassthroughValidatedRecord(data)


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


class _MissingModelEnv:
    def __init__(self):
        self.user_session = SimpleNamespace(
            app_code="demo",
            # admin: questi test esercitano side-effect dell'upsert su
            # "component" (sync/menu-dashboard), non l'ACL a livello di
            # model — model_groups_rule (fail-closed per i non-admin) non
            # e' quello sotto test qui.
            is_admin=True,
            uid="u1",
            user={"uid": "u1"},
        )
        self.orm = SimpleNamespace(
            app_settings=SimpleNamespace(
                app_code="demo",
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
            "model_groups_rule": _PassthroughModel(),
            "model_fields_rule": _PassthroughModel(),
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


def test_load_component_keeps_primary_operation_status_for_response():
    component_model = _StatusTrackingComponentModel(
        "component",
        rows=[{"rec_name": "action", "type": "form"}],
    )
    env = _ComponentHookEnv()
    env._models["component"] = component_model
    service = Service(env)

    async def allow_model(_model_key):
        return {
            "read": True,
            "create": True,
            "update": True,
            "delete": True,
            "export": True,
        }

    async def no_record_rules(_model_key):
        return []

    async def no_field_acl():
        return SimpleNamespace(
            apply_read=lambda **kwargs: (kwargs["data"], []),
        )

    async def passthrough_webhook(_event, *, context, payload):
        return SimpleNamespace(payload=payload)

    service._get_model_group_access = allow_model
    service._get_record_rules = no_record_rules
    service._get_compiled_field_acl = no_field_acl
    service.webhooks = SimpleNamespace(emit=passthrough_webhook)

    response = asyncio.run(service.load_record("component", "action"))

    # _is_sys_model("component") performs a second lookup on the same mutable
    # model and leaves its status at "Not found". The response must retain the
    # status of the primary component/action lookup.
    assert component_model.status.fail is True
    assert response.fail is False
    assert response.content.data["rec_name"] == "action"
    assert response.content.rec_name == "action"


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


def test_component_upsert_syncs_model_rules_by_default():
    env = _ComponentHookEnv()
    rule_engine = _RuleEngine()
    env.orm.db = SimpleNamespace(engine=rule_engine)
    service = Service(env)

    asyncio.run(
        service.upsert(
            model_name="component",
            data={
                "rec_name": "demo_component",
                "type": "resource",
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
                        # "fields_rule" e' config vecchia (rule_type=
                        # "fields", ritirato in favore di Layer 3/ozon-env):
                        # presente per provare che viene ignorata.
                        "fields_rule": {
                            "resticted_fields": ["salary"],
                            "allowed_groups": [
                                {"groups": ["dpo"], "actions": {"read": True}}
                            ],
                        },
                        "record_rules": [
                            {
                                "filters": {
                                    "owner_uid": {"$eq": {"var": "user.uid"}}
                                },
                                "actions": {"read": True, "update": True},
                            }
                        ],
                    },
                },
            },
            rec_name="demo_component",
        )
    )

    assert env.inserted_components == []
    assert rule_engine.groups.deleted == [
        {"app_code": "demo", "model": "demo_component"}
    ]
    assert rule_engine.fields.deleted == [
        {"app_code": "demo", "model": "demo_component"}
    ]
    assert rule_engine.groups.inserted[0]["rec_name"] == (
        "mgr.demo.demo_component.manager"
    )
    assert rule_engine.fields.inserted[0]["rec_name"] == (
        "mfr.demo.demo_component.record.0"
    )
    assert rule_engine.fields.inserted[0]["rule_type"] == "record"


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


def test_component_upsert_create_menu_dashboard_skips_no_model_component():
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
                "rec_name": "demo_no_model_form",
                "type": "form",
                "data_model": "no_model",
                "create_menu_dashboard": True,
            },
            rec_name="demo_no_model_form",
        )
    )

    # no_model component: niente menu_group ne' action, anche se
    # create_menu_dashboard e' stato richiesto esplicitamente.
    assert synced == []


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
    # La coercizione number -> string e' fatta da ozon-env in fase di
    # costruzione del model, non dal layer applicativo: qui il payload
    # arriva a upsert non modificato.
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
    
    # 1. Non-sys component -> operator is enough: it implies user via groups.
    schema_non_sys = {
        "rec_name": "customer",
        "type": "resource",
        "title": "Customer",
        "sys": False
    }
    
    asyncio.run(service._make_default_actions_for_component(schema_non_sys))

    assert len(env.action_model.upserts) == 1
    assert env.action_model.upserts[0]["groups"] == ["operator"]
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


class _OwnerTrackingModel(_UpsertModel):
    """Registra `preserve_owner` NEL MOMENTO della upsert: il flag e' messo
    e tolto attorno alla chiamata (try/finally), quindi leggerlo dopo non
    direbbe niente."""

    def __init__(self, data_model: str, rows=None):
        super().__init__(data_model, rows=rows)
        self.preserve_owner = False
        self.preserve_owner_data = None
        self.preserve_owner_during_upsert = None
        self.preserve_owner_data_during_upsert = None

    async def upsert(self, data=None, rec_name="", **kwargs):
        self.preserve_owner_during_upsert = self.preserve_owner
        self.preserve_owner_data_during_upsert = self.preserve_owner_data
        return await super().upsert(data=data, rec_name=rec_name, **kwargs)


class _LoadableUserModel(_ListModel):
    """`Service._resolve_owner_identity` usa `load({"uid": ...})`, non
    by_name: la collection `user` e' chiavata su uid."""

    async def load(self, domain):
        for row in self.rows:
            if all(row.get(k) == v for k, v in domain.items()):
                return row
        return None


class _OwnerEnv(_MissingModelEnv):
    def __init__(
        self,
        rows=None,
        is_admin: bool = True,
        uid: str = "u1",
        users=None,
    ):
        super().__init__()
        self.user_session = SimpleNamespace(
            app_code="demo",
            is_admin=is_admin,
            uid=uid,
            user={"uid": uid},
        )
        self._models = {"doc": _OwnerTrackingModel("doc", rows=rows or [])}
        if users is not None:
            self._models["user"] = _LoadableUserModel("user", rows=users)

    def get(self, model_name: str):
        return self._models.get(model_name)


def _owner_upsert(env, *, data, rec_name="", take_ownership=False):
    # `Service.upsert` ha default True (comportamento storico): l'import
    # e' l'unico chiamante che passa False, come qui.
    service = Service(env)

    async def allow_model(_model_key):
        # model_groups_rule e' fail-closed per i non-admin: qui sotto test
        # c'e' il gate sull'owner_uid, non l'ACL a livello di model.
        return {
            "read": True,
            "create": True,
            "update": True,
            "delete": True,
            "export": True,
        }

    service._get_model_group_access = allow_model
    return asyncio.run(
        service.upsert(
            "doc",
            data,
            rec_name=rec_name,
            take_ownership=take_ownership,
        )
    )


def test_import_preserves_payload_owner_uid_on_insert():
    # Default (take_ownership=False): l'owner del record esportato
    # sopravvive all'import invece di essere riscritto con chi importa.
    env = _OwnerEnv(is_admin=True, uid="importer")
    _owner_upsert(
        env, data={"rec_name": "doc-1", "owner_uid": "original"}
    )

    assert env.get("doc").preserve_owner_during_upsert is True


def test_import_take_ownership_reassigns_owner_to_writer():
    env = _OwnerEnv(is_admin=True, uid="importer")
    _owner_upsert(
        env,
        data={"rec_name": "doc-1", "owner_uid": "original"},
        take_ownership=True,
    )

    # preserve_owner resta False -> set_user_data di ozon-env sovrascrive
    # gli owner_* con la sessione corrente.
    assert env.get("doc").preserve_owner_during_upsert is False


def test_payload_without_owner_uid_is_untouched():
    # Creazione normale: nessun owner_uid nel payload, niente da preservare.
    env = _OwnerEnv(is_admin=True, uid="importer")
    _owner_upsert(env, data={"rec_name": "doc-1"})

    assert env.get("doc").preserve_owner_during_upsert is False


def test_foreign_owner_uid_denied_for_non_admin():
    # Intestare un record a un ALTRO utente assegna permessi: record_rules
    # filtra su owner_uid == user.uid e f_rule.write ha lo sblocco $owner.
    env = _OwnerEnv(is_admin=False, uid="bob")

    with pytest.raises(HTTPException) as exc:
        _owner_upsert(
            env, data={"rec_name": "doc-1", "owner_uid": "alice"}
        )

    assert exc.value.status_code == 403
    # non il 403 generico di model_groups_rule: proprio il gate owner.
    assert "admin" in exc.value.detail["message"]
    assert env.get("doc").preserve_owner_during_upsert is None


def test_own_owner_uid_allowed_for_non_admin():
    # owner_uid == se stesso: nessuna assegnazione di permessi, passa.
    env = _OwnerEnv(is_admin=False, uid="bob")
    _owner_upsert(env, data={"rec_name": "doc-1", "owner_uid": "bob"})

    assert env.get("doc").preserve_owner_during_upsert is True


def test_service_upsert_default_does_not_preserve_payload_owner():
    # Blocca il default: `Service.upsert` e' condiviso da post_update_record,
    # message_queue, step_task, action_runtime e camunda. Se il default
    # diventasse "preserva", un qualsiasi payload con owner_uid creerebbe
    # record intestati a terzi su tutte quelle rotte.
    env = _OwnerEnv(is_admin=True, uid="importer")
    service = Service(env)

    async def allow_model(_model_key):
        return {
            "read": True,
            "create": True,
            "update": True,
            "delete": True,
            "export": True,
        }

    service._get_model_group_access = allow_model
    asyncio.run(
        service.upsert("doc", {"rec_name": "doc-1", "owner_uid": "original"})
    )

    assert env.get("doc").preserve_owner_during_upsert is False


def test_owner_is_never_preserved_on_update():
    # Record gia' esistente -> UPDATE: ozon-env non chiama set_user_data e
    # il diff scarta comunque gli owner_*, quindi l'owner di un record
    # esistente non e' riassegnabile da un payload.
    env = _OwnerEnv(rows=[{"rec_name": "doc-1"}], is_admin=True, uid="u1")
    _owner_upsert(
        env,
        data={"rec_name": "doc-1", "owner_uid": "original"},
        rec_name="doc-1",
    )

    assert env.get("doc").preserve_owner_during_upsert is False


def _deny_owner_uid_on_create(session):
    """Policy reale compilata (niente stub sull'ACL): `owner_uid` negato in
    create per chiunque."""
    return compile_field_acl_policies(
        [
            {
                "model_key": "doc",
                "field_path": "owner_uid",
                "operation": "create",
                "effect": "deny",
                "actor_selector": "*",
                "active": True,
                "deleted": 0,
            }
        ],
        session=session,
    )


def test_import_errors_when_field_acl_denies_owner_uid():
    # Se una field_acl_policy scarta `owner_uid` in create, l'import con
    # take_ownership=False NON puo' ricadere silenziosamente su
    # "owner = chi importa": e' una riassegnazione di ownership mai
    # richiesta (e salterebbe pure il gate admin, che legge il payload
    # dopo l'ACL).
    env = _OwnerEnv(is_admin=True, uid="importer")
    env.user_session.compiled_field_acl = _deny_owner_uid_on_create(
        env.user_session
    )

    with pytest.raises(HTTPException) as exc:
        _owner_upsert(
            env, data={"rec_name": "doc-1", "owner_uid": "original"}
        )

    assert exc.value.status_code == 403
    assert exc.value.detail["reason"] == "owner_uid_denied_by_field_acl"
    # nessun record scritto: si solleva prima della upsert sul model.
    assert env.get("doc").preserve_owner_during_upsert is None
    assert env.get("doc").upserted_data == []


def test_take_ownership_still_works_when_field_acl_denies_owner_uid():
    # take_ownership=True E' la richiesta esplicita di intestare a chi
    # importa: la policy che nega owner_uid non deve farlo fallire.
    env = _OwnerEnv(is_admin=True, uid="importer")
    env.user_session.compiled_field_acl = _deny_owner_uid_on_create(
        env.user_session
    )

    _owner_upsert(
        env,
        data={"rec_name": "doc-1", "owner_uid": "original"},
        take_ownership=True,
    )

    assert env.get("doc").preserve_owner_during_upsert is False
    assert "owner_uid" not in env.get("doc").upserted_data[0]


def test_field_acl_denying_owner_uid_does_not_block_payload_without_owner():
    # Nessun owner_uid nel payload: niente da preservare, la creazione
    # normale passa (owner = chi scrive) anche con la policy attiva.
    env = _OwnerEnv(is_admin=True, uid="importer")
    env.user_session.compiled_field_acl = _deny_owner_uid_on_create(
        env.user_session
    )

    _owner_upsert(env, data={"rec_name": "doc-1"})

    assert env.get("doc").preserve_owner_during_upsert is False


_USER_ROWS = [
    {
        "uid": "original",
        "nome": "Mario",
        "cognome": "Rossi",
        "mail": "mario.rossi@example.org",
        "sector": "Ricerca",
        "sector_id": 7,
        "function": "ricercatore",
        "user": {"tipo_personale": "TI", "qualifica": "primo ricercatore"},
    }
]


def test_import_resolves_owner_fields_from_user_model():
    # Preservare l'owner non basta: owner_name/mail/sector/sector_id (e
    # gli altri owner_*) devono arrivare dal model `user` locale per uid,
    # non dal payload (che porta i dati dell'istanza di origine).
    env = _OwnerEnv(is_admin=True, uid="importer", users=_USER_ROWS)

    _owner_upsert(
        env,
        data={
            "rec_name": "doc-1",
            "owner_uid": "original",
            "owner_name": "Nome Dell'Altra Istanza",
            "owner_sector": "Settore Fantasma",
        },
    )

    model = env.get("doc")
    assert model.preserve_owner_during_upsert is True
    assert model.preserve_owner_data_during_upsert == {
        "owner_name": "Mario Rossi",
        "owner_mail": "mario.rossi@example.org",
        "owner_sector": "Ricerca",
        "owner_sector_id": 7,
        "owner_function": "ricercatore",
        "owner_personal_type": "TI",
        "owner_job_title": "primo ricercatore",
    }


def test_import_with_unknown_owner_uid_is_fail_soft():
    # Record esportato da un'altra istanza, utente non ancora
    # sincronizzato: l'import NON fallisce, ma gli owner_* restano vuoti
    # invece di ereditare quelli del payload.
    env = _OwnerEnv(is_admin=True, uid="importer", users=_USER_ROWS)

    _owner_upsert(
        env,
        data={
            "rec_name": "doc-1",
            "owner_uid": "ghost",
            "owner_name": "Nome Dell'Altra Istanza",
        },
    )

    model = env.get("doc")
    assert model.preserve_owner_during_upsert is True
    assert model.preserve_owner_data_during_upsert == {
        "owner_name": "",
        "owner_mail": "",
        "owner_sector": "",
        "owner_sector_id": 0,
        "owner_function": "",
        "owner_personal_type": "",
        "owner_job_title": "",
    }


def test_take_ownership_does_not_resolve_owner_identity():
    # Nessuna preservazione -> nessuna lettura del model user: gli
    # owner_* li scrive set_user_data dalla sessione, come sempre.
    env = _OwnerEnv(is_admin=True, uid="importer", users=_USER_ROWS)

    _owner_upsert(
        env,
        data={"rec_name": "doc-1", "owner_uid": "original"},
        take_ownership=True,
    )

    model = env.get("doc")
    assert model.preserve_owner_during_upsert is False
    assert model.preserve_owner_data_during_upsert is None


def test_owner_identity_not_resolved_on_update():
    # UPDATE: set_user_data non viene chiamato e il diff scarta comunque
    # gli owner_*, quindi niente da risolvere.
    env = _OwnerEnv(
        rows=[{"rec_name": "doc-1"}],
        is_admin=True,
        uid="u1",
        users=_USER_ROWS,
    )

    _owner_upsert(
        env,
        data={"rec_name": "doc-1", "owner_uid": "original"},
        rec_name="doc-1",
    )

    model = env.get("doc")
    assert model.preserve_owner_during_upsert is False
    assert model.preserve_owner_data_during_upsert is None
