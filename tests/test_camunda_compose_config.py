from pathlib import Path


def test_compose_test_app_has_required_startup_env():
    compose = Path("docker-compose-test.yml").read_text(encoding="utf-8")

    assert "MONGO_URL: db:27017" in compose
    assert "BACKEND_INTERFACE: db" in compose
    assert "OZON_LOCALEDIR: /tmp/ozon-locale" in compose
    assert "OZON_APPLANG: it" in compose
    assert "SPRING_PROFILES_INCLUDE: rdbmsH2,insecure" in compose
    assert "SPRING_PROFILES_ACTIVE:" not in compose
    assert "http://localhost:8000/docs" in compose
    assert "uv run pytest tests/test_camunda_e2e.py -q" in compose
    assert "./pyproject.toml:/app/pyproject.toml:ro" in compose
    assert "./uv.lock:/app/uv.lock:ro" in compose
    assert "MONGO_HOST:" not in compose


def test_camunda_env_example_has_required_startup_env():
    env_example = Path("tests/camunda_e2e/env.example").read_text(
        encoding="utf-8"
    )

    assert "MONGO_URL=db:27017" in env_example
    assert "BACKEND_INTERFACE=db" in env_example
    assert "OZON_LOCALEDIR=/tmp/ozon-locale" in env_example
    assert "OZON_APPLANG=it" in env_example


def test_camunda_e2e_seed_uses_schema_compatible_variables_string():
    source = Path("tests/test_camunda_e2e.py").read_text(encoding="utf-8")

    assert '"variables": "{}"' in source
    assert '"variables": {}' not in source
    assert "/import/component" in source
    assert '"rec_name": "test_request"' in source
