from __future__ import annotations

import logging
from typing import Annotated
from typing import Any

from fastapi import APIRouter
from fastapi import Body
from fastapi import Depends
from pydantic import BaseModel

from app.deps.app_env import get_authed_env
from app.deps.app_env import get_service
from app.services.service import Service

router = APIRouter(
    prefix="/services/registry",
    tags=["Services Registry"],
    dependencies=[Depends(get_authed_env)],
)
logger = logging.getLogger("uvicorn.error")


class RegisterRepoRequest(BaseModel):
    url: str
    version: str = "main"
    manifest_path: str = "manifest.json"
    active: bool = True


class RegisterManifestRequest(BaseModel):
    manifest: dict[str, Any]
    manifest_path: str = ""
    source_path: str = ""


@router.get("")
async def list_services(
    service: Annotated[Service, Depends(get_service)],
) -> list[dict[str, Any]]:
    logger.info("services registry list request")
    return await service.list_registered_services()


@router.post("/repositories")
async def register_repo(
    payload: Annotated[RegisterRepoRequest, Body(...)],
    service: Annotated[Service, Depends(get_service)],
) -> dict[str, Any]:
    logger.info("services registry repo register url=%s", payload.url)
    return await service.register_service_repo(
        url=payload.url,
        version=payload.version,
        manifest_path=payload.manifest_path,
        active=payload.active,
    )


@router.post("/manifests")
async def register_manifest(
    payload: Annotated[RegisterManifestRequest, Body(...)],
    service: Annotated[Service, Depends(get_service)],
) -> dict[str, Any]:
    code = str(payload.manifest.get("code", "") or "")
    logger.info("services registry manifest register code=%s", code)
    return await service.register_service_manifest(
        payload.manifest,
        manifest_path=payload.manifest_path,
        source_path=payload.source_path,
    )


@router.post("/{code}/up")
async def up_service(
    code: str,
    service: Annotated[Service, Depends(get_service)],
    build: bool = True,
) -> dict[str, Any]:
    logger.info("services registry up code=%s build=%s", code, build)
    return await service.up_registered_service(code, build=build)


@router.post("/{code}/down")
async def down_service(
    code: str,
    service: Annotated[Service, Depends(get_service)],
) -> dict[str, Any]:
    logger.info("services registry down code=%s", code)
    return await service.down_registered_service(code)
