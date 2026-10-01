import pytest

from calendar_scheduler.config import SchedulerConfig

M2M_VARS = (
    "SCHEDULER_OAUTH_TOKEN_URL",
    "SCHEDULER_OAUTH_CLIENT_ID",
    "SCHEDULER_OAUTH_CLIENT_SECRET",
    "SCHEDULER_OAUTH_AUDIENCE",
    "OZON_M2M_CLIENT_ID",
    "OZON_M2M_CLIENT_SECRET",
    "OZON_TOKEN_AUDIENCE",
    "TOKEN_AUDIENCE",
    "KEYCLOAK_SERVER_URL",
    "KEYCLOAK_REALM",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in M2M_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SCHEDULER_RUN_BASE_URL", "http://app:8000")


def _generic(monkeypatch):
    monkeypatch.setenv("KEYCLOAK_SERVER_URL", "https://kc.example/")
    monkeypatch.setenv("KEYCLOAK_REALM", "inrim")
    monkeypatch.setenv("OZON_M2M_CLIENT_ID", "SIR-M2M")
    monkeypatch.setenv("OZON_M2M_CLIENT_SECRET", "s3cret")
    monkeypatch.setenv("OZON_TOKEN_AUDIENCE", "sir-aud")


def test_generic_m2m_client(monkeypatch):
    _generic(monkeypatch)

    cfg = SchedulerConfig.from_env()
    cfg.validate()

    assert cfg.oauth_token_url == (
        "https://kc.example/realms/inrim/protocol/openid-connect/token"
    )
    assert cfg.oauth_client_id == "SIR-M2M"
    assert cfg.oauth_client_secret == "s3cret"
    assert cfg.oauth_audience == "sir-aud"


def test_scheduler_specific_vars_override(monkeypatch):
    _generic(monkeypatch)
    monkeypatch.setenv("SCHEDULER_OAUTH_TOKEN_URL", "https://other/token")
    monkeypatch.setenv("SCHEDULER_OAUTH_CLIENT_ID", "SCHEDULER")
    monkeypatch.setenv("SCHEDULER_OAUTH_CLIENT_SECRET", "dedicated")
    monkeypatch.setenv("SCHEDULER_OAUTH_AUDIENCE", "custom-aud")

    cfg = SchedulerConfig.from_env()

    assert cfg.oauth_token_url == "https://other/token"
    assert cfg.oauth_client_id == "SCHEDULER"
    assert cfg.oauth_client_secret == "dedicated"
    assert cfg.oauth_audience == "custom-aud"


def test_empty_override_falls_back_to_generic(monkeypatch):
    _generic(monkeypatch)
    monkeypatch.setenv("SCHEDULER_OAUTH_CLIENT_ID", "")
    monkeypatch.setenv("SCHEDULER_OAUTH_TOKEN_URL", "")

    cfg = SchedulerConfig.from_env()

    assert cfg.oauth_client_id == "SIR-M2M"
    assert cfg.oauth_token_url.startswith("https://kc.example/realms/inrim/")


def test_audience_accepts_backend_alias(monkeypatch):
    # app_settings accetta TOKEN_AUDIENCE come alias di OZON_TOKEN_AUDIENCE:
    # uno stack che usa solo l'alias non deve far chiedere un token senza aud.
    _generic(monkeypatch)
    monkeypatch.delenv("OZON_TOKEN_AUDIENCE")
    monkeypatch.setenv("TOKEN_AUDIENCE", "alias-aud")

    assert SchedulerConfig.from_env().oauth_audience == "alias-aud"


def test_missing_m2m_config_names_both_options():
    cfg = SchedulerConfig.from_env()

    with pytest.raises(ValueError, match="OZON_M2M_CLIENT_ID"):
        cfg.validate()
