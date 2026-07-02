import logging
import re
from typing import Any

from ozonenv.OzonEnv import OzonEnv
from ozonenv.core.OzonOrm import OzonOrm
from ozonenv.core.OzonOrm import OzonOrmRest

logger = logging.getLogger("uvicorn.error")

RUNTIME_MODEL_NAME_PATTERN = r"^[A-Za-z][A-Za-z0-9_]*$"
_RUNTIME_MODEL_NAME_RE = re.compile(RUNTIME_MODEL_NAME_PATTERN)


def is_runtime_model_name(name: Any) -> bool:
    normalized = str(name or "").strip()
    if not normalized:
        return False
    return bool(_RUNTIME_MODEL_NAME_RE.fullmatch(normalized))


def normalize_component_properties(schema: Any) -> None:
    if not isinstance(schema, dict):
        return
    properties = schema.get("properties")
    if isinstance(properties, str):
        import json
        try:
            properties = json.loads(properties)
            schema["properties"] = properties
        except Exception:
            pass

    if isinstance(properties, dict):
        # 1. query -> queryformeditable
        query_val = properties.get("query")
        if query_val is not None:
            import json
            if isinstance(query_val, (dict, list)):
                properties["queryformeditable"] = json.dumps(query_val)
            else:
                properties["queryformeditable"] = str(query_val)

        # 2. Orderby/orderby -> sort
        orderby_val = properties.get("Orderby")
        if orderby_val is None:
            orderby_val = properties.get("orderby")
        if orderby_val is not None:
            properties["sort"] = str(orderby_val)

        # 3. models_groups / models_restricted_fields: JSON string -> dict/list
        for acl_key in ("models_groups", "models_restricted_fields"):
            acl_val = properties.get(acl_key)
            if isinstance(acl_val, str):
                import json
                try:
                    properties[acl_key] = json.loads(acl_val)
                except Exception:
                    pass


class _RuntimeModelGuardMixin:
    def _filter_runtime_model_names(
        self,
        model_names: list[Any],
    ) -> list[str]:
        filtered: list[str] = []
        skipped: set[str] = set()
        for name in model_names:
            normalized = str(name or "").strip()
            if not normalized:
                continue
            if (
                normalized in self.orm_static_models_map
                or is_runtime_model_name(normalized)
            ):
                filtered.append(normalized)
                continue
            if normalized not in skipped:
                logger.info(
                    "skip bootstrap for non-runtime component model=%s",
                    normalized,
                )
                skipped.add(normalized)
        return list(dict.fromkeys(filtered))

    async def get_collections_names(self, query={}):
        model_names = await super().get_collections_names(query=query)
        return self._filter_runtime_model_names(model_names)

    async def import_module_model(self, model_name):
        if not is_runtime_model_name(model_name):
            logger.info(
                "skip module import for non-runtime component model=%s",
                model_name,
            )
            return
        return await super().import_module_model(model_name)

    async def add_model(self, model_name, virtual=False, data_model=""):
        if not virtual and not is_runtime_model_name(model_name):
            logger.info(
                "skip runtime add_model for non-runtime component model=%s",
                model_name,
            )
            return
        return await super().add_model(
            model_name,
            virtual=virtual,
            data_model=data_model,
        )

    async def update_model(self, schema, component):
        model_name = str(schema.get("rec_name", "") or "").strip()
        if not is_runtime_model_name(model_name):
            logger.info(
                "skip runtime update_model for non-runtime component model=%s",
                model_name,
            )
            return
        return await super().update_model(schema, component)


class AppOzonOrm(_RuntimeModelGuardMixin, OzonOrm):
    async def init_settings(self, app_code):
        # _sync_runtime_app_settings (called after init_env) is the authoritative
        # DB read and propagates merged settings to all models. Return a shell
        # here to avoid a redundant DB round-trip during init_db_models.
        from ozonenv.core.BaseModels import Settings
        return Settings(
            rec_name=app_code or "",
            app_code=app_code or "",
            upload_folder="/tmp/uploads",
            tz="Europe/Rome",
            check_fields=False,
        )


class AppOzonOrmRest(_RuntimeModelGuardMixin, OzonOrmRest):
    pass


class AppOzonEnv(OzonEnv):
    def get_orm_class(self):
        if self.get_backend_interface() == "rest":
            return AppOzonOrmRest
        return AppOzonOrm

    async def insert_update_component(self, schema):
        normalize_component_properties(schema)
        c_model = self.get("component")
        model_name = str(schema.get("rec_name", "") or "").strip()
        component = await c_model.load({"rec_name": model_name})
        new_component = await c_model.new(data=schema)
        should_sync_runtime = is_runtime_model_name(model_name)

        if not component:
            res = await c_model.insert(new_component)
            if should_sync_runtime:
                await self.orm.add_model(model_name)
            else:
                logger.info(
                    "skip runtime registration for non-runtime component rec_name=%s",
                    model_name,
                )
        else:
            res = await c_model.update(new_component)
            if should_sync_runtime:
                await self.orm.update_model(schema, component)
            else:
                logger.info(
                    "skip runtime refresh for non-runtime component rec_name=%s",
                    model_name,
                )
        return res
