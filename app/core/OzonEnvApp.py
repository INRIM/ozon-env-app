import json
import logging
import re
from typing import Any

from ozonenv.OzonEnv import OzonEnv
from ozonenv.core.OzonOrm import OzonOrm
from ozonenv.core.OzonOrm import OzonOrmRest

logger = logging.getLogger("uvicorn.error")

RUNTIME_MODEL_NAME_PATTERN = r"^[A-Za-z][A-Za-z0-9_]*$"
_RUNTIME_MODEL_NAME_RE = re.compile(RUNTIME_MODEL_NAME_PATTERN)

# Identity layer: stessa definizione usata in app.services.action_runtime
# (_is_action_allowed) — modelli esclusi dai default models_groups/
# models_restricted_fields perche' gestiti a mano (accesso solo admin).
# model_groups_rule/model_fields_rule sono le tabelle flat del motore ACL
# stesso: devono restare admin-only per costruzione (deny-by-default).
IDENTITY_MODEL_NAMES = frozenset(
    {"user", "groups", "group_users", "model_groups_rule", "model_fields_rule"}
)

_DEFAULT_MODELS_RESTRICTED_FIELDS: dict[str, Any] = {
    "fields_rule": {
        "resticted_fields": [],
        "allowed_groups": [
            {
                "groups": ["gdpr"],
                "actions": {
                    "read": True,
                    "create": True,
                    "update": True,
                    "delete": False,
                },
            },
            {
                "groups": ["dpo"],
                "actions": {
                    "read": True,
                    "create": False,
                    "update": False,
                    "delete": False,
                },
            },
        ],
    },
    "record_rulse": [
        {
            "filters": {"owner_uid": {"$eq": {"var": "user.uid"}}},
            "actions": {
                "read": True,
                "create": True,
                "update": True,
                "delete": True,
            },
            "resticted_fields": [],
        }
    ],
}

_DEFAULT_MODELS_GROUPS_NON_SYS: dict[str, Any] = {
    "rules": [
        {
            "groups": ["admin"],
            "actions": {
                "read": True,
                "create": True,
                "update": True,
                "delete": True,
                "export": True,
            },
        },
        {
            "groups": ["user"],
            "actions": {
                "read": True,
                "create": False,
                "update": False,
                "delete": False,
                "export": False,
            },
        },
        {
            "groups": ["technical_operator"],
            "actions": {
                "read": True,
                "create": False,
                "update": False,
                "delete": False,
                "export": True,
            },
        },
        {
            "groups": ["operator", "manager", "dpo"],
            "actions": {
                "read": True,
                "create": True,
                "update": True,
                "delete": False,
                "export": True,
            },
        },
    ]
}

_DEFAULT_MODELS_GROUPS_SYS: dict[str, Any] = {
    "rules": [
        {
            "groups": ["admin"],
            "actions": {
                "read": True,
                "create": True,
                "update": True,
                "delete": True,
                "export": True,
            },
        },
        {
            "groups": ["technical_operator"],
            "actions": {
                "read": True,
                "create": True,
                "update": True,
                "delete": False,
                "export": True,
            },
        },
    ]
}


def is_runtime_model_name(name: Any) -> bool:
    normalized = str(name or "").strip()
    if not normalized:
        return False
    return bool(_RUNTIME_MODEL_NAME_RE.fullmatch(normalized))


def _is_identity_model(schema: dict) -> bool:
    rec_name = str(schema.get("rec_name", "") or "").strip()
    return bool(schema.get("sys")) and rec_name in IDENTITY_MODEL_NAMES


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

    if properties is None:
        properties = {}
        schema["properties"] = properties

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

        # 4. Default models_groups / models_restricted_fields per i record
        # nuovi (non_sys) e per i sys esistenti/nuovi esclusa identity layer.
        # setdefault: non tocca override gia' presenti. Vedi app.ozon_env_acl
        # per l'enforcement effettivo (motore ACL, prossimo step).
        if not _is_identity_model(schema):
            default_groups = (
                _DEFAULT_MODELS_GROUPS_SYS
                if schema.get("sys")
                else _DEFAULT_MODELS_GROUPS_NON_SYS
            )
            properties.setdefault("models_groups", default_groups)
            properties.setdefault(
                "models_restricted_fields", _DEFAULT_MODELS_RESTRICTED_FIELDS
            )


_ACL_PROPERTY_KEYS = ("models_groups", "models_restricted_fields")


