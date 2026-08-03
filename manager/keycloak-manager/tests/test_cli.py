from keycloak_manager.cli import audience_client_ids
from keycloak_manager.cli import raw_m2m_admin_kwargs


def test_audience_client_ids_includes_app_and_m2m_without_duplicates():
    assert audience_client_ids(
        "nob-app",
        "scheduler",
        "reporting, nob-app, reporting",
    ) == ["scheduler", "nob-app", "reporting"]


def test_raw_m2m_admin_kwargs_client_credentials():
    kw = raw_m2m_admin_kwargs(
        "https://kc",
        "demo",
        client_id="admin-rest-client",
        client_secret="sek",
        admin_realm="demo",
        verify=True,
    )
    assert kw["grant_type"] == "client_credentials"
    assert kw["client_id"] == "admin-rest-client"
    assert kw["client_secret_key"] == "sek"
    assert kw["server_url"] == "https://kc/"  # trailing slash per python-keycloak
    assert kw["realm_name"] == "demo"
    assert kw["user_realm_name"] == "demo"
    # niente username/password in modalita service account
    assert "username" not in kw
    assert "password" not in kw
