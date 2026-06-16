import socket
import uuid

import pytest
from ozonenv.OzonEnv import OzonEnv

from app.app_settings import get_env_settings
from app.core.OzonModelApp import OzonModelApp
from app.deps.app_env import _build_ozon_cfg
from app.deps.app_env import _model_to_dict
from app.deps.app_env import sync_app_settings_startup
from app.core.models import AppUser

def _localhost_mongo_available(host: str = "127.0.0.1", port: int = 22222) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1.0):
            return True
    except OSError:
        return False


async def _load_settings_record(settings_model, app_code: str) -> dict:
    if hasattr(settings_model, "by_name"):
        record = await settings_model.by_name(app_code)
        if getattr(settings_model.status, "fail", False):
            return {}
        return _model_to_dict(record)
    record = await settings_model.load({"rec_name": app_code})
    if getattr(settings_model.status, "fail", False):
        return {}
    return _model_to_dict(record)


@pytest.mark.asyncio
async def test_sync_app_settings_startup_persists_public_record_real_mongo(
    tmp_path,
):
    if not _localhost_mongo_available():
        pytest.skip("Mongo locale non disponibile su 127.0.0.1:22222")

    base_settings = get_env_settings()
    temp_app_code = f"it-settings-{uuid.uuid4().hex[:8]}"
    real_settings = base_settings.model_copy(
        update={
            "app_code": temp_app_code,
            "mongo_url": "127.0.0.1:22222",
            "models_folder": str(tmp_path / "models"),
            "app_name": "Integration Test App",
            "app_version": "9.9.9",
            "module_label": "Integration Label",
            "description": "Integration Description",
            "admins": ["integration.admin"],
        }
    )

    cfg = _build_ozon_cfg()
    cfg.update(
        {
            "app_code": real_settings.app_code,
            "mongo_url": real_settings.mongo_url,
            "mongo_db": real_settings.mongo_db,
            "mongo_user": real_settings.mongo_user,
            "mongo_pass": real_settings.mongo_pass,
            "mongo_replica": real_settings.mongo_replica,
            "models_folder": real_settings.models_folder,
        }
    )

    env = OzonEnv(cfg=cfg, cls_model=OzonModelApp)
    await env.init_env(local_model={"user":AppUser})
    settings_model = env.get("settings")
    existing = await _load_settings_record(settings_model, temp_app_code)
    if existing:
        loaded = await settings_model.load({"rec_name": temp_app_code})
        if not settings_model.status.fail and loaded:
            await settings_model.remove(loaded)
    await env.close_env()

    try:
        await sync_app_settings_startup(real_settings)

        verify_env = OzonEnv(cfg=cfg, cls_model=OzonModelApp)
        await verify_env.init_env()
        verify_model = verify_env.get("settings")
        record = await _load_settings_record(verify_model, temp_app_code)

        assert record["rec_name"] == temp_app_code
        assert record["app_code"] == temp_app_code
        assert record["module_label"] == "Integration Label"
        assert record["description"] == "Integration Description"
        assert record["admins"] == ["integration.admin"]
        assert record["version"] == "9.9.9"
        assert "session_secret" not in record
        assert "keycloak_client_secret" not in record
        assert "camunda_client_secret" not in record
        await verify_env.close_env()
    finally:
        cleanup_env = OzonEnv(cfg=cfg, cls_model=OzonModelApp)
        await cleanup_env.init_env()
        cleanup_model = cleanup_env.get("settings")
        loaded = await cleanup_model.load({"rec_name": temp_app_code})
        if not cleanup_model.status.fail and loaded:
            await cleanup_model.remove(loaded)
        await cleanup_env.close_env()


@pytest.mark.asyncio
async def test_sync_app_settings_startup_backfills_admins_real_mongo(tmp_path):
    if not _localhost_mongo_available():
        pytest.skip("Mongo locale non disponibile su 127.0.0.1:22222")

    base_settings = get_env_settings()
    temp_app_code = f"it-settings-admins-{uuid.uuid4().hex[:8]}"
    real_settings = base_settings.model_copy(
        update={
            "app_code": temp_app_code,
            "mongo_url": "127.0.0.1:22222",
            "models_folder": str(tmp_path / "models"),
            "app_name": "Integration Test App",
            "app_version": "9.9.9",
            "module_label": "Integration Label",
            "description": "Integration Description",
            "admins": ["integration.admin"],
        }
    )

    cfg = _build_ozon_cfg()
    cfg.update(
        {
            "app_code": real_settings.app_code,
            "mongo_url": real_settings.mongo_url,
            "mongo_db": real_settings.mongo_db,
            "mongo_user": real_settings.mongo_user,
            "mongo_pass": real_settings.mongo_pass,
            "mongo_replica": real_settings.mongo_replica,
            "models_folder": real_settings.models_folder,
        }
    )

    env = OzonEnv(cfg=cfg, cls_model=OzonModelApp)
    await env.init_env()
    settings_model = env.get("settings")
    existing = await _load_settings_record(settings_model, temp_app_code)
    if existing:
        loaded = await settings_model.load({"rec_name": temp_app_code})
        if not settings_model.status.fail and loaded:
            await settings_model.remove(loaded)

    record = await settings_model.new(
        data={
            "rec_name": temp_app_code,
            "app_code": temp_app_code,
            "module_label": "Integration Label",
            "description": "Integration Description",
            "admins": [],
            "active": True,
            "deleted": 0,
        }
    )
    saved = await settings_model.insert(record)
    assert saved is not None
    await env.close_env()

    try:
        await sync_app_settings_startup(real_settings)

        verify_env = OzonEnv(cfg=cfg, cls_model=OzonModelApp)
        await verify_env.init_env()
        verify_model = verify_env.get("settings")
        record = await _load_settings_record(verify_model, temp_app_code)

        assert record["rec_name"] == temp_app_code
        assert record["admins"] == ["integration.admin"]
        await verify_env.close_env()
    finally:
        cleanup_env = OzonEnv(cfg=cfg, cls_model=OzonModelApp)
        await cleanup_env.init_env()
        cleanup_model = cleanup_env.get("settings")
        loaded = await cleanup_model.load({"rec_name": temp_app_code})
        if not cleanup_model.status.fail and loaded:
            await cleanup_model.remove(loaded)
        await cleanup_env.close_env()
