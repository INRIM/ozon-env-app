from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger("uvicorn.error")

_GROUPS_RULE_COLLECTION = "model_groups_rule"
_FIELDS_RULE_COLLECTION = "model_fields_rule"


def _get_db_engine(env: Any) -> Any:
    for candidate in (
        getattr(getattr(env, "db", None), "engine", None),
        getattr(getattr(getattr(env, "orm", None), "db", None), "engine", None),
    ):
        if candidate is not None:
            return candidate
    raise RuntimeError("db engine not available")


def _normalize_group(value: Any) -> str:
    return str(value or "").strip().lower()


async def _validated_row(env: Any, collection_name: str, row: dict[str, Any]) -> dict[str, Any]:
    """Valida/normalizza `row` costruendo un record col model REGISTRATO in
    ORM (dynamic, derivato dal component reale — vedi ModelGroupsRule/
    ModelFieldsRule in app/core/models.py per il perche' NON si usano
    quelle classi qui: sarebbero una shape duplicata e potenzialmente
    disallineata da quella vera che l'ORM usa per leggere/scrivere questa
    stessa collection)."""
    model = env.get(collection_name)
    record = await model.new(data=row)
    return record.get_dict(exclude={"id"})


def _parse_dict_property(raw: Any) -> dict[str, Any] | None:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return None
        try:
            parsed = json.loads(text)
        except Exception:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


async def model_groups_rows(
    env: Any, app_code: str, model_name: str, properties: dict[str, Any]
) -> list[dict[str, Any]]:
    """Flatten component.properties.models_groups (formato {"rules": [...]})
    in righe model_groups_rule, una per (app_code, model, group).

    Il formato legacy (list/CSV di nomi gruppo) NON e' un dict: nessun
    ramo lo gestisce piu' (retirato insieme a
    synth_policies_from_component_properties, mai popolato dai default
    correnti) — un component con quel formato NON produce righe qui,
    quindi il model resta senza righe model_groups_rule per i non-admin
    (fail-closed, vedi Service._get_model_group_access).
    """
    raw = _parse_dict_property((properties or {}).get("models_groups"))
    if raw is None:
        return []

    merged: dict[str, dict[str, bool]] = {}
    for rule in raw.get("rules") or []:
        if not isinstance(rule, dict):
            continue
        actions = rule.get("actions") or {}
        groups = rule.get("groups") or []
        if not isinstance(groups, (list, tuple, set)):
            groups = [groups]
        for group in groups:
            group_name = _normalize_group(group)
            if not group_name:
                continue
            bucket = merged.setdefault(
                group_name,
                {"read": False, "create": False, "update": False, "delete": False, "export": False},
            )
            for op in ("read", "create", "update", "delete", "export"):
                bucket[op] = bucket[op] or bool(actions.get(op))

    rows: list[dict[str, Any]] = []
    for group_name, ops in sorted(merged.items()):
        row = {
            "rec_name": f"mgr.{app_code}.{model_name}.{group_name}",
            "app_code": app_code,
            "model": model_name,
            "group": group_name,
            "active": True,
            "deleted": 0,
            "sys": True,
            **ops,
        }
        rows.append(await _validated_row(env, _GROUPS_RULE_COLLECTION, row))
    return rows


