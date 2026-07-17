from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException

from app.core.webhooks import WebhookDispatcher
from app.core.webhooks import WebhookEndpoint
from app.core.webhooks import parse_webhook_endpoints
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


class _Model:
    def __init__(self):
        self.data_model = "ticket"
        self.status = _Status()
        self.model = _Schema()
        self.table_columns = {"rec_name": "Name"}
        self.rows = [{"rec_name": "t1", "title": "Initial"}]
        self.upserts = []

    def get_domain(self, query):
        return query

    async def count(self, domain):
        return len(self.rows)

    async def find(self, **kwargs):
        return list(self.rows)

    async def by_name(self, name):
        for row in self.rows:
            if row.get("rec_name") == name:
                return row.copy()
        return {}

    async def upsert(
        self,
        data=None,
        rec_name="",
        data_value=None,
        trnf_config=None,
        fields_parser=None,
    ):
        record = dict(data or {})
        if rec_name:
            record["rec_name"] = rec_name
        self.upserts.append(record.copy())
        return record


class _Env:
    def __init__(self):
        self.model = _Model()
        self.user_session = SimpleNamespace(
            app_code="demo",
            uid="u1",
            # admin: questo test esercita il webhook data.before_write, non
            # l'ACL a livello di model (model_groups_rule, fail-closed per
            # i non-admin, non e' registrato in questo env fake).
            is_admin=True,
            user={"uid": "u1"},
        )
        self.orm = SimpleNamespace(app_settings=SimpleNamespace(admins=[]))

    def get(self, model_name):
        if model_name == "ticket":
            return self.model
        return None


class _FakeWebhooks:
    def __init__(self):
        self.events = []

    async def emit(self, event, *, context=None, payload=None):
        self.events.append(
            {
                "event": event,
                "context": context or {},
                "payload": payload or {},
            }
        )
        if event == "data.before_write":
            changed = dict(payload or {})
            changed["external_acl"] = "ok"
            return SimpleNamespace(payload=changed, data=None)
        return SimpleNamespace(payload=payload, data=None)


def test_parse_webhook_endpoints_accepts_object_config():
    endpoints = parse_webhook_endpoints(
        json.dumps(
            {
                "endpoints": [
                    {
                        "url": "http://hook.local/events",
                        "events": "data.before_write,user.after_create",
                    }
                ]
            }
        )
    )

    assert len(endpoints) == 1
    assert endpoints[0].accepts("data.before_write")
    assert not endpoints[0].accepts("data.after_write")


def test_webhook_dispatcher_mutates_payload_and_sends_signature():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = json.loads(request.content.decode("utf-8"))
        assert body["event"] == "data.before_write"
        return httpx.Response(
            200,
            json={"payload": {"rec_name": "t1", "approved_by_hook": True}},
        )

    transport = httpx.MockTransport(handler)
    dispatcher = WebhookDispatcher(
        enabled=True,
        endpoints=[
            WebhookEndpoint(
                url="http://hook.local/events",
                events=["data.before_write"],
            )
        ],
        signing_secret="secret",
        http_client_factory=lambda: httpx.AsyncClient(transport=transport),
    )

    result = asyncio.run(
        dispatcher.emit(
            "data.before_write",
            context={"model": "ticket"},
            payload={"rec_name": "t1"},
        )
    )

    assert result.payload == {"rec_name": "t1", "approved_by_hook": True}
    assert (
        requests[0].headers["x-ozon-webhook-signature"].startswith("sha256=")
    )


def test_webhook_dispatcher_can_deny_operation():
    dispatcher = WebhookDispatcher(
        enabled=True,
        endpoints=[WebhookEndpoint(url="http://hook.local/events")],
        http_client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    json={"allow": False, "message": "blocked by hook"},
                )
            )
        ),
    )

    with pytest.raises(HTTPException) as exc:
        asyncio.run(dispatcher.emit("data.before_write", payload={}))

    assert exc.value.status_code == 403
    assert exc.value.detail == "blocked by hook"


def test_service_upsert_uses_before_and_after_write_hooks():
    env = _Env()
    service = Service(env)
    hooks = _FakeWebhooks()
    service.webhooks = hooks

    response = asyncio.run(
        service.upsert("ticket", {"rec_name": "t1", "title": "Saved"}, "t1")
    )

    assert response.content.data["external_acl"] == "ok"
    assert env.model.upserts == [
        {"rec_name": "t1", "title": "Saved", "external_acl": "ok"}
    ]
    assert [event["event"] for event in hooks.events] == [
        "data.before_write",
        "data.after_write",
    ]


class _RaisingWebhooks:
    async def emit(self, event, *, context=None, payload=None):
        raise RuntimeError("hook down")


def test_calendar_task_event_completed():
    env = _Env()
    service = Service(env)
    hooks = _FakeWebhooks()
    service.webhooks = hooks

    result = {
        "status": "ok",
        "rec_name": "task_1",
        "task": "submit_x",
        "task_record_name": "rec_9",
        "run_id": "r1",
        "started_at": "2026-06-18T02:00:00",
        "finished_at": "2026-06-18T02:00:05",
        "message": "done",
    }
    asyncio.run(service._emit_calendar_task_event("task_1", result))

    assert len(hooks.events) == 1
    event = hooks.events[0]
    assert event["event"] == "calendar.task.completed"
    assert event["context"]["model"] == "calendar"
    assert event["context"]["rec_name"] == "task_1"
    assert event["payload"]["status"] == "ok"
    assert event["payload"]["task"] == "submit_x"
    assert event["payload"]["run_id"] == "r1"


def test_calendar_task_event_failed():
    env = _Env()
    service = Service(env)
    service.webhooks = _FakeWebhooks()

    result = {"status": "error", "rec_name": "task_2", "message": "boom"}
    asyncio.run(service._emit_calendar_task_event("task_2", result))

    assert service.webhooks.events[0]["event"] == "calendar.task.failed"


def test_calendar_task_event_is_fail_safe():
    env = _Env()
    service = Service(env)
    service.webhooks = _RaisingWebhooks()

    # Una webhook che esplode NON deve propagare: notifica esito, non blocca run.
    asyncio.run(
        service._emit_calendar_task_event("task_3", {"status": "ok"})
    )
