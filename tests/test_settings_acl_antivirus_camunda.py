import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from ozon_env_api.settings import OzonEnvApiSettings
from ozonenv.core.BaseModels import Settings as OzonSettings
from pydantic import ValidationError

from app.app_settings import AppSettings
from app.app_settings import build_public_db_settings_payload
from app.app_settings import build_api_settings
from app.app_settings import EnvSettings
from app.app_settings import get_env_settings
from app.app_settings import merge_public_db_settings
from app.core.models import AttachmentScanStatus
from app.ozon_env_acl import CompiledFieldAcl
from app.services.antivirus import AntivirusUnavailableError
from app.services.antivirus import scan_upload_non_blocking
from app.services.service import Service


class _Status:
    fail = False
    msg = ""


class _Schema:
    @staticmethod
    def schema():
        return {"components": []}

    @staticmethod
    def filter_keys():
        return {}


class _RecordModel:
    def __init__(self, name, rows=None):
        self.data_model = name
        self.status = _Status()
        self.model = _Schema()
        self.table_columns = {}
        self.rows = list(rows or [])
        self.upsert_calls = []
        self.last_obfuscate_fields = []

    def get_domain(self, query):
        return query

    async def count(self, domain):
        return len(self.rows)

    async def find(
        self,
        domain,
        sort="",
        skip=0,
        limit=0,
        pipeline_items=None,
        obfuscate_fields=None,
        fields=None,
    ):
        self.last_obfuscate_fields = list(obfuscate_fields or [])
        return [row.copy() for row in self.rows]

    async def by_name(self, name):
        for row in self.rows:
            if row.get("rec_name") == name:
                return row.copy()
        return {}

    async def upsert(
        self,
        data=None,
        rec_name="",
        data_value=None,
        trnf_config=None,
        fields_parser=None,
    ):
        self.upsert_calls.append({"data": data.copy(), "rec_name": rec_name})
        return data.copy()


class _PolicyModel:
    def __init__(self, policies):
        self.policies = policies

    def get_domain(self, query):
        return query

    async def find(self, domain, sort="", limit=0):
        return [policy.copy() for policy in self.policies]


class _AuditCollection:
    def __init__(self):
        self.docs = []

    async def insert_one(self, doc):
        self.docs.append(doc)


class _Engine:
    def __init__(self, audit):
        self.audit = audit

    def get_collection(self, name):
        assert name == "field_acl_audit"
        return self.audit


class _Env:
    def __init__(self, models, session=None, audit=None):
        self.user_session = session or SimpleNamespace(
            app_code="demo",
            uid="u1",
            # admin: questi test esercitano il motore field_acl_policy
            # (model_key/field_path/effect), non model_groups_rule (non
            # registrato in questo env fake, fail-closed per i non-admin
            # lo bloccherebbe prima ancora del field ACL sotto test).
            is_admin=True,
            user={
                "uid": "u1",
                "user_role": "base",
                "groups": ["finance"],
                "sector": "north",
            },
        )
        self.orm = SimpleNamespace(
            app_settings=SimpleNamespace(
                module_name="demo",
                version="1.0.0",
                logo_img_url="",
            )
        )
        self._models = models
        self.db = SimpleNamespace(engine=_Engine(audit or _AuditCollection()))

    def get(self, model_name):
        return self._models[model_name]


def test_env_settings_extends_ozon_settings_and_exposes_urls():
    settings = EnvSettings(
        app_code="demo",
        external_base_url="https://api.example.org",
        keycloak_server_url_public="https://kc-public",
        keycloak_server_url_internal="https://kc-internal",
        CAMUNDA_TASKLIST_URL="https://camunda-tasklist",
    )

    assert isinstance(settings, OzonSettings)
    assert settings.redirect_uri == "https://api.example.org/auth/callback"
    assert settings.keycloak_token_endpoint == (
        "https://kc-internal/realms/backend/protocol/openid-connect/token"
    )
    assert settings.camunda_tasklist_url == "https://camunda-tasklist"
    assert settings.camunda_auth_enabled is True
    assert settings.runtime_admin_roles == {"admin", "manager"}


