import json
import os
import secrets
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from dotenv import load_dotenv
from ozon_env_api.settings import OzonEnvApiSettings
from ozonenv.core.BaseModels import OzonEnvCoreSettings
from ozonenv.core.BaseModels import Settings as OzonSettings
from ozonenv.core.BaseModels import _expand_yaml_data
from pydantic import AliasChoices
from pydantic import Field
from pydantic import field_validator
from pydantic import model_validator

# Fallback quando SESSION_SECRET non e' impostato — generato una volta a
# import del modulo, NON per-istanza: `get_env_settings()` costruisce un
# nuovo `EnvSettings` ad ogni chiamata (nessuna cache), quindi un
# `default_factory` valutato per-istanza produrrebbe un secret diverso ad
# ogni richiesta e romperebbe la verifica delle firme itsdangerous tra una
# richiesta e l'altra. Stabile per la vita del processo, sconosciuto
# dall'esterno (a differenza del vecchio default hardcoded) — vedi
# docs/SECURITY_KEYCLOAK_TOKEN_ANALYSIS.it.md finding #5.
_FALLBACK_SESSION_SECRET = secrets.token_urlsafe(32)


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


def _load_yaml_config() -> dict[str, Any]:
    yaml_path = Path(".ozonenv") / "config.yaml"
    if not yaml_path.exists():
        return {}
    try:
        import yaml

        raw = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
        return _expand_yaml_data(raw)
    except Exception:
        return {}


_APP_SETTINGS_FIELDS = frozenset(
    {
        "list_order",
        "rec_name",
        "internal_port",
        "app_origin_type",
        "module_label",
        "description",
        "admins",
        "module_type",
        "module_group",
        "version",
        "port",
        "stato",
        "upload_folder",
        "web_concurrency",
        "delete_record_after_days",
        "delete_retention_hours",
        "token_expire_hours",
        "session_expire_hours",
        "theme",
        "logo_img_url",
        "server_datetime_mask",
        "server_date_mask",
        "ui_datetime_mask",
        "ui_date_mask",
        "tz",
        "report_orientation",
        "report_page_size",
        "report_footer_company",
        "report_footer_title1",
        "report_footer_sub_title",
        "report_footer_pagination",
        "report_header_space",
        "report_footer_space",
        "report_margin_left",
        "report_margin_right",
        "domain",
        "external_proxy_uri_configs",
    }
)


