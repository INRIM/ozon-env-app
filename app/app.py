from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi
from ozon_env_api.core.serviceapi import OzonEnvApiService

from app.api.action_router import router as action_router
from app.api.auth_routes import router as auth_router
from app.api.camunda_router import router as camunda_router
from app.api.filter_router import router as filter_router
from app.api.message_queue_router import router as message_queue_router
from app.api.routes import router
from app.api.service_registry_router import router as service_registry_router
from app.api.step_router import router as step_router
from app.api.websocket_router import router as websocket_router
from app.app_settings import build_api_settings
from app.app_settings import get_env_settings
from app.middleware.logging import LoggingMiddleware
from app.plugins import discover_plugins
from app.services.plugin_installer import PluginInstaller
from app.services.session_auth import AUTH_MODE_KEYCLOAK
from app.services.session_auth import normalize_auth_mode

settings = get_env_settings()
auth_mode = normalize_auth_mode(settings.auth_mode)
local_settings = build_api_settings(settings)


class _AppService(OzonEnvApiService):
    async def startup(self) -> None:
        from app.deps.app_env import _build_ozon_cfg
        from app.deps.app_env import sync_app_settings_startup

        await sync_app_settings_startup()
        installer = PluginInstaller(
            cfg=_build_ozon_cfg(), app_code=settings.app_code
        )
        await installer.run(
            discover_plugins(
                plugins_dir=settings.plugins_folder,
                app_code=settings.app_code,
            )
        )


local_service = _AppService(settings=local_settings)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await local_service.startup()
    try:
        yield
    finally:
        await local_service.shutdown()


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description=(
        (
            "Authentication uses a single token via "
            "`Authorization: Bearer <token>`.\n\n"
            "`app_code` can be selected per request via the `app_code` query "
            "parameter, otherwise the backend uses the `app_code` cookie and "
            "finally falls back to `APP_CODE`/`OZON_APP_CODE`."
        )
        if auth_mode != AUTH_MODE_KEYCLOAK
        else (
            "Authentication is delegated to a trusted reverse proxy "
            "(Keycloak/OpenID Connect), which injects "
            f"`{settings.keycloak_remote_user_header}`.\n\n"
            "The backend trusts that header only inside the deployment "
            "boundary and still creates its own internal session."
        )
    ),
    lifespan=lifespan,
)


app.add_middleware(LoggingMiddleware)
app.include_router(auth_router)
app.include_router(router)
app.include_router(action_router)
app.include_router(filter_router)
app.include_router(message_queue_router)
app.include_router(service_registry_router)
app.include_router(camunda_router)
app.include_router(step_router)
app.include_router(websocket_router)


def custom_openapi() -> dict:
    if app.openapi_schema:
        return app.openapi_schema

    schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
    )

    security_schemes = schema.get("components", {}).get("securitySchemes", {})
    api_key_scheme = security_schemes.get("APIKeyHeader")
    if isinstance(api_key_scheme, dict):
        if auth_mode == AUTH_MODE_KEYCLOAK:
            api_key_scheme["description"] = (
                "Keycloak mode: client authentication is performed upstream by "
                f"a trusted reverse proxy that injects "
                f"`{settings.keycloak_remote_user_header}`. "
                "The backend-generated session token is internal and is not "
                "the ingress credential."
            )
        else:
            api_key_scheme["description"] = (
                "Single-token mode: use the user token in `Authorization` "
                "header. No separate admin token is required per request."
            )

    get_session_schema = (
        schema.get("paths", {})
        .get("/get_session", {})
        .get("get", {})
        .get("responses", {})
        .get("200", {})
        .get("content", {})
        .get("application/json", {})
        .get("schema", {})
    )
    if isinstance(get_session_schema, dict):
        configured_app_code = settings.app_code or "<APP_CODE>"
        get_session_schema.update(
            {
                "title": "ClientSessionPayload",
                "type": "object",
                "properties": {
                    "uid": {
                        "type": "string",
                        "description": "Authenticated user identifier.",
                    },
                    "username": {
                        "type": "string",
                        "description": (
                            "Authenticated username; falls back to `uid`."
                        ),
                    },
                    "authenticated": {
                        "type": "boolean",
                        "description": (
                            "True when the response contains an authenticated "
                            "user identity."
                        ),
                    },
                    "app_code": {
                        "type": "string",
                        "description": (
                            "Resolved app code for the current request. "
                            "Precedence: `?app_code=` query parameter, "
                            "`app_code` cookie, then APP_CODE/OZON_APP_CODE "
                            f"(default: `{configured_app_code}`)."
                        ),
                    },
                    "sso_expire": {
                        "type": "string",
                        "format": "date-time",
                        "description": (
                            "Expiry timestamp of `sso_token` used for proactive "
                            "refresh before expiration."
                        ),
                    },
                },
                "required": [
                    "uid",
                    "username",
                    "authenticated",
                    "app_code",
                ],
                "additionalProperties": True,
            }
        )

    app.openapi_schema = schema
    return app.openapi_schema


app.openapi = custom_openapi