def test_env_settings_accepts_nob_camunda_aliases():
    settings = EnvSettings(
        app_code="demo",
        CAMUNDA_CLIENT_GRPCADDRESS="http://orchestration:26500",
        CAMUNDA_CLIENT_RESTADDRESS="http://orchestration:8080",
        CAMUNDA_CLIENT_AUTH_CLIENTID="connectors",
        CAMUNDA_CLIENT_AUTH_CLIENTSECRET="secret",
        CAMUNDA_CLIENT_AUTH_ISSUERURL="https://auth/realms/demo/token",
    )

    assert settings.camunda_zeebe_address == "orchestration:26500"
    assert settings.camunda_tasklist_url == "http://orchestration:8080"
    assert settings.camunda_client_id == "connectors"
    assert settings.camunda_client_secret == "secret"
    assert settings.camunda_oauth_token_url == "https://auth/realms/demo/token"


def test_env_settings_rejects_camunda_web_url_as_grpc_address():
    with pytest.raises(ValidationError) as exc_info:
        EnvSettings(
            app_code="demo",
            CAMUNDA_ZEEBE_ADDRESS="http://orchestration:8081",
        )

    assert "must point to the Camunda gRPC gateway" in str(exc_info.value)


def test_app_settings_is_db_settings_model_and_parses_admins():
    settings = AppSettings(
        rec_name="settings_mci",
        app_code="mci",
        admins="admin.one, admin.two",
    )

    assert isinstance(settings, OzonSettings)
    assert settings.rec_name == "settings_mci"
    assert settings.app_code == "mci"
    assert settings.admins == ["admin.one", "admin.two"]


def test_env_settings_parses_seed_admins_for_bootstrap_only():
    settings = EnvSettings(app_code="demo", ADMINS="seed.one,seed.two")

    assert settings.admins == ["seed.one", "seed.two"]


def test_env_settings_rejects_fail_closed_stream_limit_smaller_than_upload_limit():
    with pytest.raises(ValidationError):
        EnvSettings(
            app_code="demo",
            max_upload_size_mb=25,
            clamav_max_stream_mb=10,
        )


def test_cookie_secure_defaults_true():
    settings = EnvSettings(app_code="demo")
    assert settings.cookie_secure is True


def test_token_audience_uses_canonical_ozon_alias():
    settings = EnvSettings(
        OZON_TOKEN_AUDIENCE="nob-app",
        TOKEN_AUDIENCE="legacy-value",
    )

    assert settings.token_audience == "nob-app"


def test_token_audience_accepts_legacy_alias():
    settings = EnvSettings(TOKEN_AUDIENCE="nob-app")

    assert settings.token_audience == "nob-app"


def test_session_secret_falls_back_to_stable_random_value_when_unset():
    """SESSION_SECRET non impostato non deve piu' produrre il vecchio
    default hardcoded, ma deve restare stabile tra istanze diverse dello
    stesso processo (get_env_settings() non fa caching, vedi commento su
    _FALLBACK_SESSION_SECRET in app_settings.py) — altrimenti un cookie
    firmato in una richiesta non verificherebbe piu' nella successiva."""
    first = EnvSettings(app_code="demo")
    second = EnvSettings(app_code="demo")

    assert first.session_secret != "dev-session-secret-change-me"
    assert first.session_secret == second.session_secret
    assert len(first.session_secret) >= 32


def test_session_secret_honors_explicit_env_value():
    settings = EnvSettings(app_code="demo", session_secret="explicit-secret")
    assert settings.session_secret == "explicit-secret"


