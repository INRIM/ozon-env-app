import asyncio
import json
import types

from app.services.plugin_installer import PluginInstaller


class FakeCollection:
    def __init__(self):
        self.calls = []
        self._seen_filters: set[str] = set()

    async def update_one(self, filter_doc, update_doc, upsert=False):
        # Replica il vincolo Mongo: $set su _id immutabile -> errore.
        if "_id" in update_doc.get("$set", {}):
            raise AssertionError(
                "Performing an update on the path '_id' would modify the "
                "immutable field '_id'"
            )
        self.calls.append(
            {"filter": filter_doc, "update": update_doc, "upsert": upsert}
        )
        # Replica upserted_id di pymongo/motor: None se il filtro matchava
        # gia' un documento esistente, un id se e' stato appena inserito —
        # PluginInstaller._upsert_all lo usa per capire quali record sono
        # NUOVI (menu/action si generano solo su INSERT, non ad ogni boot).
        key = json.dumps(filter_doc, sort_keys=True)
        is_new = key not in self._seen_filters
        self._seen_filters.add(key)
        return types.SimpleNamespace(upserted_id=("new-id" if is_new else None))


class FakeDb:
    def __init__(self, coll):
        self.engine = types.SimpleNamespace(get_collection=lambda name: coll)


def test_upsert_all_strips_id_from_set():
    coll = FakeCollection()
    db = FakeDb(coll)
    installer = PluginInstaller(cfg={})

    records = [
        {"_id": "6a1fe4fb1ec8e6f72599fabf", "rec_name": "a1", "title": "A1"},
        {"rec_name": "a2", "title": "A2"},
        {"title": "no_rec_name"},  # scartato
    ]

    count, newly_inserted = asyncio.run(
        installer._upsert_all("component", records, db)
    )

    assert count == 2
    assert newly_inserted == {"a1", "a2"}
    assert [c["filter"] for c in coll.calls] == [
        {"rec_name": "a1"},
        {"rec_name": "a2"},
    ]
    # _id MAI nel $set (invariante che evita il code 66 a ogni boot)
    for call in coll.calls:
        assert "_id" not in call["update"]["$set"]
    # i restanti campi restano
    assert coll.calls[0]["update"]["$set"] == {"rec_name": "a1", "title": "A1"}
    assert all(c["upsert"] for c in coll.calls)


def test_upsert_all_record_with_id_does_not_raise():
    coll = FakeCollection()
    db = FakeDb(coll)
    installer = PluginInstaller(cfg={})

    # Senza lo strip, la FakeCollection solleverebbe (come Mongo code 66).
    asyncio.run(
        installer._upsert_all(
            "action",
            [{"_id": "deadbeefdeadbeefdeadbeef", "rec_name": "x"}],
            db,
        )
    )

    assert len(coll.calls) == 1
    assert coll.calls[0]["update"] == {"$set": {"rec_name": "x"}}


class _SpyService:
    """Fake Service: registra solo quali component hanno ricevuto la call
    a _create_menu_dashboard_for_component — la logica di skip (no_model,
    rec_name non valido) vive gia' dentro quel metodo (testata altrove),
    qui si verifica solo che PluginInstaller lo invochi per ogni component
    dello schema del plugin."""

    def __init__(self):
        self.calls = []

    async def _create_menu_dashboard_for_component(self, schema):
        self.calls.append(schema.get("rec_name"))


def test_create_menu_dashboard_for_components_invokes_hook_per_component():
    installer = PluginInstaller(cfg={})
    service = _SpyService()
    components = [
        {"rec_name": "modulo_dati_persona", "type": "resource"},
        {"rec_name": "demo_no_model_form", "data_model": "no_model"},
    ]

    asyncio.run(
        installer._create_menu_dashboard_for_components(
            "demo_plugin", components, service
        )
    )

    assert service.calls == ["modulo_dati_persona", "demo_no_model_form"]


def _write_plugin(tmp_path, config_extra=None):
    plugin_dir = tmp_path / "demo_plugin"
    (plugin_dir / "schema").mkdir(parents=True)
    config = {
        "module_name": "demo_plugin",
        "no_update": False,
        "schema": "/schema/components.json",
        "datas": [],
    }
    config.update(config_extra or {})
    (plugin_dir / "config.json").write_text(json.dumps(config))
    (plugin_dir / "schema" / "components.json").write_text(
        json.dumps(
            [{"rec_name": "demo_model", "type": "resource", "sys": False}]
        )
    )
    return plugin_dir


def _fake_db():
    coll = FakeCollection()
    registry_coll = FakeCollection()

    def get_collection(name):
        return registry_coll if name == "plugin_registry" else coll

    return types.SimpleNamespace(
        engine=types.SimpleNamespace(get_collection=get_collection)
    )


def test_install_plugin_creates_menu_dashboard_by_default(tmp_path):
    """Task: "quando carico un plugin deve assolutamente creare menu e
    action" — auto_create_actions assente/true (default) -> la hook menu/
    action gira per ogni component dello schema del plugin."""
    plugin_dir = _write_plugin(tmp_path)
    db = _fake_db()
    installer = PluginInstaller(cfg={})
    service = _SpyService()

    asyncio.run(installer._install_plugin(plugin_dir, db, service))

    assert service.calls == ["demo_model"]


def test_install_plugin_skips_menu_dashboard_when_auto_create_actions_false(
    tmp_path,
):
    """'base' spedisce gia' i propri action.json/menu_group.json fatti a
    mano (auto_create_actions=false in config.json) — l'auto-generazione
    duplicherebbe/confliggerebbe, deve restare disattivabile."""
    plugin_dir = _write_plugin(tmp_path, {"auto_create_actions": False})
    db = _fake_db()
    installer = PluginInstaller(cfg={})
    service = _SpyService()

    asyncio.run(installer._install_plugin(plugin_dir, db, service))

    assert service.calls == []


def test_install_plugin_second_boot_does_not_regenerate_existing_actions(
    tmp_path,
):
    """Regressione: un plugin non-no_update re-upserta lo schema ad OGNI
    boot (idempotente per definizione). Senza il gate su INSERT, la hook
    menu/action girerebbe di nuovo per un component gia' installato,
    sovrascrivendo con l'upsert-da-template un'action eventualmente editata
    a mano dopo il primo boot — stesso motivo per cui l'equivalente lato API
    (Service.upsert) genera i default solo su operation==INSERT."""
    plugin_dir = _write_plugin(tmp_path)
    db = _fake_db()
    installer = PluginInstaller(cfg={})

    first_service = _SpyService()
    asyncio.run(installer._install_plugin(plugin_dir, db, first_service))
    assert first_service.calls == ["demo_model"]

    second_service = _SpyService()
    asyncio.run(installer._install_plugin(plugin_dir, db, second_service))
    assert second_service.calls == []
