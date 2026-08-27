from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger("uvicorn.error")

_GROUPS_RULE_COLLECTION = "model_groups_rule"
_FIELDS_RULE_COLLECTION = "model_fields_rule"


class MalformedAclPropertyError(ValueError):
    """`component.properties.<key>` c'e' ma non ha la forma attesa.

    Distinta da "proprieta' assente": assente significa "usa i default"
    (li inietta `normalize_component_properties`), malformata significa
    che qualcuno ha scritto qualcosa di sbagliato — e in quel caso
    l'ultima cosa da fare e' interpretarla come "nessuna regola" e
    cancellare le righe esistenti.
    """

    def __init__(self, property_name: str, value: Any) -> None:
        self.property_name = property_name
        self.value = value
        super().__init__(
            f"'{property_name}' deve essere un oggetto JSON "
            f"(es. {{\"rules\": [...]}}), ricevuto "
            f"{type(value).__name__}: {str(value)[:120]}"
        )


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


def _parse_dict_property(
    raw: Any, property_name: str = "acl property"
) -> dict[str, Any] | None:
    """`None` = proprieta' assente (usa i default). Dict = valore valido.

    Qualunque altra cosa (list, numero, stringa non-JSON, JSON che non e'
    un oggetto) alza `MalformedAclPropertyError` invece di degradare a
    `None`: il ramo silenzioso faceva sembrare "nessuna regola" un
    valore semplicemente scritto male, e il chiamante cancellava le
    righe buone. Il caso piu' facile da innescare e' il json editor del
    form, che presenta il campo come array `[]` mentre qui serve un
    oggetto `{"rules": [...]}`.
    """
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return None
        try:
            parsed = json.loads(text)
        except Exception as exc:
            raise MalformedAclPropertyError(property_name, raw) from exc
        if not isinstance(parsed, dict):
            raise MalformedAclPropertyError(property_name, parsed)
        return parsed
    raise MalformedAclPropertyError(property_name, raw)


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
    raw = _parse_dict_property(
        (properties or {}).get("models_groups"), "models_groups"
    )
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
    """Flatten component.properties.models_restricted_fields.record_rules in
    righe model_fields_rule (rule_type="record"), una per indice (o una per
    gruppo, se l'entry ha "groups" — vedi sotto).

    `rule_type="fields"` (fields_rule.allowed_groups, field-masking per
    gruppo) e' RITIRATO: sostituito da Layer 3 in ozon-env — ACL a livello
    di CAMPO dichiarata su properties.f_rule/f_rule_cond dello schema field
    stesso, baked a codegen-time (vedi Model.get_field_rules()/
    get_field_rules_conditions(), letti direttamente da
    Service._load_model_fields_rule_policies/_get_field_rule_conditions,
    niente piu' sync verso una collection). Una chiave "fields_rule" ancora
    presente in config vecchia viene semplicemente ignorata qui (nessun
    ramo la legge piu').

    La chiave canonica ``record_rules`` e' scritta da
    normalize_component_properties e dai default seed.

    `filters` e' scritto come stringa JSON (campo testo + json editor nel
    form model_fields_rule, coerente con queryformeditable/altri campi
    JSON-in-textarea dell'app) — chi legge la riga (Service._get_record_rules)
    fa un json.loads difensivo, non un dict tipizzato via ORM.
    """
    raw = _parse_dict_property(
        (properties or {}).get("models_restricted_fields"),
        "models_restricted_fields",
    )
    if raw is None or "record_rules" not in raw:
        return []

    rows: list[dict[str, Any]] = []

    record_rules = raw.get("record_rules")
    if isinstance(record_rules, list):
        for index, entry in enumerate(record_rules):
            if not isinstance(entry, dict):
                continue
            actions = entry.get("actions") or {}
            filters_json = json.dumps(entry.get("filters") or {}, ensure_ascii=False)
            common = {
                "app_code": app_code,
                "model": model_name,
                "rule_type": "record",
                "filters": filters_json,
                "active": True,
                "deleted": 0,
                "sys": True,
                "read": bool(actions.get("read")),
                "create": bool(actions.get("create")),
                "update": bool(actions.get("update")),
                "delete": bool(actions.get("delete")),
                # `export` viaggia con la riga per non perdere pezzi di
                # config nel flatten, ma NON e' gate di Layer 2:
                # _RECORD_ACTION_KEYS e' (read, create, update, delete) e
                # evaluate_record_rule_access ignora la chiave. L'export
                # e' deciso a Layer 1 (model_groups_rule).
                "export": bool(actions.get("export")),
            }
            groups = entry.get("groups") or []
            if not isinstance(groups, (list, tuple, set)):
                groups = [groups]
            group_names = sorted({_normalize_group(g) for g in groups if _normalize_group(g)})
            if not group_names:
                # nessun "groups" in entry -> regola universale (group="",
                # comportamento storico: si applica a chiunque passi il
                # gate model-level, indipendentemente dal gruppo sessione).
                row = {
                    "rec_name": f"mfr.{app_code}.{model_name}.record.{index}",
                    "group": "",
                    **common,
                }
                rows.append(await _validated_row(env, _FIELDS_RULE_COLLECTION, row))
            else:
                # entry scoped a gruppi specifici -> una riga per gruppo,
                # stesso filters/actions/restricted_fields; valutata solo
                # per sessioni il cui user.groups intersect questo group
                # (vedi Service._get_record_rules).
                for group_name in group_names:
                    row = {
                        "rec_name": f"mfr.{app_code}.{model_name}.record.{index}.{group_name}",
                        "group": group_name,
                        **common,
                    }
                    rows.append(await _validated_row(env, _FIELDS_RULE_COLLECTION, row))

    return rows