def test_get_env_settings_loads_env_local(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("APP_CODE", raising=False)
    (tmp_path / ".env-local").write_text(
        "APP_CODE=from-file\n", encoding="utf-8"
    )

    settings = get_env_settings()

    assert settings.app_code == "from-file"


def test_build_api_settings_maps_env_settings_without_from_env(monkeypatch):
    def _unexpected_from_env(cls):
        raise AssertionError("from_env should not be used")

    monkeypatch.setattr(
        OzonEnvApiSettings,
        "from_env",
        classmethod(_unexpected_from_env),
    )
    settings = EnvSettings(
        app_code="demo",
        mongo_url="mongodb://db:27017",
        upload_folder="/tmp/uploads",
        tmp_upload_folder="/tmp/work",
    )

    api_settings = build_api_settings(settings)

    assert isinstance(api_settings, OzonEnvApiSettings)
    assert api_settings.app_code == "demo"
    assert api_settings.mongo_url == "mongodb://db:27017"
    assert api_settings.upload_folder == "/tmp/uploads"
    assert api_settings.tmp_upload_folder == "/tmp/work"


def test_public_db_settings_payload_excludes_sensitive_fields():
    settings = EnvSettings(
        app_code="demo",
        app_name="Demo App",
        app_version="2.4.6",
        module_label="Demo Label",
        description="Visible description",
        admins=["u-admin"],
        session_secret="session-secret",
        keycloak_client_secret="kc-secret",
        camunda_client_secret="camunda-secret",
        runtime_internal_token="runtime-secret",
        mongo_pass="mongo-secret",
    )

    payload = build_public_db_settings_payload(settings)

    assert payload["rec_name"] == "demo"
    assert payload["app_code"] == "demo"
    assert payload["module_label"] == "Demo Label"
    assert payload["description"] == "Visible description"
    assert payload["admins"] == ["u-admin"]
    assert payload["version"] == "2.4.6"
    assert "session_secret" not in payload
    assert "keycloak_client_secret" not in payload
    assert "camunda_client_secret" not in payload
    assert "runtime_internal_token" not in payload
    assert "mongo_pass" not in payload


def test_merge_public_db_settings_only_overrides_public_fields():
    settings = EnvSettings(
        app_code="demo",
        app_name="Demo App",
        module_label="Env Label",
        description="Env description",
        admins=["env-admin"],
        session_secret="env-session-secret",
        keycloak_client_secret="env-kc-secret",
    )

    merged = merge_public_db_settings(
        settings,
        {
            "module_label": "DB Label",
            "description": "DB description",
            "admins": ["db-admin"],
            "session_secret": "db-session-secret",
            "keycloak_client_secret": "db-kc-secret",
        },
    )

    assert merged.module_label == "DB Label"
    assert merged.description == "DB description"
    assert merged.admins == ["db-admin"]
    assert isinstance(merged, AppSettings)
    assert not hasattr(merged, "session_secret")
    assert not hasattr(merged, "keycloak_client_secret")


def test_field_acl_read_masks_and_compiles_on_session():
    customer = _RecordModel(
        "customer",
        rows=[
            {
                "rec_name": "c1",
                "name": "Ada",
                "secret": "classified",
                "ssn": "123",
            }
        ],
    )
    policies = _PolicyModel(
        [
            {
                "model_key": "customer",
                "field_path": "secret",
                "operation": "read",
                "effect": "obfuscate",
                "actor_selector": {"groups": ["finance"]},
                "active": True,
                "deleted": 0,
            },
            {
                "model_key": "customer",
                "field_path": "ssn",
                "operation": "read",
                "effect": "deny",
                "actor_selector": "*",
                "active": True,
                "deleted": 0,
            },
        ]
    )
    env = _Env({"customer": customer, "field_acl_policy": policies})
    service = Service(env)

    response = asyncio.run(service.load_record("customer", "c1"))

    assert response.content.data["secret"] is None
    assert "ssn" not in response.content.data
    assert response.content.obfucated_fields == ["secret"]
    assert isinstance(env.user_session.compiled_field_acl, CompiledFieldAcl)


def test_field_acl_denies_update_and_audits_attempt():
    audit = _AuditCollection()
    customer = _RecordModel(
        "customer", rows=[{"rec_name": "c1", "salary": 100}]
    )
    policies = _PolicyModel(
        [
            {
                "model_key": "customer",
                "field_path": "salary",
                "operation": "update",
                "effect": "deny",
                "actor_selector": {"uid": "u1"},
                "active": True,
                "deleted": 0,
            }
        ]
    )
    env = _Env(
        {"customer": customer, "field_acl_policy": policies}, audit=audit
    )
    service = Service(env)

    # Un campo negato in scrittura non blocca l'intero salvataggio (stessa
    # filosofia del read: oscura/ripristina, non 403 tutto il record) —
    # l'update procede, ma "salary" resta al valore STORED (100), mai
    # quello tentato (120).
    response = asyncio.run(service.upsert("customer", {"salary": 120}, rec_name="c1"))

    assert response.fail is False
    assert len(customer.upsert_calls) == 1
    assert customer.upsert_calls[0]["data"]["salary"] == 100
    assert audit.docs[0]["denied_fields"] == ["salary"]
    assert audit.docs[0]["operation"] == "update"


def test_field_acl_denies_update_on_nested_field_path():
    """`field_acl_policy` (a differenza di f_rule, mai limitato ai campi
    top-level) supporta `field_path` annidati (es. "address.zip"): il
    ripristino post-deny deve usare un traversal dotted-path-aware, non
    un `dict.pop`/assegnamento flat — un `data.pop("address.zip")` su un
    payload che ha `data["address"]["zip"]` e' un no-op silenzioso, il
    valore annidato dell'attaccante passerebbe intatto (bug reale
    individuato in review: il primo giro di `restore_or_drop_denied_
    write_fields` usava dict flat)."""
    audit = _AuditCollection()
    customer = _RecordModel(
        "customer",
        rows=[{"rec_name": "c1", "address": {"zip": "00100", "city": "Roma"}}],
    )
    policies = _PolicyModel(
        [
            {
                "model_key": "customer",
                "field_path": "address.zip",
                "operation": "update",
                "effect": "deny",
                "actor_selector": {"uid": "u1"},
                "active": True,
                "deleted": 0,
            }
        ]
    )
    env = _Env(
        {"customer": customer, "field_acl_policy": policies}, audit=audit
    )
    service = Service(env)

    response = asyncio.run(
        service.upsert(
            "customer",
            {"address": {"zip": "99999"}},
            rec_name="c1",
        )
    )

    assert response.fail is False
    assert customer.upsert_calls[0]["data"]["address"]["zip"] == "00100"
    assert audit.docs[0]["denied_fields"] == ["address.zip"]
    assert audit.docs[0]["operation"] == "update"


def test_list_records_rejects_disallowed_query_operator():
    customer = _RecordModel(
        "customer",
        rows=[{"rec_name": "c1", "name": "Ada", "secret": "classified"}],
    )
    env = _Env({"customer": customer})
    service = Service(env)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            service.list_records(
                model_name="customer",
                query={"$where": "this.secret == 'classified'"},
                order="",
                skip=0,
                limit=10,
            )
        )

    assert exc.value.status_code == 403
    assert exc.value.detail["operator"] == "$where"


