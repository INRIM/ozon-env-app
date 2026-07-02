import asyncio
import types

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.core.models import FieldAclOperation
from app.services import message_queue as mq
from app.services.common import ResponseObject, ResponseObjectData


class FakeTemplateModel:
    def __init__(self, by_name=None, find_rows=None):
        self._by_name = by_name or {}
        self._find_rows = find_rows or []
        self.find_domains = []

    async def by_name(self, name):
        return self._by_name.get(name)

    async def find(self, domain, limit=0):
        self.find_domains.append(domain)
        return list(self._find_rows)


class FakeEnv:
    def __init__(self, models):
        self._models = models

    def get(self, name):
        return self._models.get(name)


class FakeService:
    def __init__(self, env, component=None):
        self.env = env
        self._component = component
        self.upserts = []

    async def upsert(self, model_name, data=None, rec_name=""):
        self.upserts.append(
            {"model": model_name, "data": data, "rec_name": rec_name}
        )
        return ResponseObject(
            content=ResponseObjectData(mode="form", model=model_name, data=data)
        )

    async def _get_component_record(self, name):
        return self._component


def _template(rec_name):
    return types.SimpleNamespace(rec_name=rec_name)


def _service_with_templates(by_name=None, find_rows=None, component=None):
    env = FakeEnv(
        {"mail_template": FakeTemplateModel(by_name=by_name, find_rows=find_rows)}
    )
    return FakeService(env, component=component)


# --------------------------- enqueue ---------------------------------------

def test_enqueue_creates_record_da_inviare():
    svc = _service_with_templates(by_name={"welcome": _template("welcome")})

    asyncio.run(mq.enqueue(svc, "welcome", "rec-1"))

    assert len(svc.upserts) == 1
    call = svc.upserts[0]
    assert call["model"] == "message_queue"
    assert call["data"]["mail_template"] == "welcome"
    assert call["data"]["rel_rec_name"] == "rec-1"
    assert call["data"]["stato"] == mq.STATO_DA_INVIARE
    assert call["rec_name"].startswith("mq_")
    assert call["data"]["rec_name"] == call["rec_name"]


def test_enqueue_unknown_template_404():
    svc = _service_with_templates(by_name={})

    with pytest.raises(HTTPException) as exc:
        asyncio.run(mq.enqueue(svc, "missing", "rec-1"))
    assert exc.value.status_code == 404
    assert svc.upserts == []


def test_enqueue_requires_fields():
    svc = _service_with_templates(by_name={"welcome": _template("welcome")})

    with pytest.raises(HTTPException) as exc:
        asyncio.run(mq.enqueue(svc, "", "rec-1"))
    assert exc.value.status_code == 422
    with pytest.raises(HTTPException):
        asyncio.run(mq.enqueue(svc, "welcome", ""))


# --------------------- maybe_enqueue_on_save -------------------------------

def test_auto_enqueue_insert_flag_on():
    svc = _service_with_templates(
        by_name={"welcome": _template("welcome")},
        find_rows=[_template("welcome")],
        component=types.SimpleNamespace(properties={"send_mail_create": "1"}),
    )

    asyncio.run(mq.maybe_enqueue_on_save(svc, "ordini", "ord-1", FieldAclOperation.INSERT.value))

    assert len(svc.upserts) == 1
    assert svc.upserts[0]["data"]["mail_template"] == "welcome"
    assert svc.upserts[0]["data"]["rel_rec_name"] == "ord-1"
    # ha cercato i template default per quel model
    domain = svc.env.get("mail_template").find_domains[0]
    assert domain["model"] == "ordini"
    assert domain["default"] is True


def test_auto_enqueue_insert_flag_off():
    svc = _service_with_templates(
        by_name={"welcome": _template("welcome")},
        find_rows=[_template("welcome")],
        component=types.SimpleNamespace(properties={"send_mail_create": "0"}),
    )

    asyncio.run(mq.maybe_enqueue_on_save(svc, "ordini", "ord-1", FieldAclOperation.INSERT.value))

    assert svc.upserts == []


def test_auto_enqueue_update_flag_on():
    svc = _service_with_templates(
        by_name={"welcome": _template("welcome")},
        find_rows=[_template("welcome")],
        component=types.SimpleNamespace(properties={"send_mail_update": "1"}),
    )

    asyncio.run(mq.maybe_enqueue_on_save(svc, "ordini", "ord-1", "update"))

    assert len(svc.upserts) == 1


def test_auto_enqueue_update_skipped_when_only_create_flag():
    svc = _service_with_templates(
        by_name={"welcome": _template("welcome")},
        find_rows=[_template("welcome")],
        component=types.SimpleNamespace(properties={"send_mail_create": "1"}),
    )

    asyncio.run(mq.maybe_enqueue_on_save(svc, "ordini", "ord-1", "update"))

    assert svc.upserts == []


def test_auto_enqueue_no_default_template():
    svc = _service_with_templates(
        by_name={"welcome": _template("welcome")},
        find_rows=[],
        component=types.SimpleNamespace(properties={"send_mail_create": "1"}),
    )

    asyncio.run(mq.maybe_enqueue_on_save(svc, "ordini", "ord-1", FieldAclOperation.INSERT.value))

    assert svc.upserts == []


def test_auto_enqueue_skips_internal_models():
    svc = _service_with_templates(
        component=types.SimpleNamespace(properties={"send_mail_create": "1"}),
    )

    for model in ("message_queue", "mail_template", "component", "action"):
        asyncio.run(mq.maybe_enqueue_on_save(svc, model, "x", FieldAclOperation.INSERT.value))

    assert svc.upserts == []


def test_auto_enqueue_best_effort_swallows_errors():
    # _get_component_record solleva: l'hook non deve propagare.
    class Boom(FakeService):
        async def _get_component_record(self, name):
            raise RuntimeError("boom")

    svc = Boom(FakeEnv({}))
    asyncio.run(mq.maybe_enqueue_on_save(svc, "ordini", "ord-1", FieldAclOperation.INSERT.value))
    assert svc.upserts == []


# ------------------------------ endpoint -----------------------------------

def test_enqueue_endpoint():
    from app.api.message_queue_router import router
    from app.deps.app_env import get_authed_env, get_service

    svc = _service_with_templates(by_name={"welcome": _template("welcome")})

    test_app = FastAPI()
    test_app.include_router(router)
    test_app.dependency_overrides[get_authed_env] = lambda: None
    test_app.dependency_overrides[get_service] = lambda: svc

    client = TestClient(test_app)
    resp = client.post(
        "/message_queue/enqueue",
        json={"mail_template": "welcome", "rel_rec_name": "rec-9"},
    )

    assert resp.status_code == 200
    assert len(svc.upserts) == 1
    assert svc.upserts[0]["data"]["rel_rec_name"] == "rec-9"
