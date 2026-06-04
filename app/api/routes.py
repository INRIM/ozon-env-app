import json
import logging
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from pydantic import ValidationError

from ozonenv.OzonEnv import OzonEnv

from app.deps.app_env import get_authed_env, get_ozon_env, get_service
from app.services.components.selectComponentService import (
    build_remote_select_header,
)
from app.services.common import (
    ListRequest,
    RemoteSelectRequest,
    ResponseObject,
    make_response_object,
)
from app.services.remote_service import remote_data_select_response
from app.services.service import Service
from app.services.utils import _stream_ndjson_with_start_packet, check_parse_json

router = APIRouter(dependencies=[Depends(get_authed_env)])
logger = logging.getLogger("uvicorn.error")


def _dump_model(instance: Any) -> dict[str, Any]:
    if hasattr(instance, "model_dump"):
        return instance.model_dump(by_alias=True)
    return instance.dict(by_alias=True)


def _safe_encode_payload(payload: Any) -> Any:
    try:
        return jsonable_encoder(payload)
    except Exception:
        if hasattr(payload, "model_dump"):
            payload = payload.model_dump(mode="python", by_alias=True)
        elif hasattr(payload, "dict"):
            payload = payload.dict(by_alias=True)
        elif hasattr(payload, "get_dict"):
            payload = payload.get_dict()
        try:
            return jsonable_encoder(payload)
        except Exception:
            return json.loads(json.dumps(payload, default=str))


def _coerce_body_model(model_cls: type[BaseModel], payload: Any) -> BaseModel:
    normalized_payload = payload
    if isinstance(normalized_payload, (bytes, bytearray)):
        normalized_payload = normalized_payload.decode("utf-8", errors="ignore")

    # Some clients send JSON as text/plain or double-encode the payload.
    # Parse repeatedly until we reach a structured object or stop making progress.
    for _ in range(3):
        if not isinstance(normalized_payload, str):
            break
        parsed_payload = check_parse_json(normalized_payload)
        if parsed_payload == normalized_payload:
            break
        normalized_payload = parsed_payload
    try:
        if hasattr(model_cls, "model_validate"):
            return model_cls.model_validate(normalized_payload)
        return model_cls.parse_obj(normalized_payload)
    except ValidationError as exc:
        errors = []
        for item in exc.errors():
            normalized_error = item.copy()
            loc = normalized_error.get("loc", ())
            if not isinstance(loc, tuple):
                loc = tuple(loc) if isinstance(loc, list) else tuple()
            if not loc or loc[0] != "body":
                normalized_error["loc"] = ("body", *loc)
            errors.append(normalized_error)
        raise RequestValidationError(errors) from exc


def _coerce_body_dict(payload: Any) -> dict[str, Any]:
    normalized_payload = payload
    if isinstance(normalized_payload, (bytes, bytearray)):
        normalized_payload = normalized_payload.decode("utf-8", errors="ignore")

    for _ in range(3):
        if not isinstance(normalized_payload, str):
            break
        parsed_payload = check_parse_json(normalized_payload)
        if parsed_payload == normalized_payload:
            break
        normalized_payload = parsed_payload

    if isinstance(normalized_payload, dict):
        return normalized_payload.copy()

    raise RequestValidationError([
        {
            "type": "dict_type",
            "loc": ("body",),
            "msg": "Input should be a valid dictionary",
            "input": normalized_payload,
        }
    ])


@router.get("/")
async def healthcheck() -> dict[str, Any]:
    logger.info("healthcheck request received")
    return {"status": "live"}


@router.get("/get_session")
async def get_session(
    request: Request,
    ozon_env: Annotated[OzonEnv, Depends(get_ozon_env)],
    app_code: str = Query(
        default="",
        description=(
            "Optional app code override for the current request. "
            "When provided, it takes precedence over the `app_code` cookie "
            "and the server default APP_CODE."
        ),
    ),
) -> dict[str, Any]:
    requested_app_code = app_code or request.cookies.get("app_code", "")
    logger.info(
        "get session request received requested_app_code=%s resolved_app_code=%s",
        requested_app_code,
        getattr(ozon_env.user_session, "app_code", ""),
    )
    session_data = _safe_encode_payload(ozon_env.user_session)
    logger.info("get session response ready")
    return session_data


@router.get("/models/distinct")
async def get_models_distinct(
    service: Annotated[Service, Depends(get_service)],
) -> ResponseObject:
    logger.info("list models request received")
    data = await service.get_models()
    logger.info("list models request completed count=%s", len(data))
    return make_response_object(data=data, mode="list")

@router.post("/models/distinct")
async def post_models_distinct(
    payload_raw: Annotated[Any, Body(...)],
    service: Annotated[Service, Depends(get_service)],
) -> ResponseObject:
    payloadr = _coerce_body_model(RemoteSelectRequest, payload_raw)
    logger.info(
        "models distinct payload key=%s curr_model=%s has_properties=%s",
        payloadr.key,
        payloadr.curr_model,
        payloadr.has_properties(),
    )
    if not payloadr.has_properties():
        data = await service.get_models()
        logger.info("models distinct fallback to model list count=%s", len(data))
    else:
        if not payloadr.key or not payloadr.curr_model:
            logger.warning("models distinct missing key/curr_model, using model list")
            data = await service.get_models()
            return make_response_object(data=data, mode="list")
        data = await service.get_select_options(
            payloadr.key,
            payloadr.curr_model,
        )
        logger.info("models distinct select options generated count=%s", len(data))
    return make_response_object(data=data, mode="list")