async def _replace_rules(
    collection: Any,
    *,
    app_code: str,
    model_name: str,
    build_rows: Any,
    table: str,
    empty_means: str,
) -> None:
    """Sostituisce le righe di (app_code, model) — ma solo se ha davvero
    qualcosa con cui sostituirle.

    Due guardie, entrambe contro lo stesso incidente: un `delete_many`
    seguito da nessun insert. Le due tabelle rompono in direzioni
    opposte — azzerare `model_groups_rule` NEGA tutto ai non-admin,
    azzerare `model_fields_rule` TOGLIE il filtro per riga e allarga
    l'accesso — quindi in nessuno dei due casi va fatto per sbaglio.

    1. property malformata -> non si tocca niente (le righe correnti
       restano quelle buone);
    2. property valida ma che produce zero righe MENTRE ne esistono ->
       non si cancella: e' quasi sempre un errore di configurazione, non
       la volonta' di azzerare l'ACL.

    Per azzerare davvero le regole di un model: togliere la property
    (tornano i default) o scriverne una con le azioni tutte a `false`.
    """
    scope = {"app_code": app_code, "model": model_name}
    try:
        rows = await build_rows()
    except MalformedAclPropertyError as exc:
        logger.error(
            "sync_model_rules: %s malformata per model=%s app_code=%s — "
            "righe %s LASCIATE INVARIATE. %s",
            exc.property_name,
            model_name,
            app_code,
            table,
            exc,
        )
        return

    if not rows:
        existing = await collection.count_documents(scope)
        if existing:
            logger.error(
                "sync_model_rules: model=%s app_code=%s produce 0 righe ma "
                "%s ne ha %s — cancellazione ANNULLATA perche' %s. "
                "Correggere properties o azzerare esplicitamente.",
                model_name,
                app_code,
                table,
                existing,
                empty_means,
            )
            return

    await collection.delete_many(scope)
    if rows:
        await collection.insert_many(rows)


def _warn_mixed_record_rule_scopes(
    model_name: str, properties: dict[str, Any]
) -> None:
    """Segnala la coesistenza di record rule universali e group-scoped.

    Le azioni concesse sono l'UNIONE delle entry che matchano il record
    (`evaluate_record_rule_access`): una entry senza `groups` si applica a
    chiunque passi il gate model-level, quindi annacqua qualunque entry
    per gruppo piu' restrittiva sullo stesso model — il caso tipico e' la
    entry di default (`filters: {"active": true}`, tutte le azioni) lasciata
    in `properties` mentre si aggiungono le regole per gruppo.

    Solo warning: la semantica dell'union NON cambia, la config resta
    legittima (l'universale puo' essere una baseline voluta)."""
    try:
        raw = _parse_dict_property(
            (properties or {}).get("models_restricted_fields"),
            "models_restricted_fields",
        )
    except MalformedAclPropertyError:
        return
    entries = (raw or {}).get("record_rules")
    if not isinstance(entries, list):
        return
    universal: list[int] = []
    scoped: list[int] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        groups = entry.get("groups") or []
        if not isinstance(groups, (list, tuple, set)):
            groups = [groups]
        if any(_normalize_group(group) for group in groups):
            scoped.append(index)
        else:
            universal.append(index)
    if universal and scoped:
        logger.warning(
            "record_rules model=%s: entry universali %s (senza `groups`) "
            "coesistono con entry per gruppo %s — le azioni sono l'UNIONE, "
            "quindi le universali concedono anche a chi le entry per gruppo "
            "restringono. Se e' la entry di default, va rimossa.",
            model_name,
            universal,
            scoped,
        )


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

        engine = _get_db_engine(env)

        await _replace_rules(
            engine.get_collection(_GROUPS_RULE_COLLECTION),
            app_code=app_code,
            model_name=model_name,
            build_rows=lambda: model_groups_rows(
                env, app_code, model_name, properties
            ),
            table=_GROUPS_RULE_COLLECTION,
            empty_means=(
                "nessun gruppo avrebbe piu' accesso al model "
                "(model_group_access e' fail-closed): lockout dei non-admin"
            ),
        )
        _warn_mixed_record_rule_scopes(model_name, properties)
        await _replace_rules(
            engine.get_collection(_FIELDS_RULE_COLLECTION),
            app_code=app_code,
            model_name=model_name,
            build_rows=lambda: model_fields_rows(
                env, app_code, model_name, properties
            ),
            table=_FIELDS_RULE_COLLECTION,
            empty_means=(
                "sparirebbe il filtro record-level: gli utenti vedrebbero "
                "righe che oggi non vedono"
            ),
        )
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
