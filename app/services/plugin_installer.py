from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from app.core.models import AppUser

logger = logging.getLogger("uvicorn.error")

_SKIP_COLLECTIONS: frozenset[str] = frozenset({"user"})
_REGISTRY_COLLECTION = "plugin_registry"


class PluginInstaller:
    def __init__(self, cfg: dict, app_code: str = "") -> None:
        self.cfg = cfg
        self.app_code = app_code

    async def run(self, plugins: list[Path]) -> None:
        from app.core.OzonEnvApp import AppOzonEnv
        from app.services.service import Service

        env = AppOzonEnv(cfg=self.cfg)
        await env.init_env(local_model={"user":AppUser})
        try:
            db = env.orm.db
            # Sessione sintetica: il bootstrap plugin-install non ha una
            # request/login reale (env.user_session e' None qui, e
            # Service.__init__ stesso legge self.session.app_code senza
            # getattr difensivo nel suo log — va valorizzata PRIMA di
            # costruire Service, non dopo). Bastano is_admin=True (bypass
            # ACL, non e' un accesso utente) e l'app_code del plugin
            # corrente per _create_menu_dashboard_for_component/_make_
            # default_actions_for_component.
            env.user_session = SimpleNamespace(
                app_code=self.app_code,
                is_admin=True,
                user={"groups": [], "uid": "plugin-installer"},
            )
            service = Service(env)
            for plugin_dir in plugins:
                await self._install_plugin(plugin_dir, db, service)
            from app.ozon_env_acl.model_rules_sync import sync_all_model_rules

            await sync_all_model_rules(env)
        finally:
            await env.close_env()

    async def _install_plugin(
        self, plugin_dir: Path, db: Any, service: Any
    ) -> None:
        config_path = plugin_dir / "config.json"
        if not config_path.exists():
            logger.warning("plugin dir %s has no config.json, skipping", plugin_dir)
            return

        config = json.loads(config_path.read_text(encoding="utf-8"))
        module_name = config.get("module_name", plugin_dir.name)
        no_update = config.get("no_update", False)
        auto_create_actions = config.get("auto_create_actions", True)

        if no_update and await self._is_installed(module_name, db):
            logger.info("plugin '%s' already installed (no_update=true), skipping", module_name)
            return

        # Schema → component collection
        schema_rel = config.get("schema", "")
        if schema_rel:
            schema_path = plugin_dir / schema_rel.lstrip("/")
            if schema_path.exists():
                components = json.loads(schema_path.read_text(encoding="utf-8"))
                count, newly_inserted = await self._upsert_all(
                    "component", components, db
                )
                logger.info(
                    "plugin '%s': upserted %d components (%d new)",
                    module_name,
                    count,
                    len(newly_inserted),
                )
                await self._register_new_component_models(
                    components,
                    newly_inserted,
                    service,
                )
                if auto_create_actions:
                    # Gate su "questo model ha gia' almeno un'action?", non
                    # su "upserted_id era settato ADESSO" (newly_inserted):
                    # quel segnale e' vero SOLO nell'istante esatto in cui il
                    # component viene inserito per la prima volta — se il
                    # boot che lo inserisce fallisce PRIMA di arrivare alla
                    # generazione menu/action (crash, errore a monte), il
                    # component non e' piu' "new" ai boot successivi e la
                    # generazione non riprovava piu' mai (serviva il
                    # workaround manuale "Design form -> genera menu").
                    # Il check sull'esistenza dell'action e' un segnale
                    # PERSISTENTE: riprova sempre finche' non c'e' almeno
                    # un'action, ma non tocca MAI un model gia' provisionato
                    # con successo — l'action e' un upsert col template, non
                    # create-if-absent come menu_group, rigenerarla ad ogni
                    # boot cancellerebbe le modifiche manuali.
                    needs_dashboard = await self._components_needing_menu_dashboard(
                        components, db
                    )
                    target_components = [
                        component
                        for component in components
                        if isinstance(component, dict)
                        and component.get("rec_name") in needs_dashboard
                    ]
                    await self._create_menu_dashboard_for_components(
                        module_name, target_components, service
                    )
                else:
                    logger.info(
                        "plugin '%s': auto_create_actions=false, skip menu/action",
                        module_name,
                    )

        # Data files
        for entry in config.get("datas", []):
            for coll_name, rel_path in entry.items():
                if coll_name in _SKIP_COLLECTIONS:
                    continue
                data_path = plugin_dir / rel_path.lstrip("/")
                if not data_path.exists():
                    logger.warning("plugin '%s': data file %s not found", module_name, data_path)
                    continue
                records = json.loads(data_path.read_text(encoding="utf-8"))
                count, _ = await self._upsert_all(coll_name, records, db)
                logger.info("plugin '%s': upserted %d records into '%s'", module_name, count, coll_name)

        await self._mark_installed(module_name, db)
        logger.info("plugin '%s' installed", module_name)

    async def _register_new_component_models(
        self,
        components: list[dict],
        newly_inserted: set[str],
        service: Any,
    ) -> None:
        """Registra nello stesso bootstrap i model appena inseriti raw.

        `env.init_env()` precede l'installazione dei plugin, quindi al primo
        avvio non può conoscere i component che il plugin inserirà subito
        dopo. Senza questa registrazione, il pass ACL finale trova in Mongo
        `model_groups_rule`/`model_fields_rule`, ma `env.get()` restituisce
        ancora ``None`` fino al riavvio successivo.
        """
        if not newly_inserted:
            return

        from app.core.OzonEnvApp import is_runtime_model_name

        component_names = {
            str(component.get("rec_name") or "").strip()
            for component in components
            if isinstance(component, dict)
        }
        for model_name in sorted(component_names & newly_inserted):
            if is_runtime_model_name(model_name):
                await service.env.orm.add_model(model_name)

    async def _create_menu_dashboard_for_components(
        self, module_name: str, components: list[dict], service: Any
    ) -> None:
        """Menu+action per ogni component dello schema del plugin, a meno di
        `auto_create_actions: false` in config.json (vedi 'base', che spedisce
        gia' i propri action.json/menu_group.json fatti a mano — l'auto-
        generazione duplicherebbe/confliggerebbe). Riusa la stessa hook
        (`_create_menu_dashboard_for_component`, gia' testata) invocata per un
        singolo save via API: stessa idempotenza (menu_group solo se assente,
        action via upsert), stesso skip per `data_model: no_model`."""
        for component in components:
            if not isinstance(component, dict):
                continue
            rec_name = str(component.get("rec_name", "") or "").strip()
            try:
                await service._create_menu_dashboard_for_component(component)
            except Exception:
                logger.exception(
                    "plugin '%s': menu/action creation failed component=%s",
                    module_name,
                    rec_name,
                )

    async def _components_needing_menu_dashboard(
        self, components: list[dict], db: Any
    ) -> set[str]:
        """rec_name di ogni component SENZA nemmeno un'action esistente —
        segnale persistente di "mai completato con successo", a differenza
        di `newly_inserted` in `_upsert_all` (vero solo nell'istante esatto
        del primo insert, quindi cieco a un boot che inserisce il component
        ma fallisce PRIMA di generare menu/action). L'action e' l'artefatto
        giusto da controllare (non menu_group): menu_group non si crea
        affatto per i component `type: "resource"`, mentre l'action si
        crea sempre, per qualunque tipo di component — controllare
        menu_group farebbe ritentare all'infinito ogni "resource",
        sovrascrivendo ad ogni boot le sue action anche se gia' presenti."""
        action_coll = db.engine.get_collection("action")
        needing: set[str] = set()
        for component in components:
            if not isinstance(component, dict):
                continue
            rec_name = str(component.get("rec_name", "") or "").strip()
            if not rec_name:
                continue
            exists = await action_coll.count_documents(
                {"model": rec_name, "deleted": 0}
            )
            if not exists:
                needing.add(rec_name)
        return needing

    async def _is_installed(self, module_name: str, db: Any) -> bool:
        coll = db.engine.get_collection(_REGISTRY_COLLECTION)
        return bool(await coll.find_one({"module_name": module_name}))

    async def _upsert_all(
        self, collection: str, records: list[dict], db: Any
    ) -> tuple[int, set[str]]:
        coll = db.engine.get_collection(collection)
        count = 0
        newly_inserted: set[str] = set()
        for record in records:
            rec_name = record.get("rec_name")
            if not rec_name:
                continue
            # `_id` non deve MAI finire in un $set: i seed JSON lo portano come
            # stringa hex, ma i doc creati dall'ORM hanno _id ObjectId -> il $set
            # su _id (immutabile) fallisce a ogni boot (code 66). Le relazioni
            # usano rec_name, non _id: lo si scarta e Mongo gestisce _id.
            payload = {k: v for k, v in record.items() if k != "_id"}
            result = await coll.update_one(
                {"rec_name": rec_name},
                {"$set": payload},
                upsert=True,
            )
            if getattr(result, "upserted_id", None) is not None:
                newly_inserted.add(rec_name)
            count += 1
        return count, newly_inserted

    async def _mark_installed(self, module_name: str, db: Any) -> None:
        from ozonenv.core.BaseModels import BasicModel

        coll = db.engine.get_collection(_REGISTRY_COLLECTION)
        await coll.update_one(
            {"module_name": module_name},
            {"$set": {
                "module_name": module_name,
                "installed_at": BasicModel.utc_now(),
            }},
            upsert=True,
        )
