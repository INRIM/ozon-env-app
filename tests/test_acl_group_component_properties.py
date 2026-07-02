import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from app.ozon_env_acl import apply_session_groups
from app.ozon_env_acl import synth_policies_from_component_properties
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


class _RecordModel:
    def __init__(self, name, rows=None):
        self.data_model = name
        self.status = _Status()
        self.model = _Schema()
        self.table_columns = {}
        self.rows = list(rows or [])

    def get_domain(self, query):
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
        return [row.copy() for row in self.rows]

    async def by_name(self, name):
        for row in self.rows:
            if row.get("rec_name") == name:
                return row.copy()
        return {}


class _ComponentModel:
    def __init__(self, components):
        self.components = components

    def get_domain(self, query):
        return query

    async def find(self, domain, limit=0):
        return [c.copy() for c in self.components]


class _GroupUsersModel:
    def __init__(self, rows):
        self.rows = rows

    def get_domain(self, query):
        return query

    async def find(self, domain, limit=0):
        return [row.copy() for row in self.rows]


class _UserRuleModel:
    def __init__(self, rows):
        self.rows = rows
        self.domains = []

    def get_domain(self, query):
        return query

    async def find(self, domain, limit=0):
        self.domains.append(domain)
        serialized = json.dumps(domain, sort_keys=True)
        if '"department": "it"' not in serialized:
            return []
        return [row.copy() for row in self.rows[:limit or None]]


class _Env:
    def __init__(self, models, session):
        self.user_session = session
        self.orm = SimpleNamespace(
            app_settings=SimpleNamespace(
                module_name="demo", version="1.0.0", logo_img_url=""
            )
        )
        self._models = models
        self.db = SimpleNamespace(engine=None)

    def get(self, model_name):
        return self._models[model_name]


def _session(groups=None, is_admin=False):
    return SimpleNamespace(
        app_code="demo",
        uid="u1",
        is_admin=is_admin,
        user={"uid": "u1", "groups": groups or []},
    )


def test_synth_policies_from_models_groups_deny_non_admin_wildcard():
    components = [
        {
            "rec_name": "customer",
            "properties": {"models_groups": ["hr", "dpo"]},
        }
    ]
    policies = synth_policies_from_component_properties(components)

    assert len(policies) == 3  # read/insert/update, whole-model field_path "*"
    for policy in policies:
        assert policy["model_key"] == "customer"
        assert policy["field_path"] == "*"
        assert policy["effect"] == "deny"
        assert policy["actor_selector"]["exclude_groups"] == ["dpo", "hr"]
        assert policy["actor_selector"]["is_admin"] is False


def test_synth_policies_from_models_restricted_fields_per_field():
    components = [
        {
            "rec_name": "customer",
            "properties": {
                "models_restricted_fields": {"salary": ["hr", "manager"]}
            },
        }
    ]
    policies = synth_policies_from_component_properties(components)

    assert len(policies) == 3
    for policy in policies:
        assert policy["field_path"] == "salary"
        assert policy["actor_selector"]["exclude_groups"] == [
            "hr",
            "manager",
        ]


def test_synth_policies_ignores_new_engine_rules_shape():
    """models_groups/models_restricted_fields in formato {"rules": [...]} /
    {"fields_rule": ..., "record_rulse": [...]} (defaults iniettati da
    app.core.OzonEnvApp.normalize_component_properties per il motore ACL non
    ancora costruito) sono dict, non list/CSV: _as_set li stringificherebbe
    in un gruppo-fantasma e negherebbe tutto a tutti i non-admin. Devono
    essere ignorati qui finche' il motore non esiste."""
    components = [
        {
            "rec_name": "customer",
            "properties": {
                "models_groups": {
                    "rules": [{"groups": ["admin"], "actions": {"read": True}}]
                },
                "models_restricted_fields": {
                    "fields_rule": {"resticted_fields": [], "allowed_groups": []},
                    "record_rulse": [],
                },
            },
        }
    ]

    policies = synth_policies_from_component_properties(components)

    assert policies == []


def test_models_groups_denies_whole_model_for_non_member_group():
    customer = _RecordModel("customer", rows=[{"rec_name": "c1", "name": "Ada"}])
    component = _ComponentModel(
        [
            {
                "rec_name": "customer",
                "active": True,
                "deleted": 0,
                "properties": {"models_groups": ["hr"]},
            }
        ]
    )
    env = _Env(
        {"customer": customer, "component": component},
        session=_session(groups=["sales"]),
    )
    service = Service(env)

    response = asyncio.run(service.load_record("customer", "c1"))

    assert response.content.data == {}
    assert response.content.obfucated_fields == []


def test_models_groups_allows_member_group():
    customer = _RecordModel("customer", rows=[{"rec_name": "c1", "name": "Ada"}])
    component = _ComponentModel(
        [
            {
                "rec_name": "customer",
                "active": True,
                "deleted": 0,
                "properties": {"models_groups": ["hr"]},
            }
        ]
    )
    env = _Env(
        {"customer": customer, "component": component},
        session=_session(groups=["hr"]),
    )
    service = Service(env)

    response = asyncio.run(service.load_record("customer", "c1"))

    assert response.content.data["name"] == "Ada"


