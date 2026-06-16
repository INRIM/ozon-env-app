from __future__ import annotations

import logging
from datetime import datetime
from datetime import timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx

logger = logging.getLogger("calendar_scheduler")

CALENDAR_MODEL = "calendar"
# Lock persistito nel campo libero `data_value` del record calendar: model layer
# ozon-env, nessun campo a schema, nessuna collection raw.
LOCK_KEY = "scheduler_lock"


def _parse_iso(value: str, tz: ZoneInfo) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = f"{raw[:-1]}+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return dt.replace(tzinfo=tz) if dt.tzinfo is None else dt


class CalendarGateway:
    """Data plane via **ozon-env** (agnostico sull'app_code), execution via HTTP.

    - lettura/lock/`next` dei record `calendar` passano dal model layer ozon-env
      su un'unica Mongo condivisa: i record di tutti gli app_code sono visibili
      (le query calendar non filtrano per app_code);
    - l'esecuzione del task resta una chiamata HTTP all'endpoint run dell'app,
      con l'`app_code` del record, perche' l'azione gira nel runtime applicativo
      coi plugin giusti. `stato`/`last`/`active` li scrive quell'endpoint.
    """

    def __init__(
        self,
        env: Any,
        *,
        run_base_url: str,
        token_provider: Any,
        lock_ttl_seconds: int,
        timezone: str = "Europe/Rome",
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.env = env
        self.tz = ZoneInfo(timezone)
        self.lock_ttl_seconds = lock_ttl_seconds
        # token_provider.authorization() -> "Bearer <jwt>" rinnovato (M2M).
        self._token_provider = token_provider
        self._run_base_url = run_base_url.rstrip("/")
        self._http = http_client or httpx.AsyncClient(
            base_url=self._run_base_url, timeout=30.0
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    def _model(self) -> Any:
        model = self.env.get(CALENDAR_MODEL)
        if model is None:
            logger.error("model 'calendar' non registrato in ozon-env")
        return model

    # --- reads ----------------------------------------------------------

    async def list_tasks(self) -> list[dict[str, Any]]:
        model = self._model()
        if model is None:
            return []
        rows = await model.find(
            domain={"$and": [{"tipo": "task"}, {"deleted": 0}]}, limit=0
        )
        return [r.get_dict() for r in (rows or [])]

    # --- lock (data_value via model.update, full-record diff) -----------

    def _read_lock(self, data: dict[str, Any]) -> dict[str, Any] | None:
        dv = data.get("data_value")
        if not isinstance(dv, dict):
            return None
        lock = dv.get(LOCK_KEY)
        return lock if isinstance(lock, dict) else None

    def _lock_active(self, lock: dict[str, Any], now: datetime) -> bool:
        until = _parse_iso(str(lock.get("locked_until", "") or ""), self.tz)
        return until is not None and until > now

    async def _set_lock(self, record: Any, lock: dict[str, Any] | None) -> None:
        # Muta il record COMPLETO e usa model.update: l'ORM diffa l'intero
        # record, mai un upsert parziale (che cancellerebbe i campi non passati).
        dv = dict(getattr(record, "data_value", {}) or {})
        if lock is None:
            dv.pop(LOCK_KEY, None)
        else:
            dv[LOCK_KEY] = lock
        record.data_value = dv
        await self._model().update(record)

    async def acquire_lock(
        self, rec_name: str, run_id: str, scheduled_time: str
    ) -> dict[str, Any]:
        model = self._model()
        if model is None:
            return {"locked": False, "rec_name": rec_name}
        record = await model.by_name(rec_name)
        if not record:
            return {"locked": False, "rec_name": rec_name}
        data = record.get_dict()
        now = datetime.now(self.tz)
        existing = self._read_lock(data)
        if (
            existing is not None
            and existing.get("run_id") != run_id
            and self._lock_active(existing, now)
        ):
            return {
                "locked": False,
                "rec_name": rec_name,
                "current_run_id": existing.get("run_id", ""),
            }
        locked_until = now + timedelta(seconds=self.lock_ttl_seconds)
        await self._set_lock(
            record,
            {
                "run_id": run_id,
                "scheduled_time": scheduled_time,
                "locked_until": locked_until.isoformat(),
            },
        )
        return {
            "locked": True,
            "rec_name": rec_name,
            "run_id": run_id,
            "locked_until": locked_until.isoformat(),
        }

    async def release_lock(self, rec_name: str, run_id: str) -> dict[str, Any]:
        model = self._model()
        if model is None:
            return {"released": False, "rec_name": rec_name}
        record = await model.by_name(rec_name)
        if not record:
            return {"released": True, "rec_name": rec_name}
        existing = self._read_lock(record.get_dict())
        now = datetime.now(self.tz)
        if (
            existing is not None
            and existing.get("run_id") != run_id
            and self._lock_active(existing, now)
        ):
            return {"released": False, "rec_name": rec_name}
        if existing is not None:
            await self._set_lock(record, None)
        return {"released": True, "rec_name": rec_name}

    # --- next (model.update, fresh record) ------------------------------

    async def write_next(
        self, rec_name: str, next_run: datetime | None
    ) -> None:
        model = self._model()
        if model is None:
            return
        # Rilettura fresca dopo la run: l'endpoint ha gia scritto stato/last,
        # cosi il diff tocca solo `next` e non li sovrascrive con valori stale.
        record = await model.by_name(rec_name)
        if not record:
            return
        record.next = next_run
        await model.update(record)

    async def mark_config_error(self, rec_name: str) -> None:
        # Errore di parsing della schedule: e' competenza del worker (avviene
        # prima della run), quindi lo scrive via ozon-env. Non c'e' fallback
        # silenzioso (regola del piano).
        model = self._model()
        if model is None:
            return
        record = await model.by_name(rec_name)
        if not record:
            return
        record.stato = "erroreConfigurazione"
        record.active = False
        record.next = None
        await model.update(record)

    # --- execution (HTTP run endpoint) ----------------------------------

    async def run_task(
        self, rec_name: str, app_code: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        params = {"app_code": app_code} if app_code else None
        headers = {"Authorization": await self._token_provider.authorization()}
        resp = await self._http.post(
            f"/client/run/calendar_tasks/{rec_name}",
            params=params,
            json=payload,
            headers=headers,
        )
        resp.raise_for_status()
        return resp.json()
