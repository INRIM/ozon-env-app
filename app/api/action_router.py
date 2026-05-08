import logging
from typing import Annotated
from typing import Any

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request
from fastapi import Response
from fastapi.responses import JSONResponse

from app.deps.app_env import get_authed_env, get_service
from app.services.common import ResponseObject
from app.services.common import ResponseObjectData
from app.services.service import Service
from app.services.utils import check_parse_json

router = APIRouter(
    prefix="/action",
    tags=["Actions"],
    dependencies=[Depends(get_authed_env)],
)
logger = logging.getLogger("uvicorn.error")


async def _read_json_payload(request: Request) -> dict[str, Any]:
    try:
        payload = await request.json()
    except Exception:
        return {}

    if isinstance(payload, dict):
        return payload.copy()
    if isinstance(payload, str):
        parsed = check_parse_json(payload)
        if isinstance(parsed, dict):
            return parsed
    return {}


def _enforce_dashboard_mode(payload: ResponseObjectData) -> ResponseObjectData:
    if payload.mode == "card":
        return payload
    return ResponseObjectData(
        mode="card",
        data=payload.data,
        readable=payload.readable,
        editable=payload.editable,
        can_create=payload.can_create,
        model=payload.model,
        query=payload.query,
        obfucated_fields=payload.obfucated_fields,
        editable_fields=payload.editable_fields,
        schema=payload.schema,
        rec_name=payload.rec_name,
        fields=payload.fields,
        columns=payload.columns,
        filter_kyes=payload.filter_kyes,
        batch_size=payload.batch_size,
        total_count=payload.total_count,
    )


def _wrap_response(payload: ResponseObjectData) -> ResponseObject:
    fail = False
    message = ""
    data = payload.data
    if isinstance(data, dict):
        status_value = str(data.get("status", "")).lower()
        if status_value == "error":
            fail = True
            message = str(data.get("message", "") or data.get("msg", ""))
        elif bool(data.get("fail", False)):
            fail = True
            message = str(data.get("message", "") or data.get("msg", ""))
    return ResponseObject(content=payload, fail=fail, message=message)


@router.get("/menu")
async def get_action_menu(
        service: Annotated[Service, Depends(get_service)],
) -> ResponseObject:
    logger.info("action menu request")
    payload = await service.service_get_menu()
    return _wrap_response(payload)


@router.get("/menu/{parent}")
async def get_action_menu_parent(
        parent: str,
        service: Annotated[Service, Depends(get_service)],
) -> ResponseObject:
    logger.info("action menu request parent=%s", parent)
    payload = await service.service_get_menu(parent=parent)
    return _wrap_response(payload)


@router.get("/dashboard")
async def get_action_dashboard(
        service: Annotated[Service, Depends(get_service)],
) -> ResponseObject:
    logger.info("action dashboard request")
    resp = await service.service_get_dashboard()
    normalized = _enforce_dashboard_mode(resp)
    logger.info("action dashboard response mode=%s", normalized.mode)
    return _wrap_response(normalized)


@router.get("/dashboard/{parent}")
async def get_action_dashboard_parent(
        parent: str,
        service: Annotated[Service, Depends(get_service)],
) -> ResponseObject:
    logger.info("action dashboard request parent=%s", parent)
    resp = await service.service_get_dashboard(parent=parent)
    normalized = _enforce_dashboard_mode(resp)
    logger.info("action dashboard response parent=%s mode=%s", parent, normalized.mode)
    return _wrap_response(normalized)


@router.get("/layout")
async def get_action_layout(
        service: Annotated[Service, Depends(get_service)],
        name: str = "",
) -> ResponseObject:
    logger.info("action layout request name=%s", name)
    payload = await service.service_get_layout(name=name)
    return _wrap_response(payload)


