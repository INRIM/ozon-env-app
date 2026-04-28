import json
import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import StreamingResponse

from app.deps.app_env import ClientSession, client_session
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
from app.services.utils import _stream_ndjson_with_start_packet

router = APIRouter(dependencies=[Depends(client_session)])
logger = logging.getLogger("uvicorn.error")


def _dump_model(instance: Any) -> dict[str, Any]:
    """Compat helper per pydantic v1/v2."""

    if hasattr(instance, "model_dump"):
        return instance.model_dump(by_alias=True)
    return instance.dict(by_alias=True)


def _safe_encode_payload(payload: Any) -> Any:
    """Serializza payload eterogenei evitando crash su serializer pydantic."""

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
    """Liveness probe endpoint."""

    logger.info("healthcheck request received")
    return {"status": "live"}


@router.get("/get_session")
async def get_session(
    cli_session: Annotated[ClientSession, Depends(client_session)],
) -> dict[str, Any]:
    """Restituisce la sessione corrente associata al token attivo."""

    logger.info("get session request received")
    session_data = _safe_encode_payload(cli_session.service.session)
    logger.info("get session response ready")
    return session_data


@router.get("/models/distinct")
async def get_models_distinct(
    cli_session: Annotated[ClientSession, Depends(client_session)],
) -> ResponseObject:
    """Restituisce la lista modelli disponibili (`component.rec_name`)."""

    logger.info("list models request received")
    data = await cli_session.service.get_models()
    logger.info("list models request completed count=%s", len(data))
    return make_response_object(data=data, mode="list")


@router.get("/record/{model}")
async def get_record_schema(
    model: str,
    cli_session: Annotated[ClientSession, Depends(client_session)],
) -> ResponseObject:
    """Restituisce lo schema form del modello richiesto."""

    logger.info("record schema request model=%s", model)
    resp_data = await cli_session.service.compo_by_name("component", model)
    logger.info("record schema response ready model=%s", model)
    return resp_data


@router.post("/list/{model}")
async def post_list_records(
    model: str,
    payload: ListRequest,
    cli_session: Annotated[ClientSession, Depends(client_session)],
) -> StreamingResponse:
    """
    Esegue una list paginata e risponde in NDJSON:
    1) envelope iniziale
    2) record successivi in stream
    """

    logger.info(
        "list request model=%s order=%s skip=%s limit=%s",
        model,
        payload.order,
        payload.skip,
        payload.limit,
    )
    envelope = await cli_session.service.list_records(
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

    # Nota: stream_record deve essere awaited per ottenere il cursor reale.
    data = await cli_session.service.stream_record(
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
    cli_session: Annotated[ClientSession, Depends(client_session)],
) -> ResponseObject:
    """Carica un record per `model` e `rec_name`."""

    logger.info("record load request model=%s rec_name=%s", model, rec_name)
    resp_data = await cli_session.service.load_record(model, rec_name)
    if not resp_data:
        logger.warning(
            "record not found model=%s rec_name=%s",
            model,
            rec_name,
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Record '{rec_name}' not found on model '{model}'",
        )
    logger.info(
        "record load response ready model=%s rec_name=%s",
        model,
        rec_name,
    )
    return resp_data


@router.post("/record/{model}/{rec_name}")
async def post_update_record(
    model: str,
    rec_name: str,
    payload: dict[str, Any],
    cli_session: Annotated[ClientSession, Depends(client_session)],
) -> ResponseObject:
    """Aggiorna/crea un record identificato da `rec_name`."""

    logger.info("record upsert request model=%s rec_name=%s", model, rec_name)
    resp_data = await cli_session.service.upsert(
        model, payload, rec_name=rec_name
    )
    if not resp_data:
        logger.warning(
            "record upsert failed model=%s rec_name=%s",
            model,
            rec_name,
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Record '{rec_name}' not found on model '{model}'",
        )
    logger.info(
        "record upsert completed model=%s rec_name=%s",
        model,
        rec_name,
    )
    return resp_data


@router.post("/models/distinct")
async def post_models_distinct(
    payloadr: RemoteSelectRequest,
    cli_session: Annotated[ClientSession, Depends(client_session)],
) -> ResponseObject:
    """Restituisce modelli o opzioni select in base al payload ricevuto."""

    logger.info(
        "models distinct payload key=%s curr_model=%s has_properties=%s",
        payloadr.key,
        payloadr.curr_model,
        payloadr.has_properties(),
    )
    if not payloadr.has_properties():
        data = await cli_session.service.get_models()
        logger.info(
            "models distinct fallback to model list count=%s",
            len(data),
        )
    else:
        if not payloadr.key or not payloadr.curr_model:
            logger.warning(
                "models distinct missing key/curr_model, using model list"
            )
            data = await cli_session.service.get_models()
            return make_response_object(data=data, mode="list")
        field_key = payloadr.key
        curr_model = payloadr.curr_model
        data = await cli_session.service.get_select_options(
            cli_session,
            field_key,
            curr_model,
        )
        logger.info(
            "models distinct select options generated count=%s",
            len(data),
        )
    return make_response_object(data=data, mode="list")


@router.post("/get_remote_data_select")
@router.post("/get_remote_select")
async def post_remote_data_select(
    payloadr: RemoteSelectRequest,
    cli_session: Annotated[ClientSession, Depends(client_session)],
) -> ResponseObject:
    """
    Restituisce opzioni per select:
    - da schema FormIO (`key` + `curr_model`)
    - oppure da URL remoto (`data.url`)
    """

    logger.info(
        "remote select payload key=%s curr_model=%s url=%s",
        payloadr.key,
        payloadr.curr_model,
        payloadr.data.url,
    )
    data: list[Any] = []

    # Flusso principale: select options lette dalla configurazione FormIO.
    if payloadr.key and payloadr.curr_model:
        data = await cli_session.service.get_select_options(
            cli_session,
            payloadr.key,
            payloadr.curr_model,
        )
    # Fallback compatibile con payload vecchi che passano direttamente URL.
    elif payloadr.data.url:
        header_data = build_remote_select_header(_dump_model(payloadr.data))
        data = await remote_data_select_response(
            cli_session=cli_session,
            url=header_data.url,
            path_value=header_data.path_value,
            header_key=header_data.header_key,
            header_value_key=header_data.header_value_key,
        )
    else:
        logger.warning("remote select payload missing key/curr_model and url")

    logger.info("remote select response count=%s", len(data))
    return make_response_object(data=data, mode="list")
