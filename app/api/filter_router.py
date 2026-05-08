from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter
from fastapi import Depends
from fastapi.responses import StreamingResponse

from app.deps.app_env import get_authed_env
from app.deps.app_env import get_service
from app.services.common import FastSearchRequest
from app.services.service import Service
from app.services.utils import _stream_ndjson_with_start_packet

router = APIRouter(
    prefix="/filter",
    tags=["Filters"],
    dependencies=[Depends(get_authed_env)],
)
logger = logging.getLogger("uvicorn.error")


@router.post("/fast_search/{action_name}")
async def post_fast_search(
        action_name: str,
        payload: FastSearchRequest,
        service: Annotated[Service, Depends(get_service)],
) -> StreamingResponse:
    logger.info(
        "fast_search request action=%s qfields=%s skip=%s limit=%s",
        action_name,
        len(payload.query_fields),
        payload.skip,
        payload.limit,
    )
    envelope = await service.fast_search_list(
        action_name=action_name,
        query_fields=payload.query_fields,
        skip=payload.skip,
        limit=payload.limit,
        order=payload.order,
    )
    cols = envelope.content.columns
    total_count = envelope.content.total_count
    data = await service.stream_record(
        envelope,
        order=payload.order,
        skip=payload.skip,
        limit=payload.limit,
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
