from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi

from app.api.action_router import router as action_router
from app.api.routes import router
from app.app_settings import get_env_settings
from app.middleware.logging import LoggingMiddleware
from app.services.session_auth import AUTH_MODE_KEYCLOAK
from app.services.session_auth import normalize_auth_mode


settings = get_env_settings()
auth_mode = normalize_auth_mode(settings.auth_mode)

app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description=(
        (
            "Authentication uses a single token via "
            "`Authorization: Bearer <token>`.\n\n"
            "`app_code` is fixed server-side from `APP_CODE`/`OZON_APP_CODE` "
            "and is not accepted from client input."
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
)

app.add_middleware(LoggingMiddleware)
app.include_router(router)
app.include_router(action_router)


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
                    "token": {
                        "type": "string",
                        "description": (
                            "Internal session token. In token mode it matches "
                            "the user bearer token; in Keycloak mode it is "
                            "generated server-side after the trusted header is "
                            "validated."
                        ),
                    },
                    "app_code": {
                        "type": "string",
                        "description": (
                            "Fixed server app code from APP_CODE/OZON_APP_CODE "
                            f"(current: `{configured_app_code}`)."
                        ),
                    },
                    "sso_token": {
                        "type": "string",
                        "description": (
                            "Current Keycloak access token cached in session "
                            "(available in Keycloak mode)."
                        ),
                    },
                    "sso_refresh": {
                        "type": "string",
                        "description": (
                            "Current Keycloak refresh token cached in session "
                            "(available in Keycloak mode)."
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
                "required": ["token", "app_code"],
                "additionalProperties": True,
            }
        )

    app.openapi_schema = schema
    return app.openapi_schema


app.openapi = custom_openapi
