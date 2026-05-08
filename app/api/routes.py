import json
import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import StreamingResponse

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
from app.services.utils import _stream_ndjson_with_start_packet

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


@router.get("/")
async def healthcheck() -> dict[str, Any]:
    logger.info("healthcheck request received")
    return {"status": "live"}


@router.get("/get_session")
async def get_session(
    ozon_env: Annotated[OzonEnv, Depends(get_ozon_env)],
) -> dict[str, Any]:
    logger.info("get session request received")
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


@router.get("/record/{model}")
async def get_record_schema(
    model: str,
    service: Annotated[Service, Depends(get_service)],
) -> ResponseObject:
    logger.info("record schema request model=%s", model)
    resp_data = await service.compo_by_name("component", model)
    logger.info("record schema response ready model=%s", model)
    return resp_data


@router.post("/list/{model}")
async def post_list_records(
    model: str,
    payload: ListRequest,
    service: Annotated[Service, Depends(get_service)],
) -> StreamingResponse:
    logger.info(
        "list request model=%s order=%s skip=%s limit=%s",
        model,
        payload.order,
        payload.skip,
        payload.limit,
    )
    envelope = await service.list_records(
        model_name=model,
        query=payload.query,
        order=payload.order,
        skip=payload.skip,
        limit=payload.limit,
        resp_stream=True,
        batch_size=10,
    )
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
    payload: dict[str, Any],
    service: Annotated[Service, Depends(get_service)],
) -> ResponseObject:
    logger.info("record upsert request model=%s rec_name=%s", model, rec_name)
    resp_data = await service.upsert(model, payload, rec_name=rec_name)
    if not resp_data:
        logger.warning("record upsert failed model=%s rec_name=%s", model, rec_name)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Record '{rec_name}' not found on model '{model}'",
        )
    logger.info("record upsert completed model=%s rec_name=%s", model, rec_name)
    return resp_data


@router.post("/models/distinct")
async def post_models_distinct(
    payloadr: RemoteSelectRequest,
    service: Annotated[Service, Depends(get_service)],
) -> ResponseObject:
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


@router.post("/get_remote_data_select")
@router.post("/get_remote_select")
async def post_remote_data_select(
    payloadr: RemoteSelectRequest,
    service: Annotated[Service, Depends(get_service)],
) -> ResponseObject:
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
