from keycloak_manager import envfile


def test_token_url():
    assert envfile.token_url("https://kc/", "demo") == (
        "https://kc/realms/demo/protocol/openid-connect/token"
    )


def test_build_env_vars_no_prefix():
    v = envfile.build_env_vars(
        prefix="",
        server_url="https://kc",
        realm="demo",
        m2m_client_id="svc",
        m2m_secret="sek",
        app_audience="nob-app",
    )
    assert v["OAUTH_CLIENT_ID"] == "svc"
    assert v["OAUTH_CLIENT_SECRET"] == "sek"
    # invariante audience: stesso valore nei due punti
    assert v["OAUTH_AUDIENCE"] == "nob-app"
    assert v["TOKEN_AUDIENCE"] == "nob-app"
    assert "SCHEDULER_OAUTH_CLIENT_ID" not in v


def test_build_env_vars_with_prefix():
    v = envfile.build_env_vars(
        prefix="SCHEDULER",
        server_url="https://kc",
        realm="demo",
        m2m_client_id="svc",
        m2m_secret="sek",
        app_audience="nob-app",
    )
    assert v["SCHEDULER_OAUTH_CLIENT_ID"] == "svc"
    assert v["SCHEDULER_OAUTH_AUDIENCE"] == "nob-app"
    # TOKEN_AUDIENCE resta senza prefisso (var lato app)
    assert v["TOKEN_AUDIENCE"] == "nob-app"


def test_write_env_file_idempotent(tmp_path):
    f = tmp_path / "kc-env.var"
    v = envfile.build_env_vars(
        prefix="SCHEDULER",
        server_url="https://kc",
        realm="demo",
        m2m_client_id="svc",
        m2m_secret="sek",
        app_audience="nob-app",
    )
    envfile.write_env_file(str(f), v)
    envfile.write_env_file(str(f), {**v, "SCHEDULER_OAUTH_CLIENT_SECRET": "new"})
    text = f.read_text(encoding="utf-8")
    assert text.count("SCHEDULER_OAUTH_CLIENT_SECRET=") == 1
    assert "SCHEDULER_OAUTH_CLIENT_SECRET=new" in text
