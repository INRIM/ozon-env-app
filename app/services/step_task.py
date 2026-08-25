from __future__ import annotations

from typing import TYPE_CHECKING
from typing import Any

from fastapi import HTTPException

from app.services.common import ResponseObject

if TYPE_CHECKING:
    from app.services.service import Service


async def complete_step_task(
    service: "Service",
    model_name: str,
    name: str,
    payload: dict[str, Any],
) -> ResponseObject:
    """Completa uno step configurato sul model e salva il form.

    Il model arriva dal path — come per ``/record/{model}/{rec_name}`` —
    NON dal body: il client (formio) posta la submission del record, che
    non contiene il nome del model.

    Il salvataggio passa da ``Service.upsert``: in questo modo usa gli ACL
    applicativi e l'hook ``send_mail_update`` gia' responsabile della coda.
    """
    data = payload.copy()
    model_name = str(model_name or "").strip()
    if not model_name:
        raise HTTPException(status_code=422, detail="model is required")

    rec_name = str(data.get("rec_name") or "").strip()
    if not rec_name:
        raise HTTPException(status_code=422, detail="rec_name is required")

    record_model = service._get_model(model_name)
    step_name = str(name or "").strip()
    step_field_name = f"todo_supervised_{step_name}"
    step_config = record_model.model.step_field(step_field_name)
    if not step_config:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Step '{step_name}' is not configured for model "
                f"'{model_name}'"
            ),
        )

    current = await service.load_record(model_name, rec_name)
    if current.fail:
        raise HTTPException(
            status_code=404,
            detail=current.message or f"Record '{rec_name}' not found",
        )
    if not current.content.editable:
        raise HTTPException(
            status_code=403,
            detail=(
                f"Record '{rec_name}' of model '{model_name}' "
                "is not writable"
            ),
        )

    # La key del checkbox la registra `ModelMaker.eval_fieldset` leggendola
    # dal componente: le palette ufficiali spediscono "todo", ma chi
    # disegna il form puo' rinominarlo. Ricostruirla per convenzione qui
    # sarebbe una supposizione.
    checkbox_name = str(step_config.get("checkbox_field") or "").strip()
    if not checkbox_name:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Step '{step_name}' has no checkbox component in its "
                "fieldset"
            ),
        )
    if checkbox_name not in record_model.model.model_fields:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Step '{step_name}' checkbox field '{checkbox_name}' "
                f"is not a field of model '{model_name}'"
            ),
        )

    data[checkbox_name] = False
    return await service.upsert(model_name, data, rec_name=rec_name)
