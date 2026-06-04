import logging
from typing import Annotated

from fastapi import APIRouter
from fastapi import Depends
from pydantic import BaseModel

from app.deps.app_env import get_authed_env
from app.deps.app_env import get_service
from app.services.common import ResponseObject
from app.services.message_queue import enqueue
from app.services.service import Service

router = APIRouter(
    prefix="/message_queue",
    tags=["MessageQueue"],
    dependencies=[Depends(get_authed_env)],
)
logger = logging.getLogger("uvicorn.error")


class EnqueueRequest(BaseModel):
    mail_template: str
    rel_rec_name: str


@router.post("/enqueue")
async def post_enqueue(
    payload: EnqueueRequest,
    service: Annotated[Service, Depends(get_service)],
) -> ResponseObject:
    logger.info(
        "message_queue enqueue request mail_template=%s rel_rec_name=%s",
        payload.mail_template,
        payload.rel_rec_name,
    )
    return await enqueue(service, payload.mail_template, payload.rel_rec_name)
