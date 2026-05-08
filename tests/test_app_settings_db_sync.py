import asyncio
from types import SimpleNamespace

from app.app_settings import EnvSettings
from app.deps import app_env


class _FakeRecord:
    def __init__(self, payload=None):
        self._payload = dict(payload or {})
        for key, value in self._payload.items():
            setattr(self, key, value)

    def model_dump(self, mode="python"):
        payload = self._payload.copy()
        payload.update(
            {
                key: value
                for key, value in self.__dict__.items()
                if key != "_payload"
            }
        )
        return payload


class _FakeSettingsModel:
    def __init__(self, docs=None):
        self.docs = list(docs or [])
        self.new_calls = []
        self.insert_calls = []
        self.update_calls = []
        self.load_calls = []
        self.status = SimpleNamespace(fail=False, msg="")

    async def by_name(self, rec_name):
        self.load_calls.append(rec_name)
        for doc in self.docs:
            if doc.get("rec_name") == rec_name:
                self.status.fail = False
                self.status.msg = ""
                return _FakeRecord(doc)
        self.status.fail = True
        self.status.msg = "Not found"
        return _FakeRecord({})

    async def new(self, data=None, rec_name="", trnf_config=None, fields_parser=None):
        payload = dict(data or {})
        self.new_calls.append(payload.copy())
        return _FakeRecord(payload)

    async def insert(self, record):
        payload = record.model_dump()
        self.insert_calls.append(payload.copy())
        self.docs.append(payload.copy())
        return _FakeRecord(payload)

    async def update(self, record):
        payload = record.model_dump()
        self.update_calls.append(payload.copy())
        rec_name = payload.get("rec_name")
        for index, doc in enumerate(self.docs):
            if doc.get("rec_name") == rec_name:
                self.docs[index] = payload.copy()
                break
        return _FakeRecord(payload)


class _FakeEnv:
    def __init__(self, settings_model, app_settings=None):
        self.orm = SimpleNamespace(
            app_settings=app_settings or SimpleNamespace(rec_name=""),
        )
        self.upload_folder = ""
        self._settings_model = settings_model
        self.models = {
            "settings": settings_model,
            "other": SimpleNamespace(setting_app=SimpleNamespace(rec_name="")),
        }

    def get(self, model_name):
        assert model_name == "settings"
        return self._settings_model


def test_sync_runtime_app_settings_bootstraps_public_record(monkeypatch):
    monkeypatch.setattr(
        app_env,
        "settings",
        EnvSettings(
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
        ),
    )
    settings_model = _FakeSettingsModel()
    env = _FakeEnv(
        settings_model,
        app_settings=SimpleNamespace(rec_name="demo"),
    )

    asyncio.run(app_env._sync_runtime_app_settings(env))

    assert settings_model.load_calls == ["demo", "demo"]
    assert len(settings_model.new_calls) == 1
    assert len(settings_model.insert_calls) == 1
    assert settings_model.update_calls == []
    saved = settings_model.insert_calls[0]
    assert saved["rec_name"] == "demo"
    assert saved["app_code"] == "demo"
    assert saved["module_label"] == "Demo Label"
    assert saved["description"] == "Visible description"
    assert saved["version"] == "2.4.6"
    assert "session_secret" not in saved
    assert "keycloak_client_secret" not in saved
    assert "camunda_client_secret" not in saved
    assert "runtime_internal_token" not in saved
    assert "mongo_pass" not in saved
    assert env.orm.app_settings.app_code == "demo"
    assert env.orm.app_settings.module_label == "Demo Label"


def test_sync_runtime_app_settings_reads_existing_db_record(monkeypatch):
    monkeypatch.setattr(
        app_env,
        "settings",
        EnvSettings(
            app_code="demo",
            app_name="Demo App",
            module_label="Env Label",
            description="Env description",
            admins=["env-admin"],
        ),
    )
    settings_model = _FakeSettingsModel(
        [
            {
                "rec_name": "demo",
                "app_code": "",
                "module_label": "DB Label",
                "description": "DB description",
                "admins": ["db-admin"],
                "active": True,
                "deleted": 0,
            }
        ]
    )
    env = _FakeEnv(settings_model)

    asyncio.run(app_env._sync_runtime_app_settings(env))

    assert settings_model.load_calls == ["demo", "demo", "demo"]
    assert settings_model.new_calls == []
    assert settings_model.insert_calls == []
    assert len(settings_model.update_calls) == 1
    assert settings_model.update_calls[0]["app_code"] == "demo"
    assert env.orm.app_settings.app_code == "demo"
    assert env.orm.app_settings.module_label == "DB Label"
    assert env.orm.app_settings.description == "DB description"
    assert env.orm.app_settings.admins == ["db-admin"]


def test_sync_runtime_app_settings_backfills_admins_when_db_empty(monkeypatch):
    monkeypatch.setattr(
        app_env,
        "settings",
        EnvSettings(
            app_code="demo",
            app_name="Demo App",
            admins=["env-admin"],
        ),
    )
    settings_model = _FakeSettingsModel(
        [
            {
                "rec_name": "demo",
                "app_code": "demo",
                "module_label": "DB Label",
                "description": "DB description",
                "admins": [],
                "active": True,
                "deleted": 0,
            }
        ]
    )
    env = _FakeEnv(settings_model)

    asyncio.run(app_env._sync_runtime_app_settings(env))

    assert settings_model.new_calls == []
    assert settings_model.insert_calls == []
    assert len(settings_model.update_calls) == 1
    assert settings_model.update_calls[0]["admins"] == ["env-admin"]
    assert env.orm.app_settings.admins == ["env-admin"]
