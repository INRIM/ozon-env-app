from __future__ import annotations

import logging
from typing import Annotated
from typing import Any

from fastapi import APIRouter
from fastapi import Body
from fastapi import Depends

from app.deps.app_env import get_authed_env
from app.deps.app_env import get_service
from app.services.common import ResponseObject
from app.services.service import Service
from app.services.step_task import complete_step_task

router = APIRouter(
    prefix="/step",
    tags=["Step Tasks"],
    dependencies=[Depends(get_authed_env)],
)
logger = logging.getLogger("uvicorn.error")


@router.post("/{model}/{name}")
async def post_step_task(
    model: str,
    name: str,
    service: Annotated[Service, Depends(get_service)],
    payload: Annotated[dict[str, Any], Body(default_factory=dict)],
) -> ResponseObject:
    logger.info("step task request model=%s name=%s", model, name)
    return await complete_step_task(service, model, name, payload)