def test_models_groups_bypassed_for_admin():
    customer = _RecordModel("customer", rows=[{"rec_name": "c1", "name": "Ada"}])
    component = _ComponentModel(
        [
            {
                "rec_name": "customer",
                "active": True,
                "deleted": 0,
                "properties": {"models_groups": ["hr"]},
            }
        ]
    )
    env = _Env(
        {"customer": customer, "component": component},
        session=_session(groups=[], is_admin=True),
    )
    service = Service(env)

    response = asyncio.run(service.load_record("customer", "c1"))

    assert response.content.data["name"] == "Ada"


def test_models_restricted_fields_hides_field_for_non_allowed_group():
    customer = _RecordModel(
        "customer", rows=[{"rec_name": "c1", "name": "Ada", "salary": 42}]
    )
    component = _ComponentModel(
        [
            {
                "rec_name": "customer",
                "active": True,
                "deleted": 0,
                "properties": {
                    "models_restricted_fields": {"salary": ["hr"]}
                },
            }
        ]
    )
    env = _Env(
        {"customer": customer, "component": component},
        session=_session(groups=["sales"]),
    )
    service = Service(env)

    response = asyncio.run(service.load_record("customer", "c1"))

    assert response.content.data["name"] == "Ada"
    assert "salary" not in response.content.data


def test_apply_session_groups_matches_uid_in_group_users():
    session = _session()
    group_users = _GroupUsersModel(
        [
            {
                "group": "manager",
                "users": ["u1", "u2"],
                "app_code": "demo",
                "active": True,
                "deleted": 0,
            },
            {"group": "legacy", "users": ["u1"], "active": True, "deleted": 0},
            {
                "group": "other-app",
                "users": ["u1"],
                "app_code": "other",
                "active": True,
                "deleted": 0,
            },
            {"group": "dpo", "users": ["u9"], "active": True, "deleted": 0},
        ]
    )
    groups_model = _GroupUsersModel(
        [
            {
                "rec_name": "manager",
                "implied_groups": ["user", "operator"],
                "active": True,
                "deleted": 0,
            },
            {
                "rec_name": "operator",
                "implied_groups": [],
                "active": True,
                "deleted": 0,
            },
            {
                "rec_name": "user",
                "implied_groups": [],
                "active": True,
                "deleted": 0,
            },
        ]
    )
    env = _Env({"group_users": group_users, "groups": groups_model}, session=session)

    groups = asyncio.run(apply_session_groups(env, session))

    assert groups == ["manager", "operator", "user"]
    assert session.user["groups"] == ["manager", "operator", "user"]


def test_apply_session_groups_empty_when_no_match():
    session = _session()
    group_users = _GroupUsersModel(
        [{"group": "dpo", "users": ["u9"], "active": True, "deleted": 0}]
    )
    env = _Env({"group_users": group_users}, session=session)

    groups = asyncio.run(apply_session_groups(env, session))

    assert groups == []
    assert session.user["groups"] == []


def test_apply_session_groups_adds_groups_from_mongo_rule():
    session = _session()
    group_users = _GroupUsersModel([])
    groups_model = _GroupUsersModel(
        [
            {
                "rec_name": "technical_operator",
                "rule": "{\"department\":\"it\"}",
                "implied_groups": ["user"],
                "active": True,
                "deleted": 0,
            }
        ]
    )
    user_model = _UserRuleModel([{"rec_name": "u1", "active": True, "deleted": 0}])
    env = _Env(
        {"group_users": group_users, "groups": groups_model, "user": user_model},
        session=session,
    )

    groups = asyncio.run(apply_session_groups(env, session))

    assert groups == ["technical_operator", "user"]
    serialized_domain = json.dumps(user_model.domains[0], sort_keys=True)
    assert '"active": true' in serialized_domain
    assert '"department": "it"' in serialized_domain
    assert '"rec_name": "u1"' in serialized_domain


def test_group_users_schema_has_editable_app_code_with_cookie_default():
    components = json.loads(Path("app/base/schema/components.json").read_text())
    group_users = next(
        item for item in components if item["rec_name"] == "group_users"
    )
    app_code = next(
        item for item in group_users["components"] if item.get("key") == "app_code"
    )

    assert app_code["type"] == "textfield"
    assert app_code["input"] is True
    assert app_code["validate"]["required"] is True
    assert "document.cookie" in app_code["customDefaultValue"]
    assert "app_code" in app_code["customDefaultValue"]


def test_groups_schema_and_seed_define_default_implications():
    components = json.loads(Path("app/base/schema/components.json").read_text())
    groups_schema = next(item for item in components if item["rec_name"] == "groups")
    implied_groups = next(
        item
        for item in groups_schema["components"]
        if item.get("key") == "implied_groups"
    )
    rule = next(
        item for item in groups_schema["components"] if item.get("key") == "rule"
    )
    groups = json.loads(Path("app/base/data/groups.json").read_text())
    by_name = {item["rec_name"]: item for item in groups}

    assert implied_groups["type"] == "select"
    assert implied_groups["multiple"] is True
    assert implied_groups["properties"]["model"] == "groups"
    assert rule["type"] == "textarea"
    assert rule["properties"]["jeditor"] == "y"
    assert by_name["manager"]["implied_groups"] == ["user", "operator"]
    assert by_name["manager"]["rule"] == "{}"
    assert by_name["technical_operator"]["label"] == "Technical Operator"
