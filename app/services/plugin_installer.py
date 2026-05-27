from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger("uvicorn.error")

_SKIP_COLLECTIONS: frozenset[str] = frozenset({"user"})
_REGISTRY_COLLECTION = "plugin_registry"


class PluginInstaller:
    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg

    async def run(self, plugins: list[Path]) -> None:
        from app.core.OzonEnvApp import AppOzonEnv

        env = AppOzonEnv(cfg=self.cfg)
        await env.init_env()
        try:
            db = env.orm.db
            for plugin_dir in plugins:
                await self._install_plugin(plugin_dir, db)
        finally:
            await env.close_env()

    async def _install_plugin(self, plugin_dir: Path, db: Any) -> None:
        config_path = plugin_dir / "config.json"
        if not config_path.exists():
            logger.warning("plugin dir %s has no config.json, skipping", plugin_dir)
            return

        config = json.loads(config_path.read_text(encoding="utf-8"))
        module_name = config.get("module_name", plugin_dir.name)
        no_update = config.get("no_update", False)

        if no_update and await self._is_installed(module_name, db):
            logger.info("plugin '%s' already installed (no_update=true), skipping", module_name)
            return

        # Schema → component collection
        schema_rel = config.get("schema", "")
        if schema_rel:
            schema_path = plugin_dir / schema_rel.lstrip("/")
            if schema_path.exists():
                components = json.loads(schema_path.read_text(encoding="utf-8"))
                count = await self._upsert_all("component", components, db)
                logger.info("plugin '%s': upserted %d components", module_name, count)

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
                count = await self._upsert_all(coll_name, records, db)
                logger.info("plugin '%s': upserted %d records into '%s'", module_name, count, coll_name)

        await self._mark_installed(module_name, db)
        logger.info("plugin '%s' installed", module_name)

    async def _is_installed(self, module_name: str, db: Any) -> bool:
        coll = db.engine.get_collection(_REGISTRY_COLLECTION)
        return bool(await coll.find_one({"module_name": module_name}))

    async def _upsert_all(self, collection: str, records: list[dict], db: Any) -> int:
        coll = db.engine.get_collection(collection)
        count = 0
        for record in records:
            rec_name = record.get("rec_name")
            if not rec_name:
                continue
            await coll.update_one(
                {"rec_name": rec_name},
                {"$set": record},
                upsert=True,
            )
            count += 1
        return count

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
