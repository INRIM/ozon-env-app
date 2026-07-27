from __future__ import annotations

import logging
from typing import Annotated
from typing import Any

from fastapi import APIRouter
from fastapi import Body
from fastapi import Depends
from fastapi import HTTPException
from fastapi import status
from pydantic import BaseModel

from app.deps.app_env import get_service
from app.deps.app_env import require_admin_env
from app.services.service import Service

# Admin-only: questi endpoint scrivono la configurazione dei servizi e
# fanno partire `docker compose` con path presi dal record. Passano
# dall'ORM diretto (ServiceRegistryCore), NON da Service.upsert, quindi
# non sono coperti da `model_groups_rule`: il gate deve stare qui.
router = APIRouter(
    prefix="/services/registry",
    tags=["Services Registry"],
    dependencies=[Depends(require_admin_env)],
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
    try:
        return await service.up_registered_service(code, build=build)
    except ValueError as exc:
        # `_source_dir` rifiuta path assoluti o con '..' (finirebbero in
        # `cwd` di docker compose). Un record scritto prima di questo
        # vincolo deve dare un 400 leggibile, non un 500.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc


@router.post("/{code}/down")
async def down_service(
    code: str,
    service: Annotated[Service, Depends(get_service)],
) -> dict[str, Any]:
    logger.info("services registry down code=%s", code)
    try:
        return await service.down_registered_service(code)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
