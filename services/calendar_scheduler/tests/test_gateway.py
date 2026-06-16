from datetime import datetime
from datetime import timedelta
from zoneinfo import ZoneInfo

import httpx

from calendar_scheduler.gateway import CalendarGateway
from calendar_scheduler.gateway import LOCK_KEY

TZ = ZoneInfo("Europe/Rome")


class FakeRecord:
    """CoreModel-like: attributi settabili + get_dict()."""

    def __init__(self, data):
        for k, v in data.items():
            setattr(self, k, v)
        self._keys = set(data.keys())

    def __setattr__(self, key, value):
        super().__setattr__(key, value)
        if key != "_keys":
            keys = self.__dict__.get("_keys")
            if keys is not None:
                keys.add(key)

    def get_dict(self):
        return {k: getattr(self, k) for k in self._keys}


class FakeModel:
    """Model ozon-env in memoria: update(record) sostituisce il record COMPLETO
    (come l'ORM reale, che fa il diff sull'intero record)."""

    def __init__(self, records):
        self._store = {r["rec_name"]: dict(r) for r in records}

    async def find(self, domain=None, limit=0):
        return [FakeRecord(dict(r)) for r in self._store.values()]

    async def by_name(self, name):
        rec = self._store.get(name)
        return FakeRecord(dict(rec)) if rec is not None else None

    async def update(self, record):
        data = record.get_dict()
        self._store[data["rec_name"]] = data
        return record

    def raw(self, name):
        return self._store[name]


class FakeEnv:
    def __init__(self, model):
        self._model = model

    def get(self, name):
        return self._model if name == "calendar" else None


class FakeTokenProvider:
    async def authorization(self):
        return "Bearer jwt-123"


def _gateway(records, http_client=None):
    model = FakeModel(records)
    env = FakeEnv(model)
    gw = CalendarGateway(
        env,
        run_base_url="http://app",
        token_provider=FakeTokenProvider(),
        lock_ttl_seconds=600,
        timezone="Europe/Rome",
        http_client=http_client or httpx.AsyncClient(base_url="http://app"),
    )
    return gw, model


def _task(**over):
    base = {
        "rec_name": "update_model_access",
        "title": "Update Model Access",
        "calendar": "*-*-* 03:10:00",
        "task": "update_model_access",
        "app_code": "mci",
        "tipo": "task",
        "deleted": 0,
        "active": True,
        "data_value": {},
        "next": None,
        "stato": "progress",
    }
    base.update(over)
    return base


async def test_list_tasks_returns_dicts_across_app_codes():
    gw, _ = _gateway([_task(), _task(rec_name="t2", app_code="other")])
    tasks = await gw.list_tasks()
    apps = {t["app_code"] for t in tasks}
    assert apps == {"mci", "other"}


async def test_acquire_lock_grants_and_preserves_fields():
    gw, model = _gateway([_task()])
    res = await gw.acquire_lock("update_model_access", "run-1", "2026-01-01")
    assert res["locked"] is True
    stored = model.raw("update_model_access")
    assert stored["data_value"][LOCK_KEY]["run_id"] == "run-1"
    # full-record write: i campi non toccati restano
    assert stored["title"] == "Update Model Access"
    assert stored["calendar"] == "*-*-* 03:10:00"


async def test_acquire_lock_denied_when_active_other_run():
    future = (datetime.now(TZ) + timedelta(minutes=5)).isoformat()
    gw, _ = _gateway(
        [_task(data_value={LOCK_KEY: {"run_id": "old", "locked_until": future}})]
    )
    res = await gw.acquire_lock("update_model_access", "new", "2026-01-01")
    assert res["locked"] is False
    assert res["current_run_id"] == "old"


async def test_acquire_lock_grants_when_expired():
    past = (datetime.now(TZ) - timedelta(minutes=5)).isoformat()
    gw, model = _gateway(
        [_task(data_value={LOCK_KEY: {"run_id": "old", "locked_until": past}})]
    )
    res = await gw.acquire_lock("update_model_access", "new", "2026-01-01")
    assert res["locked"] is True
    assert model.raw("update_model_access")["data_value"][LOCK_KEY][
        "run_id"
    ] == "new"


async def test_release_lock_owner_clears():
    future = (datetime.now(TZ) + timedelta(minutes=5)).isoformat()
    gw, model = _gateway(
        [_task(data_value={LOCK_KEY: {"run_id": "r1", "locked_until": future}})]
    )
    res = await gw.release_lock("update_model_access", "r1")
    assert res["released"] is True
    assert LOCK_KEY not in model.raw("update_model_access")["data_value"]


async def test_release_lock_not_owner_keeps():
    future = (datetime.now(TZ) + timedelta(minutes=5)).isoformat()
    gw, model = _gateway(
        [_task(data_value={LOCK_KEY: {"run_id": "r1", "locked_until": future}})]
    )
    res = await gw.release_lock("update_model_access", "r2")
    assert res["released"] is False
    assert model.raw("update_model_access")["data_value"][LOCK_KEY][
        "run_id"
    ] == "r1"


async def test_write_next_sets_only_next():
    gw, model = _gateway([_task(stato="progress")])
    nxt = datetime(2026, 6, 17, 3, 10, tzinfo=TZ)
    await gw.write_next("update_model_access", nxt)
    stored = model.raw("update_model_access")
    assert stored["next"] == nxt
    # non tocca stato (lo possiede l'endpoint run)
    assert stored["stato"] == "progress"


async def test_mark_config_error():
    gw, model = _gateway([_task()])
    await gw.mark_config_error("update_model_access")
    stored = model.raw("update_model_access")
    assert stored["stato"] == "erroreConfigurazione"
    assert stored["active"] is False
    assert stored["next"] is None


async def test_run_task_calls_endpoint_with_app_code():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"status": "ok"})

    client = httpx.AsyncClient(
        base_url="http://app",
        transport=httpx.MockTransport(handler),
    )
    gw, _ = _gateway([_task()], http_client=client)
    res = await gw.run_task("update_model_access", "mci", {"run_id": "r1"})
    assert res["status"] == "ok"
    assert "app_code=mci" in seen["url"]
    assert "/client/run/calendar_tasks/update_model_access" in seen["url"]
    # bearer M2M iniettato per-request dal token provider
    assert seen["auth"] == "Bearer jwt-123"
