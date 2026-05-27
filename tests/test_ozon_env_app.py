import asyncio
from pathlib import Path
from types import SimpleNamespace

from app.core.OzonEnvApp import AppOzonEnv
from app.core.OzonEnvApp import AppOzonOrm
from app.core.OzonEnvApp import is_runtime_model_name


class _FakeComponentModel:
    def __init__(self, existing=None):
        self.existing = existing
        self.inserted = []
        self.updated = []

    async def load(self, query):
        if self.existing and query.get("rec_name") == self.existing.get(
            "rec_name"
        ):
            return self.existing.copy()
        return None

    async def new(self, data):
        return dict(data)

    async def insert(self, record):
        self.inserted.append(record.copy())
        return record

    async def update(self, record):
        self.updated.append(record.copy())
        return record


class _FakeOrm:
    def __init__(self):
        self.added = []
        self.updated = []

    async def add_model(self, model_name):
        self.added.append(model_name)

    async def update_model(self, schema, component):
        self.updated.append(
            {
                "schema": dict(schema),
                "component": dict(component),
            }
        )


class _FakeComponentCollection:
    def __init__(self, distinct_values):
        self.distinct_values = list(distinct_values)

    async def distinct(self, field_name, query):
        return list(self.distinct_values)


class _FakeDbEngine:
    def __init__(self, collection_names, distinct_values):
        self.collection_names = list(collection_names)
        self.component_collection = _FakeComponentCollection(distinct_values)

    async def list_collection_names(self, filter=None):
        return list(self.collection_names)

    def get_collection(self, name):
        if name != "component":
            raise AssertionError(f"unexpected collection {name}")
        return self.component_collection


class _FakeEnv:
    def __init__(self, tmp_path: Path, collection_names, distinct_values):
        self.lang = "it"
        self.db = SimpleNamespace(
            engine=_FakeDbEngine(collection_names, distinct_values)
        )
        self.config_system = {}
        self.models = {}
        self.models_folder = str(tmp_path / "models")
        self.app_code = "demo"


def test_is_runtime_model_name_rejects_builder_temporary_names():
    assert is_runtime_model_name("nullaOstaBandiRequest") is True
    assert (
        is_runtime_model_name(
            "component_40c6976d968c4966800a0667521fde47"
        )
        is True
    )
    assert (
        is_runtime_model_name(
            "component.40c6976d968c4966800a0667521fde47"
        )
        is False
    )
    assert is_runtime_model_name("") is False


def test_insert_update_component_skips_runtime_sync_for_builder_temporary_name():
    env = AppOzonEnv(cfg={"app_code": "demo"})
    component_model = _FakeComponentModel()
    orm = _FakeOrm()
    env.models = {"component": component_model}
    env.orm = orm

    asyncio.run(
        env.insert_update_component(
            {
                "rec_name": "component.40c6976d968c4966800a0667521fde47",
                "type": "form",
            }
        )
    )

    assert component_model.inserted == [
        {
            "rec_name": "component.40c6976d968c4966800a0667521fde47",
            "type": "form",
        }
    ]
    assert orm.added == []
    assert orm.updated == []


def test_insert_update_component_keeps_runtime_sync_for_valid_component_name():
    env = AppOzonEnv(cfg={"app_code": "demo"})
    component_model = _FakeComponentModel()
    orm = _FakeOrm()
    env.models = {"component": component_model}
    env.orm = orm

    asyncio.run(
        env.insert_update_component(
            {
                "rec_name": "nullaOstaBandiRequest",
                "type": "form",
            }
        )
    )

    assert component_model.inserted == [
        {
            "rec_name": "nullaOstaBandiRequest",
            "type": "form",
        }
    ]
    assert orm.added == ["nullaOstaBandiRequest"]
    assert orm.updated == []


def test_get_collections_names_filters_non_runtime_component_names(tmp_path):
    env = _FakeEnv(
        tmp_path=tmp_path,
        collection_names=["component", "settings", "nullaOstaBandiRequest"],
        distinct_values=[
            "nullaOstaBandiRequest",
            "component.40c6976d968c4966800a0667521fde47",
            "component_40c6976d968c4966800a0667521fde47",
        ],
    )
    orm = AppOzonOrm(env)

    model_names = asyncio.run(orm.get_collections_names())

    assert model_names == [
        "component",
        "settings",
        "nullaOstaBandiRequest",
        "component_40c6976d968c4966800a0667521fde47",
    ]


def test_import_module_model_ignores_invalid_runtime_name(tmp_path):
    env = _FakeEnv(
        tmp_path=tmp_path,
        collection_names=[],
        distinct_values=[],
    )
    orm = AppOzonOrm(env)

    asyncio.run(
        orm.import_module_model("component.40c6976d968c4966800a0667521fde47")
    )

    assert (
        "component.40c6976d968c4966800a0667521fde47"
        not in orm.orm_static_models_map
    )
