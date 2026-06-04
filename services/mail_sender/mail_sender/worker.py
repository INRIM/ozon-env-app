from __future__ import annotations

import asyncio
import logging
import traceback
from typing import Any

from .sender import MailError

logger = logging.getLogger("mail_sender")


def build_context(
    record: dict[str, Any], app_info: dict[str, Any]
) -> dict[str, Any]:
    """Context Jinja compatibile col vecchio sistema: data/form/user/app.

    In pull non c'e' una sessione: `user` e' best-effort dai campi owner_*
    del record correlato.
    """
    data = dict(record or {})
    user = {
        "uid": data.get("owner_uid", ""),
        "name": data.get("owner_name", ""),
        "mail": data.get("owner_mail", ""),
        "function": data.get("owner_function", ""),
        "sector": data.get("owner_sector", ""),
    }
    return {"data": data, "form": data, "user": user, "app": dict(app_info or {})}


class MailWorker:
    def __init__(
        self,
        gateway: Any,
        renderer: Any,
        sender: Any,
        *,
        send_timeout: float = 60.0,
    ) -> None:
        self.gateway = gateway
        self.renderer = renderer
        self.sender = sender
        self.send_timeout = send_timeout

    async def process_once(self) -> int:
        """Processa i message_queue `da_inviare` (CoreModel). Ritorna gli inviati."""
        records = await self.gateway.pending_messages()
        sent = 0
        for record in records:
            message = record.get_dict()
            rec_name = str(message.get("rec_name") or "").strip()
            if not rec_name:
                continue
            try:
                await self._process_one(message)
            except Exception as exc:  # noqa: BLE001 - log + segna in_errore
                logger.error(
                    "invio fallito rec_name=%s: %s", rec_name, exc
                )
                await self.gateway.mark_error(record, _format_error(exc))
                continue
            await self.gateway.mark_sent(record)
            sent += 1
            logger.info("inviato rec_name=%s", rec_name)
        return sent

    async def _process_one(self, message: dict[str, Any]) -> None:
        template_name = str(message.get("mail_template") or "").strip()
        rel_rec_name = str(message.get("rel_rec_name") or "").strip()
        if not template_name:
            raise MailError("message_queue senza mail_template")

        template = await self.gateway.load_template(template_name)
        if not template:
            raise MailError(f"mail_template '{template_name}' non trovato")
        template_data = template.get_dict()

        server_name = str(template_data.get("server") or "").strip()
        server = await self.gateway.load_server(server_name)
        if not server:
            raise MailError(f"mail_server_out '{server_name}' non trovato")

        related = await self.gateway.load_record(
            str(template_data.get("model") or "").strip(), rel_rec_name
        )
        record_data = related.get_dict() if related else {}

        context = build_context(record_data, self.gateway.app_info())
        subject, recipients, html = self.renderer.render(template_data, context)
        if not recipients:
            raise MailError("nessun destinatario risolto dal template")

        # smtplib e' bloccante: in thread per non bloccare il loop.
        await asyncio.wait_for(
            asyncio.to_thread(
                self.sender.send, server.get_dict(), subject, recipients, html
            ),
            timeout=self.send_timeout,
        )

    async def run_forever(self, interval: float) -> None:
        logger.info("mail_sender avviato (poll=%ss)", interval)
        while True:
            try:
                await self.process_once()
            except Exception:  # noqa: BLE001 - il loop non deve morire
                logger.exception("errore nel ciclo di poll")
            await asyncio.sleep(interval)


def _format_error(exc: Exception) -> str:
    return "".join(
        traceback.format_exception(type(exc), exc, exc.__traceback__)
    ).strip()
