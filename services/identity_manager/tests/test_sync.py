import pytest
import json
from types import SimpleNamespace
from identity_manager.sync import IdentitySyncService, parse_mongo_query


def test_parse_mongo_query():
    # Valid JSON
    assert parse_mongo_query('{"department": "finance"}', "test") == {"department": "finance"}
    # Single quotes (Python dict style / demjson style)
    assert parse_mongo_query("{'department': 'finance'}", "test") == {"department": "finance"}
    # Unquoted keys (YAML style)
    assert parse_mongo_query("{department: 'finance'}", "test") == {"department": "finance"}
    # Invalid structure (no curly braces)
    assert parse_mongo_query("department: finance", "test") is None
    # Plain string
    assert parse_mongo_query("technical_operator", "test") is None
    # Empty string
    assert parse_mongo_query("", "test") is None


class FakeModel:
    def __init__(self, records):
        self.records = records
        self.updated_records = []

    async def find(self, domain=None, limit=0):
        res = []
        for r in self.records:
            match = True
            if isinstance(domain, dict):
                for k, v in domain.items():
                    if k == "$and" and isinstance(v, list):
                        for sub in v:
                            for sk, sv in sub.items():
                                if r.get(sk) != sv:
                                    match = False
                    elif k in r:
                        if r.get(k) != v:
                            match = False
            if match:
                res.append(SimpleNamespace(**r))
        return res

    async def update(self, record):
        self.updated_records.append(record)
        for r in self.records:
            if r.get("rec_name") == record.rec_name:
                r["users"] = record.users
                return record
        return record


class FakeEnv:
    def __init__(self, models):
        self.models = models

    def get(self, name):
        return self.models.get(name)


@pytest.mark.asyncio
async def test_identity_sync_service():
    group_users = [
        {
            "rec_name": "group_finance_json",
            "group": "finance",
            "rule": '{"department": "finance"}',
            "users": ["old_user"],
            "active": True,
            "deleted": 0,
        },
        {
            "rec_name": "group_finance_relaxed",
            "group": "finance",
            "rule": "{department: 'finance'}",
            "users": ["old_user"],
            "active": True,
            "deleted": 0,
        },
        {
            "rec_name": "group_hr_no_rule",
            "group": "hr",
            "rule": "",
            "users": ["hr_user"],
            "active": True,
            "deleted": 0,
        },
        {
            "rec_name": "group_invalid_rule_string",
            "group": "operators",
            "rule": "technical_operator",
            "users": ["operator_1"],
            "active": True,
            "deleted": 0,
        },
        {
            "rec_name": "group_invalid_rule_empty_dict",
            "group": "operators",
            "rule": "{}",
            "users": ["operator_2"],
            "active": True,
            "deleted": 0,
        }
    ]
    users = [
        {
            "rec_name": "user1",
            "department": "finance",
            "active": True,
            "deleted": 0,
        },
        {
            "rec_name": "user2",
            "department": "hr",
            "active": True,
            "deleted": 0,
        },
        {
            "rec_name": "user3",
            "department": "finance",
            "active": True,
            "deleted": 0,
        }
    ]

    group_users_model = FakeModel(group_users)
    user_model = FakeModel(users)
    env = FakeEnv({"group_users": group_users_model, "user": user_model})

    sync_service = IdentitySyncService(env)
    await sync_service.run_sync()

    # 1. Valid JSON rule must update to ["user1", "user3"]
    assert sorted(group_users[0]["users"]) == ["user1", "user3"]

    # 2. Relaxed key/quotes rule must update to ["user1", "user3"]
    assert sorted(group_users[1]["users"]) == ["user1", "user3"]

    # 3. No rule must remain unchanged
    assert group_users[2]["users"] == ["hr_user"]

    # 4. Invalid rule string (non-dict) must NOT be executed, leaving users untouched
    assert group_users[3]["users"] == ["operator_1"]

    # 5. Empty dict rule must NOT be executed, leaving users untouched
    assert group_users[4]["users"] == ["operator_2"]
