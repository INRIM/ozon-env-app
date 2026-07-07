import asyncio
from types import SimpleNamespace

from app.ozon_env_acl.model_rules_sync import sync_all_model_rules
from app.ozon_env_acl.model_rules_sync import sync_model_rules


class _AsyncCursor:
    def __init__(self, rows):
        self._rows = list(rows)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._rows:
            raise StopAsyncIteration
        return self._rows.pop(0)


class _Collection:
    def __init__(self, rows=None):
        self.rows = list(rows or [])
        self.deleted = []
        self.inserted = []

    def find(self, query):
        return _AsyncCursor(self.rows)

    async def delete_many(self, query):
        self.deleted.append(query)

    async def insert_many(self, rows):
        self.inserted.extend(rows)


class _Engine:
    def __init__(self, collections):
        self.collections = collections

    def get_collection(self, name):
        return self.collections[name]


class _ValidatedRecord:
    """Passthrough: simula CoreModel.get_dict() senza validazione tipi
    (le row costruite da model_groups_rows/model_fields_rows sono gia'
    shaped correttamente dal chiamante, qui serve solo il round-trip
    async model.new(...).get_dict(...) usato da _validated_row)."""

    def __init__(self, data):
        self._data = dict(data)

    def get_dict(self, exclude=None):
        exclude = exclude or set()
        return {k: v for k, v in self._data.items() if k not in exclude}


class _PassthroughModel:
    async def new(self, data):
        return _ValidatedRecord(data)


class _Env:
    def __init__(self, engine):
        self.orm = SimpleNamespace(
            app_settings=SimpleNamespace(app_code="demo"),
            db=SimpleNamespace(engine=engine),
        )
        self._passthrough_model = _PassthroughModel()

    def get(self, name):
        return self._passthrough_model


def test_sync_model_rules_writes_flat_rows_using_orm_db_engine():
    groups = _Collection()
    fields = _Collection()
    env = _Env(_Engine({"model_groups_rule": groups, "model_fields_rule": fields}))
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
                        {
                            "groups": ["dpo"],
                            "actions": {"read": True},
                        }
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

    asyncio.run(sync_model_rules(env, schema))

    assert groups.deleted == [{"app_code": "demo", "model": "document"}]
    assert fields.deleted == [{"app_code": "demo", "model": "document"}]
    assert groups.inserted[0]["rec_name"] == "mgr.demo.document.manager"
    assert groups.inserted[0]["read"] is True
    assert groups.inserted[0]["update"] is True
    assert {row["rule_type"] for row in fields.inserted} == {"fields", "record"}


def test_sync_all_model_rules_normalizes_components_and_rewrites_tables():
    component = _Collection(
        rows=[
            {
                "rec_name": "document",
                "sys": False,
                "deleted": 0,
                "properties": {},
            }
        ]
    )
    groups = _Collection()
    fields = _Collection()
    env = _Env(
        _Engine(
            {
                "component": component,
                "model_groups_rule": groups,
                "model_fields_rule": fields,
            }
        )
    )

    asyncio.run(sync_all_model_rules(env))

    group_names = {row["group"] for row in groups.inserted}
    field_types = {row["rule_type"] for row in fields.inserted}
    assert {"admin", "user", "technical_operator", "operator", "manager", "dpo"} <= group_names
    assert field_types == {"fields", "record"}