@router.get("/record/{model}")
async def get_record_schema(
    model: str,
    service: Annotated[Service, Depends(get_service)],
) -> ResponseObject:
    logger.info("record schema request model=%s", model)
    resp_data = await service.compo_by_name("component", model)
    logger.info("record schema response ready model=%s", model)
    return resp_data


@router.post("/list/{model}", response_model=None)
async def post_list_records(
    model: str,
    payload_raw: Annotated[Any, Body(...)],
    service: Annotated[Service, Depends(get_service)],
    stream: bool = Query(default=True),
) -> Any:
    payload = _coerce_body_model(ListRequest, payload_raw)
    logger.info(
        "list request model=%s order=%s skip=%s limit=%s stream=%s",
        model,
        payload.order,
        payload.skip,
        payload.limit,
        stream,
    )
    envelope = await service.list_records(
        model_name=model,
        query=payload.query,
        order=payload.order,
        skip=payload.skip,
        limit=payload.limit,
        resp_stream=stream,
        batch_size=10,
    )
    if not stream:
        logger.info(
            "list response prepared model=%s total_count=%s stream=%s",
            model,
            envelope.content.total_count,
            stream,
        )
        return envelope

    cols = envelope.content.columns
    total_count = envelope.content.total_count

    data = await service.stream_record(
        envelope,
        order=payload.order,
        skip=payload.skip,
        limit=payload.limit,
    )
    columns_value = list(cols.keys()) if isinstance(cols, dict) else cols
    logger.info(
        "list stream prepared model=%s columns=%s total_count=%s",
        model,
        columns_value,
        total_count,
    )

    return StreamingResponse(
        _stream_ndjson_with_start_packet(data, envelope),
        media_type="application/x-ndjson",
        headers={
            "X-Order": payload.order,
            "X-Skip": str(payload.skip),
            "X-Limit": str(payload.limit),
            "X-columns": str(cols),
            "X-Total-Count": str(total_count),
        },
    )


@router.get("/record/{model}/{rec_name}")
async def get_record(
    model: str,
    rec_name: str,
    service: Annotated[Service, Depends(get_service)],
) -> ResponseObject:
    logger.info("record load request model=%s rec_name=%s", model, rec_name)
    resp_data = await service.load_record(model, rec_name)
    if not resp_data:
        logger.warning("record not found model=%s rec_name=%s", model, rec_name)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Record '{rec_name}' not found on model '{model}'",
        )
    logger.info("record load response ready model=%s rec_name=%s", model, rec_name)
    return resp_data


@router.post("/record/{model}/{rec_name}")
async def post_update_record(
    model: str,
    rec_name: str,
    payload_raw: Annotated[Any, Body(...)],
    service: Annotated[Service, Depends(get_service)],
) -> ResponseObject:
    payload = _coerce_body_dict(payload_raw)
    logger.info("record upsert request model=%s rec_name=%s", model, rec_name)
    logger.info(payload)
    resp_data = await service.upsert(model, payload, rec_name=rec_name)
    if not resp_data:
        logger.warning("record upsert failed model=%s rec_name=%s", model, rec_name)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Record '{rec_name}' not found on model '{model}'",
        )
    logger.info("record upsert completed model=%s rec_name=%s", model, rec_name)
    return resp_data


@router.post("/import/{model}")
async def post_import_component(
    model: str,
    payload_raw: Annotated[Any, Body(...)],
    service: Annotated[Service, Depends(get_service)],
) -> ResponseObject:
    payload = _coerce_body_dict(payload_raw)
    rec_name = str(payload.get("rec_name", "") or "").strip()
    logger.info(f"{model} import request rec_name={rec_name}")
    resp_data = await service.upsert(
        model,
        payload,
        rec_name=rec_name,
        sync_component_runtime=True,
        generate_component_defaults=False,
    )
    logger.info("component import completed rec_name=%s", rec_name)
    return resp_data


@router.post("/get_remote_data_select")
@router.post("/get_remote_select")
async def post_remote_data_select(
    payload_raw: Annotated[Any, Body(...)],
    service: Annotated[Service, Depends(get_service)],
) -> ResponseObject:
    payloadr = _coerce_body_model(RemoteSelectRequest, payload_raw)
    logger.info(
        "remote select payload key=%s curr_model=%s url=%s",
        payloadr.key,
        payloadr.curr_model,
        payloadr.data.url,
    )
    data: list[Any] = []

    if payloadr.key and payloadr.curr_model:
        data = await service.get_select_options(
            payloadr.key,
            payloadr.curr_model,
        )
    elif payloadr.data.url:
        header_data = build_remote_select_header(_dump_model(payloadr.data))
        data = await remote_data_select_response(
            service=service,
            url=header_data.url,
            path_value=header_data.path_value,
            header_key=header_data.header_key,
            header_value_key=header_data.header_value_key,
        )
    else:
        logger.warning("remote select payload missing key/curr_model and url")

    logger.info("remote select response count=%s", len(data))
    return make_response_object(data=data, mode="list")
