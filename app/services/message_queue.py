from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING
from typing import Any

from fastapi import HTTPException

from app.core.models import FieldAclOperation

if TYPE_CHECKING:
    from app.services.service import Service

logger = logging.getLogger("uvicorn.error")

MESSAGE_QUEUE_MODEL = "message_queue"
MAIL_TEMPLATE_MODEL = "mail_template"

STATO_DA_INVIARE = "da_inviare"
STATO_INVIATO = "inviato"
STATO_IN_ERRORE = "in_errore"

# Modelli per cui NON ha senso valutare l'auto-enqueue (evita ricorsione/rumore).
_SKIP_AUTO_MODELS: frozenset[str] = frozenset(
    {
        MESSAGE_QUEUE_MODEL,
        MAIL_TEMPLATE_MODEL,
        "mail_server_out",
        "component",
        "action",
    }
)


def _is_flag_on(value: Any) -> bool:
    """Il flag e' attivo solo a "1" (vedi component.properties.send_mail_*)."""
    return str(value or "0").strip() == "1"


async def enqueue(
    service: "Service",
    mail_template: str,
    rel_rec_name: str,
) -> Any:
    """Crea un record message_queue in stato `da_inviare`.

    Valida che il `mail_template` esista. Il rendering e l'invio sono a carico
    del mail service esterno (pull): qui si scrive solo il record in coda.
    """
    template_name = str(mail_template or "").strip()
    rel_name = str(rel_rec_name or "").strip()
    if not template_name:
        raise HTTPException(status_code=422, detail="mail_template is required")
    if not rel_name:
        raise HTTPException(status_code=422, detail="rel_rec_name is required")

    template_model = service.env.get(MAIL_TEMPLATE_MODEL)
    if template_model is None:
        raise HTTPException(
            status_code=404, detail="mail_template model not available"
        )
    template = await template_model.by_name(template_name)
    if not template:
        raise HTTPException(
            status_code=404,
            detail=f"mail_template '{template_name}' not found",
        )

    rec_name = f"mq_{uuid.uuid4().hex}"
    data = {
        "rec_name": rec_name,
        "mail_template": template_name,
        "rel_rec_name": rel_name,
        "stato": STATO_DA_INVIARE,
        "logs": "",
    }
    logger.info(
        "message_queue enqueue rec_name=%s mail_template=%s rel_rec_name=%s",
        rec_name,
        template_name,
        rel_name,
    )
    return await service.upsert(MESSAGE_QUEUE_MODEL, data, rec_name=rec_name)


async def _default_templates_for_model(
    service: "Service", model_name: str
) -> list[Any]:
    template_model = service.env.get(MAIL_TEMPLATE_MODEL)
    if template_model is None:
        return []
    domain = {
        "model": model_name,
        "default": True,
        "deleted": 0,
        "active": True,
    }
    found = await template_model.find(domain=domain, limit=0)
    return found if isinstance(found, list) else []


async def maybe_enqueue_on_save(
    service: "Service",
    model_name: str,
    rec_name: str,
    operation: str,
) -> None:
    """Hook post-save: accoda una mail se il component del model lo richiede.

    Il flag vive nel `component.properties` del model salvato:
    - INSERT -> `send_mail_create == "1"`
    - UPDATE -> `send_mail_update == "1"`
    Il template usato e' quello `default` per quel model. Best-effort: un
    errore qui non deve far fallire il salvataggio del record.
    """
    name = str(model_name or "").strip()
    target_rec_name = str(rec_name or "").strip()
    if not name or not target_rec_name or name in _SKIP_AUTO_MODELS:
        return

    try:
        component = await service._get_component_record(name)
        if not component:
            return
        properties = getattr(component, "properties", None) or {}
        if not isinstance(properties, dict):
            return

        if operation == FieldAclOperation.INSERT.value:
            flag_key = "send_mail_create"
        elif operation == FieldAclOperation.UPDATE.value:
            flag_key = "send_mail_update"
        else:
            flag_key = ""
        enabled = bool(flag_key) and _is_flag_on(properties.get(flag_key))
        # Senza questa riga il percorso e' muto: chi configura il flag nel
        # form design e non vede mail in coda non ha modo di distinguere
        # "flag non letto" da "nessun template default". Si logga solo se il
        # model ha davvero uno dei due flag nel component, cosi' i model che
        # non usano la mail non fanno rumore.
        if "send_mail_create" in properties or "send_mail_update" in properties:
            logger.info(
                "message_queue auto-enqueue check model=%s operation=%s "
                "flag_key=%s value=%r enabled=%s",
                name,
                operation,
                flag_key or "-",
                properties.get(flag_key) if flag_key else None,
                enabled,
            )
        if not enabled:
            return

        templates = await _default_templates_for_model(service, name)
        if not templates:
            logger.info(
                "message_queue auto-enqueue: no default template model=%s", name
            )
            return
        for template in templates:
            await enqueue(service, template.rec_name, target_rec_name)
    except Exception:
        logger.exception(
            "message_queue auto-enqueue failed model=%s rec_name=%s",
            name,
            target_rec_name,
        )
