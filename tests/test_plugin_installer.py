import asyncio
import json
import types

from app.services.plugin_installer import PluginInstaller


class FakeCollection:
    def __init__(self):
        self.calls = []
        self._seen_filters: set[str] = set()
        self.docs: list[dict] = []

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
        # usato solo per il log "(%d new)", non piu' per decidere se
        # generare menu/action (vedi _components_needing_menu_dashboard).
        key = json.dumps(filter_doc, sort_keys=True)
        is_new = key not in self._seen_filters
        self._seen_filters.add(key)
        if is_new:
            self.docs.append(dict(update_doc.get("$set", {})))
        return types.SimpleNamespace(upserted_id=("new-id" if is_new else None))

    async def count_documents(self, filter_doc):
        def matches(doc):
            return all(doc.get(k) == v for k, v in filter_doc.items())

        return len([doc for doc in self.docs if matches(doc)])

    async def insert_one(self, doc):
        self.docs.append(dict(doc))


class FakeDb:
    def __init__(self, coll, action_coll=None):
        collections = {"action": action_coll} if action_coll is not None else {}
        self.engine = types.SimpleNamespace(
            get_collection=lambda name: collections.get(name, coll)
        )


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
    """Fake Service: registra quali component hanno ricevuto la call a
    _create_menu_dashboard_for_component — la logica di skip (no_model,
    rec_name non valido) vive gia' dentro quel metodo (testata altrove),
    qui si verifica solo che PluginInstaller lo invochi per i component
    giusti. Se `action_coll` e' passato, simula il side-effect reale
    (un'action creata per il model) cosi' i test su piu' boot successivi
    possono verificare che _components_needing_menu_dashboard smetta di
    richiamare l'hook una volta che l'action esiste davvero."""

    def __init__(self, action_coll=None):
        self.calls = []
        self._action_coll = action_coll

    async def _create_menu_dashboard_for_component(self, schema):
        rec_name = schema.get("rec_name")
        self.calls.append(rec_name)
        if self._action_coll is not None:
            await self._action_coll.insert_one(
                {"model": rec_name, "rec_name": f"action_{rec_name}", "deleted": 0}
            )


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
    action_coll = FakeCollection()

    def get_collection(name):
        if name == "plugin_registry":
            return registry_coll
        if name == "action":
            return action_coll
        return coll

    db = types.SimpleNamespace(
        engine=types.SimpleNamespace(get_collection=get_collection)
    )
    return db, action_coll


def test_install_plugin_creates_menu_dashboard_by_default(tmp_path):
    """Task: "quando carico un plugin deve assolutamente creare menu e
    action" — auto_create_actions assente/true (default) -> la hook menu/
    action gira per ogni component dello schema del plugin."""
    plugin_dir = _write_plugin(tmp_path)
    db, action_coll = _fake_db()
    installer = PluginInstaller(cfg={})
    service = _SpyService(action_coll)

    asyncio.run(installer._install_plugin(plugin_dir, db, service))

    assert service.calls == ["demo_model"]


def test_install_plugin_skips_menu_dashboard_when_auto_create_actions_false(
    tmp_path,
):
    """'base' spedisce gia' i propri action.json/menu_group.json fatti a
    mano (auto_create_actions=false in config.json) — l'auto-generazione
    duplicherebbe/confliggerebbe, deve restare disattivabile."""
    plugin_dir = _write_plugin(tmp_path, {"auto_create_actions": False})
    db, action_coll = _fake_db()
    installer = PluginInstaller(cfg={})
    service = _SpyService(action_coll)

    asyncio.run(installer._install_plugin(plugin_dir, db, service))

    assert service.calls == []


def test_install_plugin_second_boot_does_not_regenerate_existing_actions(
    tmp_path,
):
    """Regressione: un plugin non-no_update re-upserta lo schema ad OGNI
    boot (idempotente per definizione). Un model gia' PROVISIONATO CON
    SUCCESSO (ha gia' almeno un'action) non deve essere ritoccato: l'action
    e' un upsert-da-template, non create-if-absent — rigenerarla ad ogni
    boot cancellerebbe una modifica manuale fatta dopo il primo boot."""
    plugin_dir = _write_plugin(tmp_path)
    db, action_coll = _fake_db()
    installer = PluginInstaller(cfg={})

    first_service = _SpyService(action_coll)
    asyncio.run(installer._install_plugin(plugin_dir, db, first_service))
    assert first_service.calls == ["demo_model"]

    second_service = _SpyService(action_coll)
    asyncio.run(installer._install_plugin(plugin_dir, db, second_service))
    assert second_service.calls == []


def test_install_plugin_retries_menu_dashboard_after_failed_first_boot(
    tmp_path,
):
    """Regressione per il bug reale: se il PRIMO boot inserisce il
    component ma fallisce PRIMA di generare menu/action (crash, errore a
    monte — es. il codegen di un field type non supportato), il vecchio
    gate su "upserted_id era settato ADESSO" non riprovava mai piu': il
    component non e' piu' "new" al boot successivo, quindi l'hook menu/
    action non veniva piu' invocata finche' qualcuno non lo generava a
    mano da "Design form". Qui: component gia' presente in DB (simulando
    un boot precedente riuscito solo a meta'), ZERO action esistenti -> il
    boot successivo deve ritentare e riuscire, senza intervento manuale."""
    plugin_dir = _write_plugin(tmp_path)
    db, action_coll = _fake_db()
    # Simula il component gia' upsertato da un boot precedente (fallito
    # prima di arrivare a menu/action) — nessuna action esiste ancora.
    installer = PluginInstaller(cfg={})
    asyncio.run(
        installer._upsert_all(
            "component",
            [{"rec_name": "demo_model", "type": "resource", "sys": False}],
            db,
        )
    )
    assert action_coll.docs == []

    service = _SpyService(action_coll)
    asyncio.run(installer._install_plugin(plugin_dir, db, service))

    assert service.calls == ["demo_model"]
    assert action_coll.docs != []
