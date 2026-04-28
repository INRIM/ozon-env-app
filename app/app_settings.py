import json
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from ozonenv.core.BaseModels import Settings
from pydantic import AliasChoices
from pydantic import ConfigDict
from pydantic import Field
from pydantic import model_validator

try:
    from pydantic_settings import SettingsConfigDict
except ImportError:  # pragma: no cover - optional when pydantic-settings is absent
    SettingsConfigDict = ConfigDict


def _iter_env_aliases(field_name: str, alias: Any) -> list[str]:
    aliases: list[str] = []
    if isinstance(alias, str):
        aliases.append(alias)
    elif isinstance(alias, AliasChoices):
        aliases.extend(str(item) for item in alias.choices)
    aliases.extend([field_name, field_name.upper()])
    deduplicated: list[str] = []
    for item in aliases:
        if item and item not in deduplicated:
            deduplicated.append(item)
    return deduplicated


def _load_settings_from_env() -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for field_name, field in EnvSettings.model_fields.items():
        for alias in _iter_env_aliases(field_name, field.validation_alias):
            if alias in os.environ:
                payload[field_name] = os.environ[alias]
                break
    return payload


class EnvSettings(Settings):
    model_config = SettingsConfigDict(
        env_file=".env-local",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_code: str = Field(
        default="",
        validation_alias=AliasChoices("APP_CODE", "OZON_APP_CODE"),
    )
    app_name: str = Field(
        default="ozon-env-api", validation_alias="OZON_APP_NAME"
    )
    app_version: str = Field(
        default="1.0.0", validation_alias="OZON_APP_VERSION"
    )

    asgi_host: str = Field(default="0.0.0.0", validation_alias="ASGI_HOST")
    asgi_port: int = Field(default=8000, validation_alias="ASGI_PORT")

    auth_mode: str = Field(default="token", validation_alias="AUTH_MODE")
    token_header: str = Field(
        default="Authorization", validation_alias="TOKEN_HEADER"
    )
    keycloak_remote_user_header: str = Field(
        default="x-remote-user",
        validation_alias="KEYCLOAK_REMOTE_USER_HEADER",
    )

    mongo_user: str = Field(default="", validation_alias="MONGO_USER")
    mongo_pass: str = Field(default="", validation_alias="MONGO_PASS")
    mongo_url: str = Field(default="", validation_alias="MONGO_URL")
    mongo_db: str = Field(default="", validation_alias="MONGO_DB")
    mongo_replica: str = Field(default="", validation_alias="MONGO_REPLICA")

    models_folder: str = Field(
        default="/models", validation_alias="MODELS_FOLDER"
    )

    external_base_url: str = Field(
        default="http://localhost:8080",
        validation_alias="EXTERNAL_BASE_URL",
    )

    session_secret: str = Field(
        default="dev-session-secret-change-me",
        validation_alias="SESSION_SECRET",
    )
    cookie_secure: bool = Field(
        default=False, validation_alias="COOKIE_SECURE"
    )

    post_login_redirect_url: str = Field(
        default="/",
        validation_alias="POST_LOGIN_REDIRECT_URL",
    )
    logout_redirect_url: str = Field(
        default="/",
        validation_alias="LOGOUT_REDIRECT_URL",
    )

    keycloak_realm: str = Field(
        default="backend", validation_alias="KEYCLOAK_REALM"
    )
    keycloak_client_id: str = Field(
        default="backend-web",
        validation_alias="KEYCLOAK_CLIENT_ID",
    )
    keycloak_client_secret: str = Field(
        default="dev-client-secret-change-me",
        validation_alias="KEYCLOAK_CLIENT_SECRET",
    )
    keycloak_server_url_public: str = Field(
        default="https://keycloak.example.internal",
        validation_alias="KEYCLOAK_SERVER_URL_PUBLIC",
    )
    keycloak_server_url_internal: str = Field(
        default="https://keycloak.example.internal",
        validation_alias="KEYCLOAK_SERVER_URL_INTERNAL",
    )

    camunda_web_url: str = Field(
        default="https://camunda.example.internal",
        validation_alias="CAMUNDA_WEB_URL",
    )
    camunda_api_url: str | None = Field(
        default=None, validation_alias="CAMUNDA_API_URL"
    )
    camunda_zeebe_address: str = Field(
        default="camunda.example.internal:443",
        validation_alias="CAMUNDA_ZEEBE_ADDRESS",
    )
    camunda_zeebe_secure: bool = Field(
        default=True,
        validation_alias="CAMUNDA_ZEEBE_SECURE",
    )

    camunda_tasklist_url_override: str | None = Field(
        default=None,
        validation_alias="CAMUNDA_TASKLIST_URL",
    )
    camunda_oauth_token_url_override: str | None = Field(
        default=None,
        validation_alias="CAMUNDA_OAUTH_TOKEN_URL",
    )

    camunda_client_id: str = Field(
        default="nob-service-client",
        validation_alias="CAMUNDA_CLIENT_ID",
    )
    camunda_client_secret: str = Field(
        default="change-me",
        validation_alias="CAMUNDA_CLIENT_SECRET",
    )
    camunda_tenant_id: str = Field(
        default="", validation_alias="CAMUNDA_TENANT_ID"
    )
    camunda_process_id: str = Field(
        default="", validation_alias="CAMUNDA_PROCESS_ID"
    )

    runtime_default_process_id: str = Field(
        default="",
        validation_alias="RUNTIME_DEFAULT_PROCESS_ID",
    )
    runtime_action_process_map_json: str = Field(
        default="{}",
        validation_alias="RUNTIME_ACTION_PROCESS_MAP_JSON",
    )
    runtime_function_process_map_json: str = Field(
        default="{}",
        validation_alias="RUNTIME_FUNCTION_PROCESS_MAP_JSON",
    )
    runtime_admin_roles_json: str = Field(
        default='["admin","manager"]',
        validation_alias="RUNTIME_ADMIN_ROLES_JSON",
    )
    runtime_default_app_key: str = Field(
        default="nullaostabandi",
        validation_alias="RUNTIME_DEFAULT_APP_KEY",
    )
    runtime_default_form_key: str = Field(
        default="request-form",
        validation_alias="RUNTIME_DEFAULT_FORM_KEY",
    )
    runtime_validate_payload_schema: bool = Field(
        default=True,
        validation_alias="RUNTIME_VALIDATE_PAYLOAD_SCHEMA",
    )
    runtime_require_published_schema: bool = Field(
        default=False,
        validation_alias="RUNTIME_REQUIRE_PUBLISHED_SCHEMA",
    )
    runtime_internal_token: str = Field(
        default="runtime-internal-token-change-me",
        validation_alias="RUNTIME_INTERNAL_TOKEN",
    )

    camunda_verify_tls: bool = Field(
        default=True,
        validation_alias="CAMUNDA_VERIFY_TLS",
    )
    camunda_enabled: bool = Field(
        default=True, validation_alias="CAMUNDA_ENABLED"
    )

    workflow_wait_timeout_seconds: float = Field(
        default=5.0,
        validation_alias="WORKFLOW_WAIT_TIMEOUT_SECONDS",
    )
    workflow_wait_poll_interval_seconds: float = Field(
        default=0.5,
        validation_alias="WORKFLOW_WAIT_POLL_INTERVAL_SECONDS",
    )
    sso_refresh_margin_seconds: int = Field(
        default=60,
        validation_alias="SSO_REFRESH_MARGIN_SECONDS",
    )

    upload_root: Path = Field(
        default=Path("/data/uploads"), validation_alias="UPLOAD_ROOT"
    )
    max_upload_size_mb: int = Field(
        default=25, validation_alias="MAX_UPLOAD_SIZE_MB"
    )

    clamav_host: str = Field(default="clamav", validation_alias="CLAMAV_HOST")
    clamav_port: int = Field(default=3310, validation_alias="CLAMAV_PORT")
    clamav_enabled: bool = Field(
        default=True, validation_alias="CLAMAV_ENABLED"
    )
    clamav_fail_closed: bool = Field(
        default=True, validation_alias="CLAMAV_FAIL_CLOSED"
    )
    clamav_timeout_seconds: float = Field(
        default=20.0,
        validation_alias="CLAMAV_TIMEOUT_SECONDS",
    )
    clamav_max_stream_mb: int = Field(
        default=25,
        validation_alias="CLAMAV_MAX_STREAM_MB",
    )

    @property
    def max_upload_size_bytes(self) -> int:
        return self.max_upload_size_mb * 1024 * 1024

    @property
    def clamav_max_stream_bytes(self) -> int:
        return self.clamav_max_stream_mb * 1024 * 1024

    @model_validator(mode="after")
    def validate_upload_scanning_limits(self) -> "EnvSettings":
        if (
            self.clamav_enabled
            and self.clamav_fail_closed
            and self.clamav_max_stream_mb < self.max_upload_size_mb
        ):
            raise ValueError(
                "CLAMAV_MAX_STREAM_MB must be >= MAX_UPLOAD_SIZE_MB when ClamAV fail-closed is enabled"
            )
        return self

    @property
    def redirect_uri(self) -> str:
        return f"{self.external_base_url}/auth/callback"

    @property
    def keycloak_authorization_endpoint(self) -> str:
        return (
            f"{self.keycloak_server_url_public}/realms/{self.keycloak_realm}"
            "/protocol/openid-connect/auth"
        )

    @property
    def keycloak_logout_endpoint(self) -> str:
        return (
            f"{self.keycloak_server_url_public}/realms/{self.keycloak_realm}"
            "/protocol/openid-connect/logout"
        )

    @property
    def keycloak_token_endpoint(self) -> str:
        return (
            f"{self.keycloak_server_url_internal}/realms/{self.keycloak_realm}"
            "/protocol/openid-connect/token"
        )

    @property
    def keycloak_userinfo_endpoint(self) -> str:
        return (
            f"{self.keycloak_server_url_internal}/realms/{self.keycloak_realm}"
            "/protocol/openid-connect/userinfo"
        )

    @property
    def camunda_api_base_url(self) -> str:
        return (self.camunda_api_url or self.camunda_web_url).rstrip("/")

    @property
    def camunda_tasklist_url(self) -> str:
        if self.camunda_tasklist_url_override:
            return self.camunda_tasklist_url_override.rstrip("/")
        return f"{self.camunda_api_base_url}/tasklist"

    @property
    def camunda_operate_url(self) -> str:
        return f"{self.camunda_web_url.rstrip('/')}/operate"

    @property
    def camunda_oauth_token_url(self) -> str:
        if self.camunda_oauth_token_url_override:
            return self.camunda_oauth_token_url_override
        return self.keycloak_token_endpoint

    @property
    def runtime_action_process_map(self) -> dict[str, str]:
        payload = json.loads(self.runtime_action_process_map_json or "{}")
        if not isinstance(payload, dict):
            return {}
        return {str(key): str(value) for key, value in payload.items() if value}

    @property
    def runtime_function_process_map(self) -> dict[str, str]:
        payload = json.loads(self.runtime_function_process_map_json or "{}")
        if not isinstance(payload, dict):
            return {}
        return {str(key): str(value) for key, value in payload.items() if value}

    @property
    def runtime_admin_roles(self) -> set[str]:
        payload = json.loads(self.runtime_admin_roles_json or "[]")
        if not isinstance(payload, list):
            return set()
        return {str(value) for value in payload if str(value)}

def get_env_settings() -> EnvSettings:
    load_dotenv(".env-local", override=False)
    return EnvSettings(**_load_settings_from_env())
