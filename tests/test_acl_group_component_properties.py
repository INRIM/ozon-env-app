import asyncio
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
            {"group": "hr", "users": ["u1", "u2"], "active": True, "deleted": 0},
            {"group": "dpo", "users": ["u9"], "active": True, "deleted": 0},
        ]
    )
    env = _Env({"group_users": group_users}, session=session)

    groups = asyncio.run(apply_session_groups(env, session))

    assert groups == ["hr"]
    assert session.user["groups"] == ["hr"]


def test_apply_session_groups_empty_when_no_match():
    session = _session()
    group_users = _GroupUsersModel(
        [{"group": "dpo", "users": ["u9"], "active": True, "deleted": 0}]
    )
    env = _Env({"group_users": group_users}, session=session)

    groups = asyncio.run(apply_session_groups(env, session))

    assert groups == []
    assert session.user["groups"] == []
