from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from calendar_scheduler.config import SchedulerConfig
from calendar_scheduler.scheduler import CalendarScheduler


class FakeGateway:
    """Sostituisce CalendarGateway: registra le chiamate, niente Mongo/HTTP."""

    def __init__(self, tasks=None, lock_locked=True):
        self._tasks = tasks or []
        self._lock_locked = lock_locked
        self.calls: list[tuple[str, Any]] = []

    async def list_tasks(self):
        return list(self._tasks)

    async def acquire_lock(self, rec_name, run_id, scheduled_time):
        self.calls.append(("lock", rec_name))
        if self._lock_locked:
            return {"locked": True, "rec_name": rec_name, "run_id": run_id}
        return {"locked": False, "rec_name": rec_name, "current_run_id": "x"}

    async def release_lock(self, rec_name, run_id):
        self.calls.append(("release", rec_name))
        return {"released": True}

    async def run_task(self, rec_name, app_code, payload):
        self.calls.append(("run", (rec_name, app_code)))
        return {"status": "ok", "rec_name": rec_name}

    async def write_next(self, rec_name, next_run):
        self.calls.append(("next", rec_name))

    async def mark_config_error(self, rec_name):
        self.calls.append(("config_error", rec_name))


def _config():
    return SchedulerConfig(
        run_base_url="http://app",
        oauth_token_url="http://kc/token",
        oauth_client_id="scheduler",
        oauth_client_secret="secret",
        oauth_audience="",
        oauth_scope="",
        poll_interval=45,
        lock_ttl_seconds=1800,
        http_timeout=30,
        timezone="Europe/Rome",
        jobstore_url="sqlite://",
        misfire_grace_time=300,
        health_file="/tmp/h",
    )


def _make(gateway):
    return CalendarScheduler(
        gateway, _config(), scheduler=AsyncIOScheduler(timezone="Europe/Rome")
    )


def _task(**over):
    base = {
        "rec_name": "update_model_access",
        "app_code": "mci",
        "tipo": "task",
        "calendar": "*-*-* 03:10:00",
        "action": "add",
        "active": True,
        "deleted": 0,
    }
    base.update(over)
    return base


async def test_sync_registers_active_task_and_writes_next():
    gw = FakeGateway(tasks=[_task()])
    sched = _make(gw)
    sched.scheduler.start()
    try:
        count = await sched.sync_once()
        assert count == 1
        assert sched.scheduler.get_job("update_model_access") is not None
        assert ("next", "update_model_access") in gw.calls
    finally:
        sched.scheduler.shutdown(wait=False)


async def test_sync_removes_on_action_remove():
    gw = FakeGateway(tasks=[_task(calendar="@daily")])
    sched = _make(gw)
    sched.scheduler.start()
    try:
        await sched.sync_once()
        assert sched.scheduler.get_job("update_model_access") is not None
        gw._tasks = [_task(calendar="@daily", action="remove", active=False)]
        await sched.sync_once()
        assert sched.scheduler.get_job("update_model_access") is None
    finally:
        sched.scheduler.shutdown(wait=False)


async def test_sync_invalid_expression_marks_config_error():
    gw = FakeGateway(tasks=[_task(rec_name="bad", calendar="Mon..Fri 03:10:00")])
    sched = _make(gw)
    sched.scheduler.start()
    try:
        await sched.sync_once()
        assert sched.scheduler.get_job("bad") is None
        assert ("config_error", "bad") in gw.calls
    finally:
        sched.scheduler.shutdown(wait=False)


async def test_execute_task_locks_runs_next_releases():
    gw = FakeGateway()
    sched = _make(gw)
    await sched.execute_task("update_model_access", "mci")
    kinds = [c[0] for c in gw.calls]
    assert kinds == ["lock", "run", "next", "release"]
    # app_code propagato alla run HTTP
    run_call = next(c for c in gw.calls if c[0] == "run")
    assert run_call[1] == ("update_model_access", "mci")


async def test_execute_task_skips_when_lock_denied():
    gw = FakeGateway(lock_locked=False)
    sched = _make(gw)
    await sched.execute_task("update_model_access", "mci")
    kinds = [c[0] for c in gw.calls]
    assert kinds == ["lock"]
    assert "run" not in kinds