def test_list_records_rejects_query_on_acl_denied_field():
    customer = _RecordModel(
        "customer",
        rows=[{"rec_name": "c1", "name": "Ada", "salary": 100}],
    )
    policies = _PolicyModel(
        [
            {
                "model_key": "customer",
                "field_path": "salary",
                "operation": "read",
                "effect": "deny",
                "actor_selector": "*",
                "active": True,
                "deleted": 0,
            }
        ]
    )
    env = _Env({"customer": customer, "field_acl_policy": policies})
    service = Service(env)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            service.list_records(
                model_name="customer",
                query={"salary": {"$gt": 50}},
                order="",
                skip=0,
                limit=10,
            )
        )

    assert exc.value.status_code == 403
    assert exc.value.detail["fields"] == ["salary"]


def test_list_records_rejects_sort_on_acl_denied_field():
    """order=salary:desc + limit=1 e' lo stesso oracle del filtro: rivela
    il ranking (quindi il valore) di un campo mascherato in output senza
    mai leggerlo dal payload di risposta."""
    customer = _RecordModel(
        "customer",
        rows=[{"rec_name": "c1", "name": "Ada", "salary": 100}],
    )
    policies = _PolicyModel(
        [
            {
                "model_key": "customer",
                "field_path": "salary",
                "operation": "read",
                "effect": "deny",
                "actor_selector": "*",
                "active": True,
                "deleted": 0,
            }
        ]
    )
    env = _Env({"customer": customer, "field_acl_policy": policies})
    service = Service(env)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            service.list_records(
                model_name="customer",
                query={},
                order="salary:desc",
                skip=0,
                limit=1,
            )
        )

    assert exc.value.status_code == 403
    assert exc.value.detail["fields"] == ["salary"]


def test_list_records_allows_ordinary_query_on_open_field():
    customer = _RecordModel(
        "customer",
        rows=[{"rec_name": "c1", "name": "Ada", "salary": 100}],
    )
    policies = _PolicyModel(
        [
            {
                "model_key": "customer",
                "field_path": "salary",
                "operation": "read",
                "effect": "deny",
                "actor_selector": "*",
                "active": True,
                "deleted": 0,
            }
        ]
    )
    env = _Env({"customer": customer, "field_acl_policy": policies})
    service = Service(env)

    response = asyncio.run(
        service.list_records(
            model_name="customer",
            query={"name": {"$eq": "Ada"}},
            order="",
            skip=0,
            limit=10,
        )
    )

    assert response.content.data[0]["name"] == "Ada"


def test_antivirus_unavailable_is_reported_without_blocking_upload(tmp_path):
    class OfflineScanner:
        async def scan_file(self, file_path):
            raise AntivirusUnavailableError("clamav offline")

    payload = tmp_path / "payload.bin"
    payload.write_bytes(b"payload")

    result = asyncio.run(
        scan_upload_non_blocking(OfflineScanner(), payload)
    )

    assert result.status == AttachmentScanStatus.ERROR
    assert result.engine == "clamav-unavailable"