async def model_fields_rows(
    env: Any, app_code: str, model_name: str, properties: dict[str, Any]
) -> list[dict[str, Any]]:
    """Flatten component.properties.models_restricted_fields in righe
    model_fields_rule — kind "fields" (fields_rule.allowed_groups, una riga
    per group) e kind "record" (record_rulse, una riga per indice).

    Chiavi input con typo (resticted_fields, record_rulse) parsate as-is: e'
    il formato scritto da normalize_component_properties/i default seed.
    Il formato legacy {field_path: [groups]} manca di entrambe le chiavi
    "fields_rule"/"record_rulse": nessun ramo lo gestisce piu' (retirato
    insieme a synth_policies_from_component_properties, mai popolato dai
    default correnti) — un component con quel formato non produce righe
    qui.

    `filters` e' scritto come stringa JSON (campo testo + json editor nel
    form model_fields_rule, coerente con queryformeditable/altri campi
    JSON-in-textarea dell'app) — chi legge la riga (Service._get_record_rulse)
    fa un json.loads difensivo, non un dict tipizzato via ORM.
    """
    raw = _parse_dict_property((properties or {}).get("models_restricted_fields"))
    if raw is None or not ({"fields_rule", "record_rulse"} & raw.keys()):
        return []

    rows: list[dict[str, Any]] = []

    fields_rule = raw.get("fields_rule") or {}
    if isinstance(fields_rule, dict):
        restricted_fields = list(fields_rule.get("resticted_fields") or [])
        merged: dict[str, dict[str, bool]] = {}
        for entry in fields_rule.get("allowed_groups") or []:
            if not isinstance(entry, dict):
                continue
            actions = entry.get("actions") or {}
            groups = entry.get("groups") or []
            if not isinstance(groups, (list, tuple, set)):
                groups = [groups]
            for group in groups:
                group_name = _normalize_group(group)
                if not group_name:
                    continue
                bucket = merged.setdefault(
                    group_name,
                    {"read": False, "create": False, "update": False, "delete": False},
                )
                for op in ("read", "create", "update", "delete"):
                    bucket[op] = bucket[op] or bool(actions.get(op))

        for group_name, ops in sorted(merged.items()):
            row = {
                "rec_name": f"mfr.{app_code}.{model_name}.fields.{group_name}",
                "app_code": app_code,
                "model": model_name,
                "rule_type": "fields",
                "group": group_name,
                "restricted_fields": restricted_fields,
                "filters": "{}",
                "active": True,
                "deleted": 0,
                "sys": True,
                **ops,
            }
            rows.append(await _validated_row(env, _FIELDS_RULE_COLLECTION, row))

    record_rulse = raw.get("record_rulse")
    if isinstance(record_rulse, list):
        for index, entry in enumerate(record_rulse):
            if not isinstance(entry, dict):
                continue
            actions = entry.get("actions") or {}
            row = {
                "rec_name": f"mfr.{app_code}.{model_name}.record.{index}",
                "app_code": app_code,
                "model": model_name,
                "rule_type": "record",
                "group": "",
                "restricted_fields": list(entry.get("resticted_fields") or []),
                "filters": json.dumps(entry.get("filters") or {}, ensure_ascii=False),
                "active": True,
                "deleted": 0,
                "sys": True,
                "read": bool(actions.get("read")),
                "create": bool(actions.get("create")),
                "update": bool(actions.get("update")),
                "delete": bool(actions.get("delete")),
            }
            rows.append(await _validated_row(env, _FIELDS_RULE_COLLECTION, row))

    return rows


async def sync_model_rules(env: Any, schema: dict[str, Any]) -> None:
    """Cancella e riscrive le righe model_groups_rule/model_fields_rule per
    (app_code, model) a partire da schema["properties"]. Fail-soft: un
    errore qui non deve mai rompere un save di component."""
    from app.core.OzonEnvApp import is_runtime_model_name

    model_name = str((schema or {}).get("rec_name") or "").strip()
    if not is_runtime_model_name(model_name):
        return

    try:
        app_code = str(getattr(env.orm.app_settings, "app_code", "") or "").strip()
        if not app_code:
            return
        properties = schema.get("properties")
        if not isinstance(properties, dict):
            properties = {}

        groups_rows = await model_groups_rows(env, app_code, model_name, properties)
        fields_rows = await model_fields_rows(env, app_code, model_name, properties)

        engine = _get_db_engine(env)

        groups_coll = engine.get_collection(_GROUPS_RULE_COLLECTION)
        await groups_coll.delete_many({"app_code": app_code, "model": model_name})
        if groups_rows:
            await groups_coll.insert_many(groups_rows)

        fields_coll = engine.get_collection(_FIELDS_RULE_COLLECTION)
        await fields_coll.delete_many({"app_code": app_code, "model": model_name})
        if fields_rows:
            await fields_coll.insert_many(fields_rows)
    except Exception:
        logger.exception(
            "sync_model_rules failed for model=%s — rule tables left stale",
            model_name,
        )


async def sync_all_model_rules(env: Any) -> None:
    """Pass di startup: riflatta le regole per ogni component installato,
    incluso quelli scritti raw dal PluginInstaller (mai passati da
    normalize_component_properties). Non riscrive il component stesso."""
    from app.core.OzonEnvApp import normalize_component_properties

    try:
        coll = _get_db_engine(env).get_collection("component")
    except Exception:
        logger.exception("sync_all_model_rules: component collection unavailable")
        return

    try:
        async for doc in coll.find({"deleted": 0}):
            schema = dict(doc)
            normalize_component_properties(schema)
            await sync_model_rules(env, schema)
    except Exception:
        logger.exception("sync_all_model_rules failed")
