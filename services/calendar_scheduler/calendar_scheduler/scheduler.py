from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from .config import SchedulerConfig
from .gateway import CalendarGateway
from .parser import CalendarParseError
from .parser import parse_calendar_expression

logger = logging.getLogger("calendar_scheduler")

_ACTION_PAUSE = "pause"
_ACTION_RESUME = "resume"
_ACTION_REMOVE = "remove"
_ACTION_MANUAL = "manuale"


def _is_truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "si", "x"}
    return bool(value)


class CalendarScheduler:
    """Sincronizza i task calendar (tutti gli app_code) ed esegue le run.

    Data plane via ozon-env (gateway), execution via HTTP run endpoint.
    Persistenza job su jobstore SQLAlchemy. Concorrenza 1 run per task.
    """

    def __init__(
        self,
        gateway: CalendarGateway,
        config: SchedulerConfig,
        *,
        scheduler: AsyncIOScheduler | None = None,
    ) -> None:
        self.gateway = gateway
        self.config = config
        self.tz = ZoneInfo(config.timezone)
        self.scheduler = scheduler or AsyncIOScheduler(
            jobstores={
                "default": SQLAlchemyJobStore(url=config.jobstore_url)
            },
            job_defaults={
                "coalesce": True,
                "max_instances": 1,
                "misfire_grace_time": config.misfire_grace_time,
            },
            timezone=self.tz,
        )

    # --- lifecycle ------------------------------------------------------

    def start(self) -> None:
        _RUNTIME.bind(self)
        if not self.scheduler.running:
            self.scheduler.start()

    def shutdown(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=True)

    # --- sync -----------------------------------------------------------

    async def sync_once(self) -> int:
        tasks = await self.gateway.list_tasks()
        for task in tasks:
            try:
                await self._apply_task(task)
            except CalendarParseError as exc:
                await self._mark_config_error(task, str(exc))
            except Exception:  # noqa: BLE001 - un task rotto non ferma il sync
                logger.exception(
                    "sync task fallito rec_name=%s", task.get("rec_name", "")
                )
        return len(tasks)

    async def _apply_task(self, task: dict[str, Any]) -> None:
        rec_name = str(task.get("rec_name", "") or "").strip()
        if not rec_name:
            return
        action = str(task.get("action", "") or "").strip().lower()
        deleted = task.get("deleted", 0)
        active = _is_truthy(task.get("active", False))

        if deleted not in (0, "0", 0.0) or action == _ACTION_REMOVE:
            self._remove_job(rec_name)
            return
        if action == _ACTION_PAUSE:
            self._pause_job(rec_name)
            return
        if action == _ACTION_RESUME:
            self._resume_job(rec_name)
            return
        if action == _ACTION_MANUAL:
            # esecuzione immediata gestita dall'endpoint run, non schedulata
            return
        if not active:
            self._remove_job(rec_name)
            return

        await self._register_job(rec_name, task)

    async def _register_job(
        self, rec_name: str, task: dict[str, Any]
    ) -> None:
        expression = str(task.get("calendar", "") or "").strip()
        if not expression:
            raise CalendarParseError(
                f"calendar expression empty for {rec_name}"
            )
        app_code = str(task.get("app_code", "") or "").strip()
        parsed = parse_calendar_expression(
            expression, timezone=self.config.timezone
        )
        trigger = parsed.to_trigger()
        self.scheduler.add_job(
            run_calendar_job,
            trigger=trigger,
            args=[rec_name, app_code],
            id=rec_name,
            name=rec_name,
            replace_existing=True,
        )
        job = self.scheduler.get_job(rec_name)
        next_run = job.next_run_time if job else None
        await self.gateway.write_next(rec_name, next_run)
        logger.info(
            "job registrato rec_name=%s app_code=%s expr=%s next=%s",
            rec_name,
            app_code,
            expression,
            next_run.isoformat() if next_run else None,
        )

    def _remove_job(self, rec_name: str) -> None:
        if self.scheduler.get_job(rec_name):
            self.scheduler.remove_job(rec_name)
            logger.info("job rimosso rec_name=%s", rec_name)

    def _pause_job(self, rec_name: str) -> None:
        if self.scheduler.get_job(rec_name):
            self.scheduler.pause_job(rec_name)
            logger.info("job in pausa rec_name=%s", rec_name)

    def _resume_job(self, rec_name: str) -> None:
        if self.scheduler.get_job(rec_name):
            self.scheduler.resume_job(rec_name)
            logger.info("job ripreso rec_name=%s", rec_name)

    async def _mark_config_error(
        self, task: dict[str, Any], message: str
    ) -> None:
        rec_name = str(task.get("rec_name", "") or "").strip()
        if not rec_name:
            return
        self._remove_job(rec_name)
        logger.error(
            "errore configurazione rec_name=%s: %s", rec_name, message
        )
        try:
            await self.gateway.mark_config_error(rec_name)
        except Exception:  # noqa: BLE001
            logger.exception(
                "mark_config_error fallito rec_name=%s", rec_name
            )

    # --- esecuzione -----------------------------------------------------

    async def execute_task(self, rec_name: str, app_code: str = "") -> None:
        run_id = uuid.uuid4().hex
        scheduled_time = datetime.now(self.tz).isoformat()
        lock = await self.gateway.acquire_lock(
            rec_name, run_id, scheduled_time
        )
        if not lock.get("locked"):
            logger.warning(
                "lock non acquisito rec_name=%s current=%s",
                rec_name,
                lock.get("current_run_id", ""),
            )
            return
        try:
            logger.info(f"Run task {run_id}")
            await self.gateway.run_task(
                rec_name,
                app_code,
                {
                    "run_id": run_id,
                    "scheduled_time": scheduled_time,
                    "trigger": "scheduler",
                    "attempt": 1,
                },
            )
            # Lo stato/last/active li ha aggiornati l'endpoint run; il worker
            # comunica solo il prossimo fire calcolato dallo scheduler.
            job = self.scheduler.get_job(rec_name)
            next_run = job.next_run_time if job else None
            await self.gateway.write_next(rec_name, next_run)
        except Exception:  # noqa: BLE001 - nessun retry automatico (decisione)
            logger.exception("run task fallita rec_name=%s", rec_name)
        finally:
            try:
                await self.gateway.release_lock(rec_name, run_id)
            except Exception:  # noqa: BLE001
                logger.exception("release lock fallito rec_name=%s", rec_name)


class _Runtime:
    """Registry di processo: il job persistente referenzia una funzione globale,
    ma ha bisogno dello scheduler vivo per gateway/config."""

    def __init__(self) -> None:
        self._scheduler: CalendarScheduler | None = None

    def bind(self, scheduler: CalendarScheduler) -> None:
        self._scheduler = scheduler

    def get(self) -> CalendarScheduler:
        if self._scheduler is None:
            raise RuntimeError("CalendarScheduler runtime not bound")
        return self._scheduler


_RUNTIME = _Runtime()


async def run_calendar_job(rec_name: str, app_code: str = "") -> None:
    """Entrypoint job APScheduler: riferimento globale richiesto dal jobstore
    persistente."""
    await _RUNTIME.get().execute_task(rec_name, app_code)
