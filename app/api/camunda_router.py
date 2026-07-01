from __future__ import annotations

import logging
from typing import Annotated
from typing import Any

from fastapi import APIRouter
from fastapi import Body
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query

from app.deps.app_env import get_authed_env
from app.deps.app_env import get_service
from app.services.common import ResponseObject
from app.services.service import Service

router = APIRouter(
    prefix="/gateway/camunda",
    tags=["Camunda Gateway"],
    dependencies=[Depends(get_authed_env)],
)
logger = logging.getLogger("uvicorn.error")


def _update_data_enabled(
    update_data: bool,
    update_data_legacy: bool,
) -> bool:
    return update_data or update_data_legacy


def _resolve_process_id(process_id: str, payload: dict[str, Any]) -> str:
    """Risolve il process_id (process instance key).

    Allo start (update_data) il process_id viene salvato nel form; quindi il
    frontend, completando, manda il form col `process_id` nel payload. Si legge
    quindi dal payload (form/data/variables), col path come fallback esplicito.
    """
    path_value = str(process_id or "").strip()
    if path_value and path_value != "-":
        return path_value
    for container_key in ("form", "data", "variables"):
        container = payload.get(container_key)
        if isinstance(container, dict):
            value = str(container.get("process_id") or "").strip()
            if value:
                return value
    return str(payload.get("process_id") or "").strip()


def _camunda_http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, LookupError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, ValueError):
        return HTTPException(status_code=422, detail=str(exc))
    return HTTPException(status_code=500, detail=str(exc))


@router.post("/start/{process_key}")
async def start_process(
    process_key: str,
    service: Annotated[Service, Depends(get_service)],
    payload: Annotated[dict[str, Any], Body(default_factory=dict)],
    update_data: Annotated[
        bool,
        Query(alias="update-data"),
    ] = False,
    update_data_legacy: Annotated[
        bool,
        Query(alias="update_data"),
    ] = False,
) -> ResponseObject:
    logger.info("camunda gateway start process_key=%s", process_key)
    try:
        return await service.start_camunda_gateway_process(
            process_key,
            payload,
            update_data=_update_data_enabled(update_data, update_data_legacy),
        )
    except (LookupError, ValueError) as exc:
        raise _camunda_http_error(exc) from exc


@router.post("/start/{process_model}/{process_name}")
async def start_process_for_model(
    process_model: str,
    process_name: str,
    service: Annotated[Service, Depends(get_service)],
    payload: Annotated[dict[str, Any], Body(default_factory=dict)],
    update_data: Annotated[
        bool,
        Query(alias="update-data"),
    ] = False,
    update_data_legacy: Annotated[
        bool,
        Query(alias="update_data"),
    ] = False,
) -> ResponseObject:
    logger.info(
        "camunda gateway start process_model=%s process_name=%s",
        process_model,
        process_name,
    )
    try:
        return await service.start_camunda_gateway_process(
            process_name,
            payload,
            update_data=_update_data_enabled(update_data, update_data_legacy),
            process_model=process_model,
        )
    except (LookupError, ValueError) as exc:
        raise _camunda_http_error(exc) from exc


@router.get("/status/{process_id}")
async def process_status(
    process_id: str,
    service: Annotated[Service, Depends(get_service)],
) -> ResponseObject:
    logger.info("camunda gateway status process_id=%s", process_id)
    try:
        return await service.get_camunda_gateway_status(process_id)
    except (LookupError, ValueError) as exc:
        raise _camunda_http_error(exc) from exc


@router.post("/complete")
@router.post("/complete/{process_id}")
async def complete_task(
    service: Annotated[Service, Depends(get_service)],
    payload: Annotated[dict[str, Any], Body(default_factory=dict)],
    process_id: str = "",
) -> ResponseObject:
    resolved = _resolve_process_id(process_id, payload)
    logger.info("camunda gateway complete process_id=%s", resolved)
    try:
        return await service.complete_camunda_gateway_task(resolved, payload)
    except (LookupError, ValueError) as exc:
        raise _camunda_http_error(exc) from exc


@router.post("/action/approved")
@router.post("/action/{process_id}/approved")
async def approve_task(
    service: Annotated[Service, Depends(get_service)],
    payload: Annotated[dict[str, Any], Body(default_factory=dict)],
    process_id: str = "",
) -> ResponseObject:
    resolved = _resolve_process_id(process_id, payload)
    logger.info("camunda gateway approve process_id=%s", resolved)
    try:
        return await service.complete_camunda_gateway_task(
            resolved,
            payload,
            decision="approved",
        )
    except (LookupError, ValueError) as exc:
        raise _camunda_http_error(exc) from exc


@router.post("/action/refused")
@router.post("/action/{process_id}/refused")
async def refuse_task(
    service: Annotated[Service, Depends(get_service)],
    payload: Annotated[dict[str, Any], Body(default_factory=dict)],
    process_id: str = "",
) -> ResponseObject:
    resolved = _resolve_process_id(process_id, payload)
    logger.info("camunda gateway refuse process_id=%s", resolved)
    try:
        return await service.complete_camunda_gateway_task(
            resolved,
            payload,
            decision="refused",
        )
    except (LookupError, ValueError) as exc:
        raise _camunda_http_error(exc) from exc


# --- batch: payload con `rec_names` (lista) -> task singolo per ognuno --------


@router.post("/complete_many")
async def complete_many(
    service: Annotated[Service, Depends(get_service)],
    payload: Annotated[dict[str, Any], Body(default_factory=dict)],
) -> ResponseObject:
    logger.info(
        "camunda gateway complete_many n=%s",
        len(payload.get("rec_names") or []),
    )
    return await service.complete_many_camunda_gateway_tasks(payload)


@router.post("/action/approve_many")
async def approve_many(
    service: Annotated[Service, Depends(get_service)],
    payload: Annotated[dict[str, Any], Body(default_factory=dict)],
) -> ResponseObject:
    logger.info(
        "camunda gateway approve_many n=%s",
        len(payload.get("rec_names") or []),
    )
    return await service.complete_many_camunda_gateway_tasks(
        payload, decision="approved"
    )


@router.post("/action/refuse_many")
async def refuse_many(
    service: Annotated[Service, Depends(get_service)],
    payload: Annotated[dict[str, Any], Body(default_factory=dict)],
) -> ResponseObject:
    logger.info(
        "camunda gateway refuse_many n=%s",
        len(payload.get("rec_names") or []),
    )
    return await service.complete_many_camunda_gateway_tasks(
        payload, decision="refused"
    )