def preserve_acl_properties_on_partial_save(
    schema: dict, existing_component: Any
) -> None:
    """Ripristina `models_groups`/`models_restricted_fields` dal component
    esistente se il nuovo payload di save non le porta.

    `OzonModel.update()` (ozon-env) fa un diff a livello di top-level field
    tra il documento originale e quello nuovo, poi `$set` sui field
    cambiati — `properties` e' UN campo atomico, non merge chiave-per-
    chiave. Un save che tocca solo altre chiavi di properties (es. un
    editor "report"/rheader/rfooter che ricostruisce properties da zero
    con solo quelle chiavi) sovrascriverebbe l'intero sotto-documento,
    cancellando silenziosamente una config ACL impostata in precedenza
    (bug reale osservato su 'user': models_restricted_fields sparita dopo
    un save successivo non correlato). Vedi anche la memoria di progetto
    "ozon-env upsert partial wipe" — stesso pattern, qui applicato a
    `properties` invece che al record intero.
    """
    if not existing_component:
        return
    existing_properties = getattr(existing_component, "properties", None)
    if existing_properties is None and isinstance(existing_component, dict):
        existing_properties = existing_component.get("properties")
    if isinstance(existing_properties, str):
        try:
            existing_properties = json.loads(existing_properties)
        except Exception:
            existing_properties = None
    if not isinstance(existing_properties, dict):
        return

    properties = schema.get("properties")
    if properties is None:
        properties = {}
        schema["properties"] = properties
    if not isinstance(properties, dict):
        return

    for key in _ACL_PROPERTY_KEYS:
        if key not in properties and key in existing_properties:
            logger.info(
                "preserve_acl_properties: ripristino '%s' su component rec_name=%s"
                " (assente nel payload di save, presente nell'esistente)",
                key,
                schema.get("rec_name"),
            )
            properties[key] = existing_properties[key]


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
        if not virtual and self._is_app_static_model(model_name):
            logger.info(
                "skip runtime add_model for static component model=%s",
                model_name,
            )
            return
        return await super().add_model(
            model_name,
            virtual=virtual,
            data_model=data_model,
        )

    def _is_app_static_model(self, model_name: str) -> bool:
        """True solo per i model REGISTRATI ESPLICITAMENTE come statici da
        `_STATIC_MODELS` (app/deps/app_env.py), NON per qualunque nome
        presente in `orm_static_models_map` — quest'ultimo include anche
        model che dovrebbero restare dynamic ma hanno semplicemente un
        .py cache in models_folder (auto-importato da init_models()).
        Usare `orm_static_models_map` qui bloccherebbe per sempre la
        rigenerazione/regen di quei model dynamic (bug reale osservato:
        model_fields_rule/model_groups_rule non si aggiornavano mai dal
        component, pur non essendo in _STATIC_MODELS)."""
        return model_name in getattr(self, "app_static_model_names", set())

    async def update_model(self, schema, component):
        model_name = str(schema.get("rec_name", "") or "").strip()
        if not is_runtime_model_name(model_name):
            logger.info(
                "skip runtime update_model for non-runtime component model=%s",
                model_name,
            )
            return
        if self._is_app_static_model(model_name):
            # I model statici (FieldAclPolicy, MailTemplate, ...) sono registrati come
            # classi Pydantic dedicate in app/core/models.py. La base
            # `OzonOrm.update_model` rigenera il model dallo schema JSON del
            # component (via ModelMaker) e rimpiazza la registrazione
            # statica con una dinamica derivata dai tipi del form builder
            # (es. textarea -> str), rompendo i campi tipizzati (list/dict)
            # quando si salva/ri-salva lo schema del component da editor.
            # Va saltato: lo schema del component resta aggiornato (gia'
            # salvato sopra da insert_update_component), ma il model
            # registrato in ORM resta la classe statica.
            logger.info(
                "skip runtime update_model for static component model=%s",
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
        c_model = self.get("component")
        model_name = str(schema.get("rec_name", "") or "").strip()
        component = await c_model.load({"rec_name": model_name})
        # Va prima del normalize: se il payload non porta models_groups/
        # models_restricted_fields, ripristina quelle esistenti PRIMA che
        # normalize_component_properties possa iniettarci un default
        # fresco (setdefault non toccherebbe piu' nulla a quel punto).
        preserve_acl_properties_on_partial_save(schema, component)
        normalize_component_properties(schema)
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
        try:
            from app.ozon_env_acl.model_rules_sync import sync_model_rules

            await sync_model_rules(self, schema)
        except Exception:
            logger.exception(
                "component rule sync failed rec_name=%s",
                model_name,
            )
        return res
