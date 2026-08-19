import json
import logging
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from pydantic import ValidationError

from ozonenv.OzonEnv import OzonEnv

from app.app_settings import get_env_settings
from app.deps.app_env import get_authed_env, get_ozon_env, get_service
from app.services.common import (
    ListRequest,
    RemoteSelectRequest,
    ResponseObject,
    make_response_object,
)
from app.services.attachments import delete_attachment
from app.services.attachments import load_attachment_metadata
from app.services.attachments import load_record_attachment_file
from app.services.attachments import save_formio_attachment
from app.services.service import Service
from app.services.utils import _stream_ndjson_with_start_packet, check_parse_json

router = APIRouter(dependencies=[Depends(get_authed_env)])
logger = logging.getLogger("uvicorn.error")


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


def _session_response_data(session: Any) -> dict[str, Any]:
    session_data = session.get_dict()
    user = session_data.get("user")
    user_data = session_data.get("user_data", {})
    normalized_user = {
        **(user if isinstance(user, dict) else {}),
        "user_data": user_data if isinstance(user_data, dict) else {},
    }
    uid = str(
        session_data.get("uid")
        or session_data.get("user_uid")
        or normalized_user.get("uid")
        or ""
    ).strip()
    username = str(
        session_data.get("username")
        or normalized_user.get("username")
        or normalized_user.get("preferred_username")
        or uid
    ).strip()
    if uid:
        normalized_user["uid"] = uid
    if username:
        normalized_user["username"] = username
    session_data["user"] = normalized_user
    session_data["uid"] = uid
    session_data["username"] = username
    session_data["authenticated"] = bool(uid)
    for sensitive_key in ("token", "sso_token", "sso_refresh", "claims"):
        session_data.pop(sensitive_key, None)
    return session_data


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


async def _read_optional_body_dict(request: Request) -> dict[str, Any]:
    try:
        payload = await request.json()
    except Exception:
        return {}
    if isinstance(payload, dict):
        return payload.copy()
    if isinstance(payload, str):
        parsed = check_parse_json(payload)
        if isinstance(parsed, dict):
            return parsed.copy()
    return {}


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
    session_data = _session_response_data(ozon_env.user_session)
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


@router.post("/client/run/calendar_tasks/{rec_name}")
async def post_run_calendar_task(
    rec_name: str,
    request: Request,
    service: Annotated[Service, Depends(get_service)],
) -> dict[str, Any]:
    payload = await _read_optional_body_dict(request)
    logger.info(
        "calendar task run request rec_name=%s trigger=%s",
        rec_name,
        payload.get("trigger", "manual"),
    )
    return await service.run_calendar_task(rec_name, payload=payload)


@router.post("/client/attachment")
async def post_client_attachment(
    request: Request,
    ozon_env: Annotated[OzonEnv, Depends(get_ozon_env)],
) -> dict[str, Any]:
    settings = get_env_settings()
    app_code = str(getattr(ozon_env.user_session, "app_code", "") or "")
    return await save_formio_attachment(
        request=request,
        settings=settings,
        app_code=app_code,
    )


@router.get("/client/attachment/{attachment_id}")
async def get_client_attachment(
    attachment_id: str,
    ozon_env: Annotated[OzonEnv, Depends(get_ozon_env)],
) -> FileResponse:
    settings = get_env_settings()
    app_code = str(getattr(ozon_env.user_session, "app_code", "") or "")
    file_path, metadata = load_attachment_metadata(
        settings=settings,
        app_code=app_code,
        attachment_id=attachment_id,
    )
    return FileResponse(
        file_path,
        media_type=str(metadata.get("type") or "application/octet-stream"),
        filename=str(metadata.get("name") or "attachment"),
    )


@router.get("/client/attachment/{model}/{rec_name}/{filename}")
async def get_client_record_attachment(
    model: str,
    rec_name: str,
    filename: str,
    service: Annotated[Service, Depends(get_service)],
) -> FileResponse:
    settings = get_env_settings()
    # L'allegato eredita l'ACL del record che lo possiede: senza questo
    # gate bastava indovinare model/rec_name (che sono identificativi
    # leggibili, non capability) per scaricare allegati di record non
    # leggibili via /record/{model}/{rec_name}.
    #
    # `load_record` applica model_groups_rule + record_rules: quando il
    # read finale e' negato solleva direttamente HTTPException(404)
    # (service.py, ramo `if not final_read`), quindi il diniego ACL esce
    # di qui senza toccare il filesystem. Il `if not record` sotto copre
    # il caso record inesistente.
    record = await service.load_record(model, rec_name)
    if not record:
        logger.warning(
            "record attachment denied model=%s rec_name=%s uid=%s",
            model,
            rec_name,
            getattr(getattr(service, "session", None), "uid", ""),
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Attachment not found",
        )
    file_path, metadata = load_record_attachment_file(
        settings=settings,
        model=model,
        rec_name=rec_name,
        filename=filename,
    )
    return FileResponse(
        file_path,
        media_type=str(metadata.get("type") or "application/octet-stream"),
        filename=str(metadata.get("name") or "attachment"),
    )


@router.delete("/client/attachment/{attachment_id}")
async def delete_client_attachment(
    attachment_id: str,
    ozon_env: Annotated[OzonEnv, Depends(get_ozon_env)],
) -> dict[str, Any]:
    settings = get_env_settings()
    app_code = str(getattr(ozon_env.user_session, "app_code", "") or "")
    return delete_attachment(
        settings=settings,
        app_code=app_code,
        attachment_id=attachment_id,
    )


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
        "remote select payload key=%s curr_model=%s",
        payloadr.key,
        payloadr.curr_model,
    )
    data: list[Any] = []

    # SOLO risoluzione server-side. `key`+`curr_model` identificano il
    # component, da cui `app.services.formio` legge url/headers della
    # select remota (_load_select_field_config -> _load_remote_url_source).
    #
    # Il ramo che prendeva `data.url` dal body e' stato rimosso: era una
    # SSRF (URL arbitrario fetchato dal server, risposta restituita al
    # chiamante) e, peggio, `data.headerValueKey` finiva in
    # `get_global_param()` — che non applica ACL — permettendo di far
    # spedire il valore di QUALUNQUE record `global_params` come header
    # HTTP verso un host scelto dal client. I client devono usare
    # `key`+`curr_model`; la config remota vive sul component, non nel
    # payload.
    if payloadr.key and payloadr.curr_model:
        data = await service.get_select_options(
            payloadr.key,
            payloadr.curr_model,
        )
    elif payloadr.data.url:
        logger.warning(
            "remote select refused client-supplied url (use key+curr_model)"
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Client-supplied 'data.url' is not accepted. Use "
                "'key' + 'curr_model': the remote endpoint is resolved "
                "server-side from the component definition."
            ),
        )
    else:
        logger.warning("remote select payload missing key/curr_model")

    logger.info("remote select response count=%s", len(data))
    return make_response_object(data=data, mode="list")