@router.get("/layout/{name}")
async def get_action_layout_name(
        name: str,
        service: Annotated[Service, Depends(get_service)],
) -> ResponseObject:
    logger.info("action layout request by path name=%s", name)
    payload = await service.service_get_layout(name=name)
    return _wrap_response(payload)


@router.get("/next_action/{curr_action}")
@router.get("/next_action/{curr_action}/{rec_name}")
async def get_action_next_action(
        curr_action: str,
        service: Annotated[Service, Depends(get_service)],
        rec_name: str = "",
) -> Response:
    logger.info(
        "action next redirect request curr_action=%s rec_name=%s",
        curr_action,
        rec_name,
    )
    target = await service.service_get_next_action_redirect(
        curr_action=curr_action,
        rec_name=rec_name,
    )
    if not target:
        logger.info("action next redirect empty curr_action=%s rec_name=%s", curr_action, rec_name)
        return Response(status_code=204)
    logger.info("action next redirect curr_action=%s target=%s", curr_action, target)
    return JSONResponse(
        content={
            "mode": "redirect",
            "data": {
                "next_page": target,
            },
        }
    )


@router.get("/{name}")
async def get_action(
        name: str,
        service: Annotated[Service, Depends(get_service)],
        query: str = "{}",
        order: str = "",
        skip: int = 0,
        limit: int = 100,
) -> ResponseObject:
    logger.info("action get request name=%s order=%s skip=%s limit=%s", name, order, skip, limit)
    parsed_query = check_parse_json(query)
    if not isinstance(parsed_query, dict):
        raise HTTPException(status_code=422, detail="query must be a valid JSON object")

    payload = await service.service_handle_action_get(
        action_name=name,
        rec_name="",
        query=parsed_query,
        order=order,
        skip=skip,
        limit=limit,
    )
    return _wrap_response(payload)


@router.get("/{name}/{rec_name}")
async def get_action_rec_name(
        name: str,
        rec_name: str,
        service: Annotated[Service, Depends(get_service)],
        query: str = "{}",
        order: str = "",
        skip: int = 0,
        limit: int = 100,
) -> ResponseObject:
    logger.info("action get request name=%s rec_name=%s", name, rec_name)
    parsed_query = check_parse_json(query)
    if not isinstance(parsed_query, dict):
        raise HTTPException(status_code=422, detail="query must be a valid JSON object")

    payload = await service.service_handle_action_get(
        action_name=name,
        rec_name=rec_name,
        query=parsed_query,
        order=order,
        skip=skip,
        limit=limit,
    )
    return _wrap_response(payload)


@router.post("/{name}")
async def post_action(
        name: str,
        request: Request,
        service: Annotated[Service, Depends(get_service)],
) -> ResponseObject:
    payload = await _read_json_payload(request)
    logger.info("action post request name=%s payload_keys=%s", name, list(payload.keys()))
    response_payload = await service.service_handle_action_post(
        action_name=name,
        data=payload,
    )
    return _wrap_response(response_payload)


@router.post("/{name}/{rec_name}")
async def post_action_rec_name(
        name: str,
        rec_name: str,
        request: Request,
        service: Annotated[Service, Depends(get_service)],
) -> ResponseObject:
    payload = await _read_json_payload(request)
    logger.info(
        "action post request name=%s rec_name=%s payload_keys=%s",
        name,
        rec_name,
        list(payload.keys()),
    )
    response_payload = await service.service_handle_action_post(
        action_name=name,
        data=payload,
        rec_name=rec_name,
    )
    return _wrap_response(response_payload)


@router.delete("/{name}/{rec_name}")
async def delete_action_rec_name(
        name: str,
        rec_name: str,
        request: Request,
        service: Annotated[Service, Depends(get_service)],
) -> ResponseObject:
    payload = await _read_json_payload(request)
    logger.info("action delete request name=%s rec_name=%s", name, rec_name)
    response_payload = await service.service_handle_action_delete(
        action_name=name,
        rec_name=rec_name,
        data=payload,
    )
    return _wrap_response(response_payload)
