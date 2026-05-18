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
