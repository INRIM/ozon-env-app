import json
import asyncio
from pathlib import Path
from types import SimpleNamespace
import pytest

from app.services.service import Service
from app.services.action_runtime import ActionRuntime


class FakeUserSession:
    def __init__(self, is_admin=False, groups=None, uid="test_user"):
        self.is_admin = is_admin
        self.is_public = False
        self.uid = uid
        self.app_code = "test_app"
        self.user = {"uid": uid, "groups": groups or []}


class FakeRecord(SimpleNamespace):
    def get_dict(self):
        return self.__dict__


class FakeModel:
    def __init__(self, name, records=None):
        self.name = name
        self.records = records or []

    async def find(self, domain=None, sort=None, limit=0, **kwargs):
        # Filter mock records
        res = []
        for r in self.records:
            # simple filter checks
            match = True
            if isinstance(domain, dict):
                for k, v in domain.items():
                    if k == "$and" and isinstance(v, list):
                        for sub in v:
                            for sk, sv in sub.items():
                                if r.get(sk) != sv:
                                    match = False
                    elif r.get(k) != v:
                        match = False
            if match:
                res.append(FakeRecord(**r) if isinstance(r, dict) else r)
        return res

    async def count(self, domain=None):
        found = await self.find(domain=domain)
        return len(found)

    async def by_name(self, name):
        for r in self.records:
            if r.get("rec_name") == name:
                return FakeRecord(**r)
        return None


class FakeEnv:
    def __init__(self, models):
        self.models = models

    def get(self, name):
        return self.models.get(name)


def test_schema_components_have_groups_field():
    components_path = Path("app/base/schema/components.json")
    assert components_path.exists()
    components = json.loads(components_path.read_text())
    
    # 1. Test menu_group schema
    menu_group_schema = next(c for c in components if c.get("rec_name") == "menu_group")
    groups_field = None
    for item in menu_group_schema["components"]:
        if item.get("key") == "columns":
            for col in item.get("columns", []):
                for comp in col.get("components", []):
                    if comp.get("key") == "groups":
                        groups_field = comp
    assert groups_field is not None
    assert groups_field["type"] == "select"
    assert groups_field["multiple"] is True
    assert groups_field["properties"]["model"] == "groups"

    # 2. Test action schema
    action_schema = next(c for c in components if c.get("rec_name") == "action")
    groups_field_action = next(c for c in action_schema["components"] if c.get("key") == "groups")
    assert groups_field_action is not None
    assert groups_field_action["type"] == "select"
    assert groups_field_action["multiple"] is True
    assert groups_field_action["properties"]["model"] == "groups"


def test_is_action_allowed():
    # Setup ActionRuntime
    service = SimpleNamespace(
        session=FakeUserSession(is_admin=False, groups=["technical_operator"])
    )
    runtime = ActionRuntime(service)

    # 1. Admin action on identity layer is not allowed for technical_operator
    action_user = SimpleNamespace(
        admin=True,
        sys=True,
        model="user",
        groups=[]
    )
    assert runtime._is_action_allowed(action_user) is False

    # 2. Admin action on standard model is allowed for technical_operator
    action_std = SimpleNamespace(
        admin=True,
        sys=True,
        model="standard",
        groups=[]
    )
    assert runtime._is_action_allowed(action_std) is True

    # 3. Action with explicit group restriction is allowed if user belongs to the group
    action_explicit = SimpleNamespace(
        admin=False,
        sys=False,
        model="custom",
        groups=["special_operator"]
    )
    # user is technical_operator, not special_operator -> False
    assert runtime._is_action_allowed(action_explicit) is False

    # user has special_operator -> True
    service.session.user["groups"] = ["special_operator"]
    assert runtime._is_action_allowed(action_explicit) is True

    # 4. Admin is always allowed
    service.session.is_admin = True
    service.session.user["groups"] = []
    assert runtime._is_action_allowed(action_user) is True
    assert runtime._is_action_allowed(action_explicit) is True


def test_is_menu_group_allowed():
    # Setup Service
    service = Service.__new__(Service)
    service.session = FakeUserSession(is_admin=False, groups=["technical_operator"])

    # 1. Identity menu group is restricted to Admin only
    mg_identity = SimpleNamespace(
        admin=True,
        rec_name="identity",
        groups=[]
    )
    assert service._is_menu_group_allowed(mg_identity) is False

    # 2. Config/other system menu groups are allowed for technical_operator
    mg_config = SimpleNamespace(
        admin=True,
        rec_name="config",
        groups=[]
    )
    assert service._is_menu_group_allowed(mg_config) is True

    # 3. Custom groups set on menu group
    mg_custom = SimpleNamespace(
        admin=False,
        rec_name="custom_mg",
        groups=["manager"]
    )
    assert service._is_menu_group_allowed(mg_custom) is False

    service.session.user["groups"] = ["manager"]
    assert service._is_menu_group_allowed(mg_custom) is True

    # 4. Admin is always allowed
    service.session.is_admin = True
    service.session.user["groups"] = []
    assert service._is_menu_group_allowed(mg_identity) is True
    assert service._is_menu_group_allowed(mg_custom) is True


def test_apply_session_groups_sets_is_tech():
    from app.ozon_env_acl import apply_session_groups
    
    session = SimpleNamespace(
        app_code="demo",
        uid="u1",
        is_admin=False,
        user={"uid": "u1", "groups": []},
    )
    
    # Mock environment with group_users model
    group_users_model = FakeModel("group_users", [
        {
            "rec_name": "gu1",
            "group": "technical_operator",
            "users": ["u1"],
            "app_code": "demo",
            "active": True,
            "deleted": 0,
        }
    ])
    env = FakeEnv({"group_users": group_users_model})
    
    # Run apply_session_groups
    groups = asyncio.run(apply_session_groups(env, session))
    
    # Should resolve groups and set is_tech
    assert "technical_operator" in groups
    assert getattr(session, "is_tech", None) is True