def _normalize_public_setting_value(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {
            str(key): _normalize_public_setting_value(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_normalize_public_setting_value(item) for item in value]
    if isinstance(value, set):
        return [
            _normalize_public_setting_value(item)
            for item in sorted(value, key=lambda item: str(item))
        ]
    return value


def _parse_admins_value(value: Any) -> list[str]:
    if isinstance(value, str):
        value = value.strip()
        if value.startswith("["):
            parsed = json.loads(value)
            return [str(item).strip() for item in parsed if str(item).strip()]
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _normalize_camunda_grpc_address(value: Any) -> str:
    address = str(value or "").strip()
    if not address:
        return address

    parsed = urlparse(address)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        address = parsed.netloc
        parsed_port = parsed.port
        if parsed_port in {8080, 8081}:
            raise ValueError(
                "CAMUNDA_ZEEBE_ADDRESS must point to the Camunda gRPC gateway, "
                "not the REST/web URL. Use host:26500 or "
                "CAMUNDA_CLIENT_GRPCADDRESS."
            )
    elif "://" in address:
        raise ValueError(
            "CAMUNDA_ZEEBE_ADDRESS must be a gRPC target like host:26500."
        )

    if address.endswith("/"):
        address = address.rstrip("/")
    return address


class AppSettings(OzonSettings):
    """Runtime settings record stored in the DB `settings` model."""

    app_code: str = ""
    admins: list[str] = Field(
        default_factory=list,
        validation_alias=AliasChoices("ADMINS", "APP_ADMINS"),
    )
    session_expire_hours: int = Field(default=12)
    # ore prima della cancellazione definitiva di un record soft-deleted
    # (usato da action_runtime per il timestamp di delete differito).
    delete_retention_hours: int = Field(default=24)
    server_datetime_mask: str = Field(default="%Y-%m-%dT%H:%M:%S")
    server_date_mask: str = Field(default="%Y-%m-%dT%H:%M:%S")
    ui_datetime_mask: str = Field(default="%d/%m/%Y %H:%M:%S")
    ui_date_mask: str = Field(default="%d/%m/%Y")
    domain: str = Field(default="")
    external_proxy_uri_configs: list[dict[str, Any]] = Field(
        default_factory=list
    )

    @field_validator("admins", mode="before")
    @classmethod
    def _parse_admins(cls, v: Any) -> list[str]:
        return _parse_admins_value(v)


class EnvSettings(OzonEnvCoreSettings):
    # Override inherited fields with app-specific aliases / defaults.
    # AppSettings is the DB-backed runtime settings object.
    app_code: str = Field(
        default="",
        validation_alias=AliasChoices("APP_CODE", "OZON_APP_CODE"),
    )
    token_audience: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "OZON_TOKEN_AUDIENCE",
            "TOKEN_AUDIENCE",
        ),
    )
    # Local auth uses "token" as default; ozon-env core defaults to "session"
    auth_mode: str = Field(default="token", validation_alias="AUTH_MODE")

    admins: list[str] = Field(
        default_factory=list,
        validation_alias=AliasChoices("ADMINS", "APP_ADMINS"),
    )

    @field_validator("admins", mode="before")
    @classmethod
    def _parse_seed_admins(cls, v: Any) -> list[str]:
        return _parse_admins_value(v)

    delete_retention_hours: int = Field(
        default=24, validation_alias="DELETE_RETENTION_HOURS"
    )

    app_name: str = Field(
        default="ozon-env-api", validation_alias="OZON_APP_NAME"
    )
    app_version: str = Field(
        default="1.0.0", validation_alias="OZON_APP_VERSION"
    )

    asgi_host: str = Field(default="0.0.0.0", validation_alias="ASGI_HOST")
    asgi_port: int = Field(default=8000, validation_alias="ASGI_PORT")
    # debug abilita i logger.debug() sparsi nel motore ACL/record_rules
    # (app.ozon_env_acl, Service._get_record_rules/list_records/ecc.).
    log_level: str = Field(default="info", validation_alias="LOG_LEVEL")

    @field_validator("log_level", mode="before")
    @classmethod
    def _normalize_log_level(cls, v: Any) -> str:
        normalized = str(v or "info").strip().lower()
        valid = {"critical", "error", "warning", "info", "debug", "trace"}
        return normalized if normalized in valid else "info"

    token_header: str = Field(
        default="Authorization", validation_alias="TOKEN_HEADER"
    )
    keycloak_remote_user_header: str = Field(
        default="x-remote-user",
        validation_alias="KEYCLOAK_REMOTE_USER_HEADER",
    )

    external_base_url: str = Field(
        default="http://localhost:8080",
        validation_alias="EXTERNAL_BASE_URL",
    )

    # Se SESSION_SECRET non e' impostato, i cookie firmati restano
    # comunque non forgeable da chi conosce solo il codice sorgente (vedi
    # _FALLBACK_SESSION_SECRET sopra), al prezzo di invalidare le sessioni
    # esistenti ad ogni riavvio del processo — E di rompere la verifica tra
    # worker/repliche diversi (ognuno genera il proprio fallback), quindi
    # e' un net positivo solo per run singolo-processo/dev. Va impostato
    # esplicitamente per qualunque deploy con web_concurrency > 1 o piu'
    # repliche (gia' richiesto da ansible-deploy).
    session_secret: str = Field(
        default_factory=lambda: _FALLBACK_SESSION_SECRET,
        validation_alias="SESSION_SECRET",
    )
    cookie_secure: bool = Field(
        default=True, validation_alias="COOKIE_SECURE"
    )
    auth_cookie_name: str = Field(
        default="ozon_session", validation_alias="AUTH_COOKIE_NAME"
    )
    auth_cookie_max_age: int = Field(
        default=86400, validation_alias="AUTH_COOKIE_MAX_AGE"
    )
    auth_cookie_samesite: str = Field(
        default="lax", validation_alias="AUTH_COOKIE_SAMESITE"
    )
    csrf_cookie_name: str = Field(
        default="ozon_csrf", validation_alias="CSRF_COOKIE_NAME"
    )
    # Origin allowlist per l'handshake WebSocket (CSWSH). CSV; vuota = nessun
    # controllo (coerente con assenza di CORS app-wide + cookie SameSite=Lax).
    ws_allowed_origins: str = Field(
        default="", validation_alias="WS_ALLOWED_ORIGINS"
    )
    auth_state_cookie_name: str = Field(
        default="ozon_state", validation_alias="AUTH_STATE_COOKIE_NAME"
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
        validation_alias=AliasChoices(
            "CAMUNDA_ZEEBE_ADDRESS",
            "CAMUNDA_CLIENT_GRPCADDRESS",
            "ORCHESTRATION_GRPC_COMPOSE_URL",
        ),
    )
    camunda_zeebe_secure: bool = Field(
        default=True,
        validation_alias="CAMUNDA_ZEEBE_SECURE",
    )

    camunda_tasklist_url_override: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "CAMUNDA_TASKLIST_URL",
            "CAMUNDA_CLIENT_RESTADDRESS",
            "ORCHESTRATION_COMPOSE_URL",
        ),
    )
    camunda_oauth_token_url_override: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "CAMUNDA_OAUTH_TOKEN_URL",
            "CAMUNDA_CLIENT_AUTH_ISSUERURL",
        ),
    )

    camunda_client_id: str = Field(
        default="nob-service-client",
        validation_alias=AliasChoices(
            "CAMUNDA_CLIENT_ID",
            "CAMUNDA_CLIENT_AUTH_CLIENTID",
        ),
    )
    camunda_client_secret: str = Field(
        default="change-me",
        validation_alias=AliasChoices(
            "CAMUNDA_CLIENT_SECRET",
            "CAMUNDA_CLIENT_AUTH_CLIENTSECRET",
        ),
    )
    camunda_auth_enabled: bool = Field(
        default=True,
        validation_alias="CAMUNDA_AUTH_ENABLED",
    )
    camunda_tenant_id: str = Field(
        default="", validation_alias="CAMUNDA_TENANT_ID"
    )
    camunda_process_id: str = Field(
        default="", validation_alias="CAMUNDA_PROCESS_ID"
    )
    # Dopo il complete di uno user task, attende che il flow avanzi oltre gli
    # eventuali service/external task (fino al prossimo user task o alla fine
    # del processo) prima di rispondere. 0 = nessuna attesa.
    camunda_complete_wait_seconds: float = Field(
        default=30.0, validation_alias="CAMUNDA_COMPLETE_WAIT_SECONDS"
    )
    camunda_poll_interval_seconds: float = Field(
        default=0.5, validation_alias="CAMUNDA_POLL_INTERVAL_SECONDS"
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

    @field_validator("camunda_zeebe_address", mode="before")
    @classmethod
    def _normalize_camunda_zeebe_address(cls, v: Any) -> str:
        return _normalize_camunda_grpc_address(v)

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
    model_file_dump_mode: str = Field(
        default="",
        validation_alias=AliasChoices(
            "MODEL_FILE_DUMP_MODE",
            "",
        ),
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
    clamav_tmp_dir: Path = Field(
        default=Path("/tmp"), validation_alias="CLAMAV_TMP_DIR"
    )

    plugins_folder: Path = Field(
        default=Path("/plugins"), validation_alias="PLUGINS_FOLDER"
    )

    @property
    def max_upload_size_bytes(self) -> int:
        return self.max_upload_size_mb * 1024 * 1024

    @property
    def clamav_max_stream_bytes(self) -> int:
        return self.clamav_max_stream_mb * 1024 * 1024

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
    def logout_redirect_absolute_url(self) -> str:
        """`LOGOUT_REDIRECT_URL` reso assoluto.

        Keycloak valida `post_logout_redirect_uri` contro la lista del
        client e rifiuta con 400 qualunque valore relativo: col default
        "/" il redirect di logout finiva su una pagina di errore di
        Keycloak invece che sull'app.
        """
        target = str(self.logout_redirect_url or "/").strip()
        if target.startswith(("http://", "https://")):
            return target
        return f"{self.external_base_url.rstrip('/')}/{target.lstrip('/')}"

    @property
    def keycloak_logout_endpoint_internal(self) -> str:
        """End-session endpoint sulla rete interna.

        `keycloak_logout_endpoint` (public) e' l'URL su cui si manda il
        BROWSER; questo e' quello che chiama il server per revocare il
        refresh token, e deve passare per l'hostname interno come tutte
        le altre chiamate server->Keycloak (token/userinfo/jwks).
        """
        return (
            f"{self.keycloak_server_url_internal}/realms/{self.keycloak_realm}"
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
    def keycloak_jwks_url(self) -> str:
        return (
            f"{self.keycloak_server_url_internal}/realms/{self.keycloak_realm}"
            "/protocol/openid-connect/certs"
        )

    @property
    def keycloak_issuer(self) -> str:
        return (
            f"{self.keycloak_server_url_public}/realms/{self.keycloak_realm}"
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
        return {
            str(key): str(value) for key, value in payload.items() if value
        }

    @property
    def runtime_function_process_map(self) -> dict[str, str]:
        payload = json.loads(self.runtime_function_process_map_json or "{}")
        if not isinstance(payload, dict):
            return {}
        return {
            str(key): str(value) for key, value in payload.items() if value
        }

    @property
    def runtime_admin_roles(self) -> set[str]:
        payload = json.loads(self.runtime_admin_roles_json or "[]")
        if not isinstance(payload, list):
            return set()
        return {str(value) for value in payload if str(value)}

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


def build_public_db_settings_payload(settings: Any) -> dict[str, Any]:
    app_code = str(getattr(settings, "app_code", "") or "").strip()
    if isinstance(settings, AppSettings):
        rec_name = str(getattr(settings, "rec_name", "") or app_code).strip()
    else:
        rec_name = app_code
    defaults = AppSettings(rec_name=rec_name, app_code=app_code)
    payload: dict[str, Any] = {
        "rec_name": rec_name,
        "app_code": app_code,
        "active": True,
        "deleted": 0,
    }
    for field_name in _APP_SETTINGS_FIELDS:
        if field_name == "rec_name":
            continue
        if hasattr(settings, field_name):
            value = getattr(settings, field_name)
        else:
            value = getattr(defaults, field_name)
        if value is None:
            continue
        payload[field_name] = _normalize_public_setting_value(value)

    if not payload.get("module_label"):
        payload["module_label"] = str(
            getattr(settings, "module_name", "")
            or getattr(settings, "app_name", "")
            or settings.app_code
        ).strip()
    if not payload.get("description"):
        payload["description"] = str(
            getattr(settings, "description", "")
            or getattr(settings, "app_name", "")
            or settings.app_code
        ).strip()
    payload["version"] = str(
        getattr(settings, "app_version", "")
        or payload.get("version", "")
        or getattr(settings, "version", "")
        or ""
    ).strip()
    return payload


def merge_public_db_settings(
    settings: EnvSettings,
    db_payload: Mapping[str, Any] | None,
) -> AppSettings:
    merged = build_public_db_settings_payload(settings)
    if not isinstance(db_payload, Mapping):
        return AppSettings(**merged)
    for field_name in _APP_SETTINGS_FIELDS:
        if field_name in db_payload and field_name in AppSettings.model_fields:
            merged[field_name] = db_payload[field_name]
    return AppSettings(**merged)


def build_api_settings(settings: EnvSettings) -> OzonEnvApiSettings:
    # Older ozon-env-api builds still use dataclasses.asdict() in from_env().
    field_names = set(OzonEnvApiSettings.model_fields)
    return OzonEnvApiSettings(**settings.model_dump(include=field_names))


def get_env_settings() -> EnvSettings:
    load_dotenv(".env-local", override=False)
    load_dotenv(".env", override=False)
    yaml_data = _load_yaml_config()
    env_data = _load_settings_from_env()
    return EnvSettings(**{**yaml_data, **env_data})
