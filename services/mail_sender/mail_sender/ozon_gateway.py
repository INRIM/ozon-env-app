from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("mail_sender")

MESSAGE_QUEUE_MODEL = "message_queue"
MAIL_TEMPLATE_MODEL = "mail_template"
MAIL_SERVER_MODEL = "mail_server_out"

STATO_DA_INVIARE = "da_inviare"
STATO_INVIATO = "inviato"
STATO_IN_ERRORE = "in_errore"


class OzonGateway:
    """Accesso al DB tramite il **model layer** di ozon-env.

    Usa `env.get(model)` -> `find` / `by_name` / `update` con CoreModel: le
    letture tornano CoreModel (il worker passa `get_dict()` a Jinja), lo stato
    si aggiorna mutando il record e chiamando `update()` (diff -> `$set`, mai
    `_id`). app_code-aware: la coda e' filtrata sui record della propria app o
    su quelli condivisi (`app_code` vuoto/assente).
    """

    def __init__(
        self, env: Any, app_info: dict[str, Any], app_code: str = ""
    ) -> None:
        self.env = env
        self._app_info = app_info
        self._app_code = str(app_code or "").strip()

    def app_info(self) -> dict[str, Any]:
        return self._app_info

    def _get(self, name: str) -> Any:
        model = self.env.get(name)
        if model is None:
            logger.warning("model '%s' non registrato in ozon-env", name)
        return model

    def _pending_domain(self) -> dict[str, Any]:
        stato = {"stato": STATO_DA_INVIARE}
        if not self._app_code:
            return stato
        return {
            "$and": [
                stato,
                {
                    "$or": [
                        {"app_code": self._app_code},
                        {"app_code": ""},
                        {"app_code": None},
                        {"app_code": {"$exists": False}},
                    ]
                },
            ]
        }

    # --- reads (CoreModel) --------------------------------------------------
    async def pending_messages(self) -> list[Any]:
        model = self._get(MESSAGE_QUEUE_MODEL)
        if model is None:
            return []
        return await model.find(domain=self._pending_domain(), limit=0) or []

    async def load_template(self, name: str) -> Any:
        return await self._by_name(MAIL_TEMPLATE_MODEL, name)

    async def load_server(self, name: str) -> Any:
        return await self._by_name(MAIL_SERVER_MODEL, name)

    async def load_record(self, model: str, rec_name: str) -> Any:
        return await self._by_name(model, rec_name)

    async def _by_name(self, model: str, name: str) -> Any:
        if not model or not name:
            return None
        record_model = self._get(model)
        if record_model is None:
            return None
        return await record_model.by_name(name)

    # --- writes (CoreModel.update) -----------------------------------------
    async def mark_sent(self, record: Any) -> None:
        await self._update_status(record, STATO_INVIATO, "")

    async def mark_error(self, record: Any, logs: str) -> None:
        await self._update_status(record, STATO_IN_ERRORE, logs)

    async def _update_status(self, record: Any, stato: str, logs: str) -> None:
        model = self._get(MESSAGE_QUEUE_MODEL)
        if model is None:
            return
        setattr(record, "stato", stato)
        setattr(record, "logs", logs)
        await model.update(record)
