from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from fastapi import status

from app.core.models import FieldAclEffect
from app.core.models import FieldAclOperation

logger = logging.getLogger("uvicorn.error")

WILDCARD = "*"
ADMIN_GROUP_NAME = "admin"


def _obj_to_dict(obj: Any) -> dict[str, Any]:
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj.copy()
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="python")
    if hasattr(obj, "dict"):
        return obj.dict()
    if hasattr(obj, "get_dict"):
        return obj.get_dict()
    return {}


def _field(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _as_set(value: Any) -> set[str]:
    if value in (None, "", [], (), {}):
        return set()
    if isinstance(value, str):
        return {item.strip().strip("/") for item in value.split(",") if item.strip()}
    if isinstance(value, (list, tuple, set)):
        return {str(item).strip().strip("/") for item in value if str(item).strip()}
    return {str(value).strip().strip("/")}


def _lower_set(value: Any) -> set[str]:
    return {item.lower() for item in _as_set(value)}


def _parse_mongo_rule(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value.copy()
    if not isinstance(value, str):
        return {}
    raw = value.strip()
    if not raw or raw == "{}":
        return {}
    try:
        parsed = json.loads(raw)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _session_actor(session: Any) -> dict[str, Any]:
    user = _field(session, "user", {}) or {}
    uid = str(_field(session, "uid", "") or user.get("uid") or "")
    role_values = _as_set(user.get("roles")) | _as_set(user.get("role"))
    role_values |= _as_set(user.get("user_role")) | _as_set(_field(session, "user_role", ""))
    group_values = _as_set(user.get("groups")) | _as_set(_field(session, "groups", ""))
    sector = (
        _field(session, "sector", "")
        or user.get("sector")
        or _field(session, "owner_sector", "")
        or user.get("owner_sector")
        or ""
    )
    sector_id = (
        _field(session, "sector_id", "")
        or user.get("sector_id")
        or _field(session, "owner_sector_id", "")
        or user.get("owner_sector_id")
        or ""
    )
    return {
        "uid": uid,
        "roles": _lower_set(role_values),
        "groups": _lower_set(group_values),
        "sector": str(sector).lower(),
        "sector_id": str(sector_id),
        "is_admin": bool(_field(session, "is_admin", False) or user.get("is_admin")),
    }


def apply_session_allowed_users(
    session: Any, admins: Any
) -> list[str]:
    """Popola `session.user.allowed_users` al login (sezione ACL row-level).

    - utente admin: di default gli admin (la lista degli uid admin);
    - utente non admin: i valori dalle ACL (catena responsabili gia' in
      `user.allowed_users`, es. da people_sync) piu' il proprio uid.

    Mutazione in-memory del dict annidato `user` (come il patch is_admin):
    request-scoped, non persiste.
    """
    user = _field(session, "user", None)
    if not isinstance(user, dict):
        return []
    admin_set = {str(a).strip() for a in (admins or []) if str(a).strip()}
    is_admin = bool(_field(session, "is_admin", False) or user.get("is_admin"))
    uid = str(_field(session, "uid", "") or user.get("uid") or "").strip()
    if is_admin:
        allowed = sorted(admin_set)
    else:
        acl_allowed = _as_set(user.get("allowed_users"))
        if uid:
            acl_allowed.add(uid)
        allowed = sorted(acl_allowed)
    user["allowed_users"] = allowed
    return allowed


async def _expand_implied_groups(env: Any, groups: set[str]) -> set[str]:
    if not groups:
        return set()
    try:
        model = env.get("groups")
    except Exception:
        return set(groups)
    if model is None:
        return set(groups)
    query: dict[str, Any] = {"active": True, "deleted": 0}
    try:
        domain = model.get_domain(query)
    except Exception:
        domain = query
    try:
        records = await model.find(domain=domain, limit=0)
    except Exception:
        return set(groups)

    implications: dict[str, set[str]] = {}
    for record in records:
        data = _obj_to_dict(record)
        group_name = str(data.get("rec_name") or "").strip()
        if not group_name:
            continue
        implications[group_name] = _as_set(data.get("implied_groups"))

    expanded = set(groups)
    pending = list(groups)
    while pending:
        group_name = pending.pop()
        for implied_group in implications.get(group_name, set()):
            if implied_group not in expanded:
                expanded.add(implied_group)
                pending.append(implied_group)
    return expanded


async def _groups_from_rules(env: Any, uid: str) -> set[str]:
    try:
        groups_model = env.get("groups")
        user_model = env.get("user")
    except Exception:
        return set()
    if groups_model is None or user_model is None:
        return set()
    groups_query: dict[str, Any] = {"active": True, "deleted": 0}
    try:
        groups_domain = groups_model.get_domain(groups_query)
    except Exception:
        groups_domain = groups_query
    try:
        group_records = await groups_model.find(domain=groups_domain, limit=0)
    except Exception:
        return set()

    matched: set[str] = set()
    for record in group_records:
        data = _obj_to_dict(record)
        group_name = str(data.get("rec_name") or "").strip()
        rule = _parse_mongo_rule(data.get("rule"))
        if not group_name or not rule:
            continue
        user_query = {
            "$and": [
                {"active": True},
                {"deleted": 0},
                {"$or": [{"rec_name": uid}, {"uid": uid}]},
                rule,
            ]
        }
        try:
            user_domain = user_model.get_domain(user_query)
        except Exception:
            user_domain = user_query
        try:
            users = await user_model.find(domain=user_domain, limit=1)
        except Exception:
            users = []
        if users:
            matched.add(group_name)
    return matched


async def apply_session_groups(env: Any, session: Any) -> list[str]:
    """Popola session.user.groups e session.groups da group_users
    (request-scoped, non persiste).

    Sovrascrive sempre entrambi i campi, anche a vuoto: ozon-env.session_app()
    puo' aver propagato groups dal JWT keycloak sul record persistito quando
    l'utente e' nuovo (OzonOrm.build_auth_user usa claims.get("groups") come
    fallback) — group_users resta l'unica fonte effettiva ad ogni request.
    """
    user = _field(session, "user", None)
    if not isinstance(user, dict):
        return []
    uid = str(_field(session, "uid", "") or user.get("uid") or "").strip()
    app_code = str(
        _field(session, "app_code", "") or user.get("app_code") or ""
    ).strip()
    groups: set[str] = set()
    if uid and app_code:
        try:
            model = env.get("group_users")
        except Exception:
            model = None
        if model is not None:
            query: dict[str, Any] = {
                "active": True,
                "deleted": 0,
                "app_code": app_code,
            }
            try:
                domain = model.get_domain(query)
            except Exception:
                domain = query
            try:
                records = await model.find(domain=domain, limit=0)
            except Exception:
                records = []
            for record in records:
                data = _obj_to_dict(record)
                record_app_code = str(data.get("app_code") or "").strip()
                if record_app_code != app_code:
                    continue
                members = _as_set(data.get("users"))
                if uid in members:
                    group_name = str(data.get("group") or "").strip()
                    if group_name:
                        groups.add(group_name)
        groups |= await _groups_from_rules(env, uid)
        groups = await _expand_implied_groups(env, groups)
    sorted_groups = sorted(groups)
    user["groups"] = sorted_groups
    try:
        session.groups = sorted_groups
    except Exception:
        pass
    try:
        session.is_tech = "technical_operator" in sorted_groups
    except Exception:
        pass
    return sorted_groups


async def get_admin_uids(env: Any, app_code: str) -> list[str]:
    """Uid del gruppo 'admin' in group_users per app_code.

    Sostituisce setting_app.admins come fonte di is_admin: keycloak resta
    responsabile della sola autenticazione, non dell'autorizzazione admin.
    """
    if not app_code:
        return []
    try:
        model = env.get("group_users")
    except Exception:
        return []
    if model is None:
        return []
    query: dict[str, Any] = {"active": True, "deleted": 0, "app_code": app_code}
    try:
        domain = model.get_domain(query)
    except Exception:
        domain = query
    try:
        records = await model.find(domain=domain, limit=0)
    except Exception:
        return []
    admin_uids: set[str] = set()
    for record in records:
        data = _obj_to_dict(record)
        if str(data.get("app_code") or "").strip() != app_code:
            continue
        if str(data.get("group") or "").strip().lower() != ADMIN_GROUP_NAME:
            continue
        admin_uids |= _as_set(data.get("users"))
    return sorted(admin_uids)


def _selector_matches(selector: Any, actor: dict[str, Any]) -> bool:
    if selector in (None, "", {}, [], WILDCARD):
        return True
    if isinstance(selector, str):
        raw = selector.strip()
        if not raw or raw == WILDCARD:
            return True
        key, sep, value = raw.partition(":")
        if sep:
            key = key.strip().lower()
            values = _lower_set(value)
            if key in {"uid", "user"}:
                return actor["uid"].lower() in values
            if key in {"role", "roles"}:
                return bool(actor["roles"] & values)
            if key in {"group", "groups"}:
                return bool(actor["groups"] & values)
            if key in {"sector", "settore"}:
                return actor["sector"] in values
        raw_lower = raw.lower().strip("/")
        return (
            raw_lower == actor["uid"].lower()
            or raw_lower in actor["roles"]
            or raw_lower in actor["groups"]
            or raw_lower == actor["sector"]
        )
    if not isinstance(selector, dict):
        return False

    user_values = _lower_set(selector.get("uid") or selector.get("user"))
    user_values |= _lower_set(selector.get("users"))
    if user_values and actor["uid"].lower() not in user_values:
        return False

    role_values = _lower_set(selector.get("role") or selector.get("roles"))
    if role_values and not actor["roles"] & role_values:
        return False

    group_values = _lower_set(selector.get("group") or selector.get("groups"))
    if group_values and not actor["groups"] & group_values:
        return False

    exclude_group_values = _lower_set(selector.get("exclude_groups"))
    if exclude_group_values and actor["groups"] & exclude_group_values:
        return False

    sector_values = _lower_set(selector.get("sector") or selector.get("sectors"))
    if sector_values and actor["sector"] not in sector_values:
        return False

    sector_ids = _as_set(selector.get("sector_id") or selector.get("sector_ids"))
    if sector_ids and actor["sector_id"] not in sector_ids:
        return False

    if "is_admin" in selector and bool(selector["is_admin"]) != actor["is_admin"]:
        return False
    return True


def _path_matches(policy_path: str, field_path: str) -> bool:
    policy_path = str(policy_path or WILDCARD)
    if policy_path == WILDCARD:
        return True
    if policy_path.endswith(".*"):
        prefix = policy_path[:-2]
        return field_path == prefix or field_path.startswith(f"{prefix}.")
    return policy_path == field_path


def iter_payload_paths(payload: Any, prefix: str = "") -> set[str]:
    if not isinstance(payload, dict):
        return set()
    paths: set[str] = set()
    for key, value in payload.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        paths.add(path)
        if isinstance(value, dict):
            paths |= iter_payload_paths(value, path)
    return paths


def _is_traversable(value: Any) -> bool:
    return isinstance(value, dict) or hasattr(value, "__dict__") or hasattr(
        value, "model_fields"
    )


def _path_get_child(current: Any, part: str) -> Any:
    if isinstance(current, dict):
        return current.get(part)
    return getattr(current, part, None)


def _path_has_key(current: Any, part: str) -> bool:
    if isinstance(current, dict):
        return part in current
    return hasattr(current, part)


def _path_set(current: Any, part: str, value: Any) -> None:
    """Best-effort: dict[part]=value oppure setattr(current, part, value)
    (CoreModel/pydantic instance) — apply_read/find/by_name su model reali
    ritornano istanze pydantic, non dict, quindi l'oscuramento deve
    funzionare su entrambi (bug reale osservato: senza questo, obfuscate/
    restore su dati reali era un no-op silenzioso, la baseline "mascherata"
    non veniva mai davvero applicata)."""
    if isinstance(current, dict):
        current[part] = value
        return
    try:
        setattr(current, part, value)
    except Exception:
        logger.warning(
            "path_set: impossibile impostare '%s' su %s (validazione pydantic?)",
            part,
            type(current),
        )


def _path_del(current: Any, part: str) -> None:
    if isinstance(current, dict):
        current.pop(part, None)
        return
    _path_set(current, part, None)


def _clear_record(item: Any) -> None:
    """WILDCARD DENY: azzera l'intero record. `dict.clear()` per i dict;
    per istanze pydantic/CoreModel (reali, da find()/by_name() — non
    dict) non esiste `.clear()`, quindi era un no-op silenzioso: il
    record DENY-whole-model restava completamente visibile sui dati
    reali. Fallback: None su ogni field noto del model."""
    if isinstance(item, dict):
        item.clear()
        return
    field_names = getattr(type(item), "model_fields", None)
    if field_names is None:
        field_names = vars(item).keys()
    for field_name in list(field_names):
        _path_set(item, field_name, None)


def _delete_path(payload: Any, field_path: str) -> None:
    if not _is_traversable(payload):
        return
    parts = [part for part in field_path.split(".") if part]
    current = payload
    for part in parts[:-1]:
        current = _path_get_child(current, part)
        if not _is_traversable(current):
            return
    if parts:
        _path_del(current, parts[-1])


def _obfuscate_path(payload: Any, field_path: str) -> None:
    if not _is_traversable(payload):
        return
    parts = [part for part in field_path.split(".") if part]
    current = payload
    for part in parts[:-1]:
        current = _path_get_child(current, part)
        if not _is_traversable(current):
            return
    if parts and _path_has_key(current, parts[-1]):
        _path_set(current, parts[-1], None)


def restore_path(payload: Any, field_path: str, original: Any) -> None:
    """Inverso di `_obfuscate_path`: ripristina il valore originale su un
    campo (usato quando `record_rulse` sblocca un campo altrimenti
    oscurato dalla policy di gruppo, es. record di proprieta' dell'utente)."""
    if not _is_traversable(payload):
        return
    parts = [part for part in field_path.split(".") if part]
    current = payload
    for part in parts[:-1]:
        current = _path_get_child(current, part)
        if not _is_traversable(current):
            return
    if parts:
        _path_set(current, parts[-1], original)


def obfuscate_fields_in_place(payload: dict[str, Any], fields: list[str]) -> None:
    """Applica `_obfuscate_path` per ciascun campo — usato dai path che
    saltano l'oscuramento server-side (aggregate obfuscate_fields) per
    poter valutare `record_rulse` sui valori reali, e devono quindi
    ri-applicare loro la baseline in Python (es. stream NDJSON)."""
    for field_path in fields:
        _obfuscate_path(payload, field_path)


def _copy_data(data: Any) -> Any:
    if isinstance(data, list):
        return [_copy_data(item) for item in data]
    if isinstance(data, dict):
        return {key: _copy_data(value) for key, value in data.items()}
    if hasattr(data, "model_copy"):
        return data.model_copy(deep=True)
    if hasattr(data, "copy"):
        try:
            return data.copy()
        except TypeError:
            return data
    return data


@dataclass(frozen=True)
class CompiledFieldAclPolicy:
    model_key: str
    field_path: str
    operation: str
    effect: str
    app_key: str = ""
    form_key: str = ""
    workflow_stage: str = ""
    task_key: str = ""
    priority: int = 100


class CompiledFieldAcl:
    def __init__(
        self,
        policies: list[CompiledFieldAclPolicy] | None = None,
    ) -> None:
        self.policies = sorted(
            policies or [],
            key=lambda policy: policy.priority,
        )

    def for_operation(
        self,
        *,
        operation: str,
        model_key: str,
        app_key: str = "",
        form_key: str = "",
        workflow_stage: str = "",
        task_key: str = "",
    ) -> list[CompiledFieldAclPolicy]:
        return [
            policy
            for policy in self.policies
            if policy.operation == operation
            and self._context_matches(
                policy=policy,
                model_key=model_key,
                app_key=app_key,
                form_key=form_key,
                workflow_stage=workflow_stage,
                task_key=task_key,
            )
        ]

    def denied_fields(
        self,
        *,
        operation: str,
        model_key: str,
        field_paths: set[str],
        app_key: str = "",
        form_key: str = "",
        workflow_stage: str = "",
        task_key: str = "",
    ) -> list[str]:
        policies = self.for_operation(
            operation=operation,
            model_key=model_key,
            app_key=app_key,
            form_key=form_key,
            workflow_stage=workflow_stage,
            task_key=task_key,
        )
        allow_policies = [
            policy for policy in policies if policy.effect == FieldAclEffect.ALLOW.value
        ]
        denied: list[str] = []
        for field_path in sorted(field_paths):
            matching = [p for p in policies if _path_matches(p.field_path, field_path)]
            if any(policy.effect == FieldAclEffect.DENY.value for policy in matching):
                denied.append(field_path)
                continue
            if allow_policies and not any(
                policy.effect == FieldAclEffect.ALLOW.value for policy in matching
            ):
                denied.append(field_path)
        return denied

    def read_masks(
        self,
        *,
        model_key: str,
        app_key: str = "",
        form_key: str = "",
        workflow_stage: str = "",
        task_key: str = "",
    ) -> tuple[list[str], list[str]]:
        policies = self.for_operation(
            operation=FieldAclOperation.READ.value,
            model_key=model_key,
            app_key=app_key,
            form_key=form_key,
            workflow_stage=workflow_stage,
            task_key=task_key,
        )
        denied = sorted(
            {
                policy.field_path
                for policy in policies
                if policy.effect == FieldAclEffect.DENY.value
            }
        )
        obfuscated = sorted(
            {
                policy.field_path
                for policy in policies
                if policy.effect == FieldAclEffect.OBFUSCATE.value
            }
        )
        return denied, obfuscated

    def apply_read(
        self,
        *,
        model_key: str,
        data: Any,
        app_key: str = "",
        form_key: str = "",
        workflow_stage: str = "",
        task_key: str = "",
    ) -> tuple[Any, list[str]]:
        denied, obfuscated = self.read_masks(
            model_key=model_key,
            app_key=app_key,
            form_key=form_key,
            workflow_stage=workflow_stage,
            task_key=task_key,
        )
        cloned = _copy_data(data)
        items = cloned if isinstance(cloned, list) else [cloned]
        for item in items:
            for field_path in denied:
                # "*" nega l'intero record (no campo letterale da rimuovere)
                if field_path == WILDCARD:
                    _clear_record(item)
                    continue
                _delete_path(item, field_path)
            for field_path in obfuscated:
                _obfuscate_path(item, field_path)
        return cloned, obfuscated

    def _context_matches(
        self,
        *,
        policy: CompiledFieldAclPolicy,
        model_key: str,
        app_key: str,
        form_key: str,
        workflow_stage: str,
        task_key: str,
    ) -> bool:
        if policy.model_key not in {"", WILDCARD, model_key}:
            return False
        if policy.app_key and policy.app_key not in {WILDCARD, app_key}:
            return False
        if policy.form_key and policy.form_key not in {WILDCARD, form_key}:
            return False
        if policy.workflow_stage and policy.workflow_stage not in {
            WILDCARD,
            workflow_stage,
        }:
            return False
        if policy.task_key and policy.task_key not in {WILDCARD, task_key}:
            return False
        return True


def _model_groups_to_policies(
    model_key: str,
    field_path: str,
    allowed_groups: list[str],
) -> list[dict[str, Any]]:
    if not allowed_groups:
        return []
    return [
        {
            "model_key": model_key,
            "field_path": field_path,
            "operation": operation,
            "effect": FieldAclEffect.DENY.value,
            "actor_selector": {
                "exclude_groups": allowed_groups,
                "is_admin": False,
            },
            "priority": 10,
        }
        for operation in (
            FieldAclOperation.READ.value,
            FieldAclOperation.INSERT.value,
            FieldAclOperation.UPDATE.value,
        )
    ]


def _filter_value_matches(actual: Any, expected: Any) -> bool:
    if isinstance(expected, dict):
        if "$eq" in expected:
            return actual == expected["$eq"]
        if "$ne" in expected:
            return actual != expected["$ne"]
        if "$in" in expected:
            values = expected["$in"]
            if not isinstance(values, (list, tuple, set)):
                return False
            # Semantica Mongo: $in matcha sia "actual e' uno dei valori" sia
            # (se actual e' esso stesso un array, es. record["groups"]) "actual
            # ha almeno un elemento in comune con values".
            if isinstance(actual, (list, tuple, set)):
                return bool(set(actual) & set(values))
            return actual in values
        if "$nin" in expected:
            values = expected["$nin"]
            if not isinstance(values, (list, tuple, set)):
                return True
            if isinstance(actual, (list, tuple, set)):
                return not (set(actual) & set(values))
            return actual not in values
        # operatore non supportato: nessun match invece di falso positivo.
        return False
    return actual == expected


def _record_matches_filters(record: dict[str, Any], filters: dict[str, Any]) -> bool:
    """Match (non query) di un filtro mongo-shaped contro UN record gia'
    caricato. Fail-closed di proposito: qualunque forma non riconosciuta
    (operatore top-level diverso da $and/$or, valore non gestito da
    `_filter_value_matches`) fa fallire il match invece di farlo passare
    a vuoto — un falso "match" qui rivela un campo altrimenti oscurato a
    chiunque, un falso "non match" lascia solo la baseline in vigore
    (nessuna fuga di dati, solo eccesso di prudenza)."""
    if not isinstance(filters, dict) or not filters:
        return False
    for field_path, expected in filters.items():
        if field_path == "$and":
            clauses = expected if isinstance(expected, list) else None
            if not clauses or not all(
                _record_matches_filters(record, clause) for clause in clauses
            ):
                return False
            continue
        if field_path == "$or":
            clauses = expected if isinstance(expected, list) else None
            if not clauses or not any(
                _record_matches_filters(record, clause) for clause in clauses
            ):
                return False
            continue
        if str(field_path).startswith("$"):
            # operatore top-level non supportato: fail-closed.
            return False
        actual = record.get(field_path) if isinstance(record, dict) else None
        if not _filter_value_matches(actual, expected):
            return False
    return True


def evaluate_record_rule_override(
    record_rulse: list[dict[str, Any]],
    *,
    record: dict[str, Any],
    resolve_var: Any,
) -> list[str] | None:
    """Valuta `record_rulse` contro UN record gia' caricato (post-var-resolve).

    `resolve_var` e' un callable `(query_dict) -> query_dict` che risolve i
    nodi json-logic `{"var": "user.uid"}` presenti in `filters` (iniettato
    dal chiamante per evitare un import circolare verso app.services.service,
    che gia' importa questo modulo).

    Se UNA regola matcha il record, ritorna il suo `restricted_fields`:
    NON e' la lista finale di campi oscurati, e' lo SCOPE di campi che
    QUESTA regola sblocca (tipicamente `[]` = sblocca tutto quello che la
    baseline oscura). Il chiamante (`apply_record_rule_override`) fa la
    differenza insiemistica con la baseline — un campo fuori scope
    (es. "token" quando la regola copre solo "codicefiscale") resta
    oscurato anche a match, non viene rivelato per errore.

    Se NESSUNA regola matcha, ritorna None (il chiamante mantiene
    l'oscuramento di baseline da fields_rule per intero).
    """
    if not record_rulse or not isinstance(record, dict):
        return None
    rec_name = record.get("rec_name")
    for index, rule in enumerate(record_rulse):
        raw_filters = rule.get("filters") or {}
        resolved_filters = resolve_var(raw_filters) if raw_filters else {}
        if not isinstance(resolved_filters, dict) or not resolved_filters:
            logger.debug(
                "acl.record_rule rec_name=%s rule=%s raw_filters=%s -> skip (filtro vuoto/non risolto)",
                rec_name,
                index,
                raw_filters,
            )
            continue
        matched = _record_matches_filters(record, resolved_filters)
        logger.debug(
            "acl.record_rule rec_name=%s rule=%s resolved_filters=%s matched=%s",
            rec_name,
            index,
            resolved_filters,
            matched,
        )
        if matched:
            raw_restricted = rule.get("restricted_fields")
            if raw_restricted is None:
                raw_restricted = rule.get("resticted_fields")
            reveal_scope = [
                str(f).strip() for f in (raw_restricted or []) if str(f).strip()
            ]
            logger.debug(
                "acl.record_rule rec_name=%s rule=%s MATCH -> sblocca (dalla baseline) %s",
                rec_name,
                index,
                reveal_scope or "[tutto]",
            )
            return reveal_scope
    logger.debug("acl.record_rule rec_name=%s nessuna regola matcha", rec_name)
    return None


_RECORD_ACTION_KEYS: tuple[str, ...] = ("read", "create", "update", "delete")
_FULL_RECORD_ACCESS: dict[str, bool] = {key: True for key in _RECORD_ACTION_KEYS}
_NO_RECORD_ACCESS: dict[str, bool] = {key: False for key in _RECORD_ACTION_KEYS}


def evaluate_record_rule_access(
    record_rulse: list[dict[str, Any]],
    *,
    record: dict[str, Any],
    resolve_var: Any,
) -> dict[str, bool] | None:
    """Valuta `record_rulse` (rule_type="record") contro UN record e ritorna
    le azioni concesse (read/create/update/delete) dalla PRIMA regola che
    matcha, o None se nessuna regola matcha.

    Stessa matching logic (filtri risolti via resolve_var + _record_matches_
    filters) di evaluate_record_rule_override, ma legge il campo azioni della
    riga invece di restricted_fields — vista diversa della stessa riga
    model_fields_rule. None (nessun match) e' fail-closed: il chiamante
    (record_rule_access) nega tutto, non concede la baseline."""
    if not record_rulse or not isinstance(record, dict):
        return None
    rec_name = record.get("rec_name")
    for index, rule in enumerate(record_rulse):
        raw_filters = rule.get("filters") or {}
        resolved_filters = resolve_var(raw_filters) if raw_filters else {}
        if not isinstance(resolved_filters, dict) or not resolved_filters:
            continue
        if _record_matches_filters(record, resolved_filters):
            actions = {
                key: bool(rule.get(key, False)) for key in _RECORD_ACTION_KEYS
            }
            logger.debug(
                "acl.record_rule_access rec_name=%s rule=%s MATCH -> %s",
                rec_name,
                index,
                actions,
            )
            return actions
    logger.debug(
        "acl.record_rule_access rec_name=%s nessuna regola matcha", rec_name
    )
    return None


def record_rule_access(
    *,
    record_rulse: list[dict[str, Any]],
    record: dict[str, Any],
    resolve_var: Any,
    is_admin: bool,
) -> dict[str, bool]:
    """Azioni concesse (read/create/update/delete) su UN record gia' caricato,
    secondo record_rulse — apertura/accesso a documenti non di proprieta'.

    Fail-closed: se il model ha record_rulse configurato e nessuna regola
    matcha il record (non e' il tuo record, non sei nel gruppo coperto),
    nega tutto — niente fallback alla baseline (a differenza del field-
    masking, qui "nessun match" e' proprio negazione di accesso al record).
    Se il model non ha record_rulse, resta senza restrizioni. Admin bypassa
    sempre (coerente col bypass legacy di models_groups/models_restricted_
    fields — diverso dal fields_rule GDPR-style, che non lo concede)."""
    if is_admin or not record_rulse:
        return dict(_FULL_RECORD_ACCESS)
    granted = evaluate_record_rule_access(
        record_rulse, record=record, resolve_var=resolve_var
    )
    if granted is None:
        return dict(_NO_RECORD_ACCESS)
    return granted


def record_rule_read_domain(
    record_rulse: list[dict[str, Any]],
    *,
    resolve_var: Any,
) -> dict[str, Any]:
    """Domain mongo che restringe una query alle sole righe leggibili secondo
    record_rulse, per un attore NON admin — OR dei filtri risolti (gia'
    scoped sull'utente corrente via resolve_var) di ogni regola con
    read=True. Se nessuna regola concede read, ritorna un domain che non
    matcha nulla (fail-closed: un OR vuoto in mongo matcherebbe tutto, qui
    deve invece nascondere tutto)."""
    clauses: list[dict[str, Any]] = []
    for rule in record_rulse:
        if not rule.get("read", False):
            continue
        raw_filters = rule.get("filters") or {}
        resolved = resolve_var(raw_filters) if raw_filters else {}
        if isinstance(resolved, dict) and resolved:
            clauses.append(resolved)
    if not clauses:
        return {"rec_name": {"$in": []}}
    return {"$or": clauses}


def _read_path(payload: Any, field_path: str) -> Any:
    if not _is_traversable(payload):
        return None
    parts = [part for part in field_path.split(".") if part]
    current = payload
    for part in parts[:-1]:
        current = _path_get_child(current, part)
        if not _is_traversable(current):
            return None
    if parts:
        return _path_get_child(current, parts[-1])
    return None


def apply_record_rule_override(
    *,
    original: dict[str, Any],
    obfuscated: dict[str, Any],
    baseline_obfuscated_fields: list[str],
    record_rulse: list[dict[str, Any]],
    resolve_var: Any,
) -> list[str]:
    """Applica `record_rulse` a UN item gia' passato da
    `CompiledFieldAcl.apply_read` (baseline `fields_rule`, per-actor).

    Muta `obfuscated` in place: se una regola matcha, `evaluate_record_
    rule_override` ritorna lo SCOPE di campi che quella regola sblocca —
    qui si fa la differenza insiemistica con la baseline (`baseline -
    scope`), NON una sostituzione: un campo oscurato dalla baseline ma
    FUORI dallo scope della regola (es. "token" quando la regola copre
    solo "codicefiscale") resta oscurato anche a match. Scope vuoto `[]`
    = sblocca tutto cio' che la baseline oscura. Se nessuna regola matcha,
    la baseline resta invariata.

    Ritorna la lista finale di campi oscurati per questo item.
    """
    if not record_rulse:
        return list(baseline_obfuscated_fields)
    reveal_scope = evaluate_record_rule_override(
        record_rulse, record=original, resolve_var=resolve_var
    )
    if reveal_scope is None:
        return list(baseline_obfuscated_fields)
    reveal_all = not reveal_scope
    reveal_set = set(reveal_scope)
    final_obfuscated: list[str] = []
    for field_path in baseline_obfuscated_fields:
        if reveal_all or field_path in reveal_set:
            restore_path(obfuscated, field_path, _read_path(original, field_path))
        else:
            final_obfuscated.append(field_path)
    return final_obfuscated


def synth_policies_from_component_properties(
    components: list[Any],
) -> list[dict[str, Any]]:
    """Deriva FieldAclPolicy da properties.models_groups/models_restricted_fields, admin esclusi."""
    synthesized: list[dict[str, Any]] = []
    for component in components or []:
        data = _obj_to_dict(component)
        if not data:
            continue
        model_key = str(data.get("rec_name") or "").strip()
        if not model_key:
            continue
        properties = data.get("properties")
        if isinstance(properties, str):
            import json

            try:
                properties = json.loads(properties)
            except Exception:
                properties = {}
        if not isinstance(properties, dict):
            continue

        # Formato legacy (list/CSV di nomi gruppo) -> policy sintetiche
        # DENY-by-exclusion. Il nuovo formato {"rules": [...]} (vedi
        # app.core.OzonEnvApp defaults) e' spec-data per il motore ACL non
        # ancora costruito: e' un dict, non list/str, quindi qui va ignorato
        # esplicitamente — altrimenti _as_set lo stringifica in un unico
        # gruppo-fantasma e nega tutto a tutti i non-admin.
        models_groups_raw = properties.get("models_groups")
        if not isinstance(models_groups_raw, dict):
            allowed_groups = sorted(_as_set(models_groups_raw))
            synthesized.extend(
                _model_groups_to_policies(model_key, WILDCARD, allowed_groups)
            )

        restricted_fields = properties.get("models_restricted_fields")
        if isinstance(restricted_fields, dict) and not (
            {"fields_rule", "record_rulse"} & restricted_fields.keys()
        ):
            for field_path, field_groups in restricted_fields.items():
                if not str(field_path or "").strip():
                    continue
                field_allowed = sorted(_as_set(field_groups))
                synthesized.extend(
                    _model_groups_to_policies(
                        model_key, str(field_path), field_allowed
                    )
                )
        # Formato nuovo {"fields_rule": ..., "record_rulse": [...]}: la
        # fonte di verita' per l'enforcement e' `model_fields_rule`
        # (collection flat, popolata al salva del component da
        # model_rules_sync.py — component.properties puo' anche non
        # persistere la config), non component.properties qui. Vedi
        # Service._load_model_fields_rule_policies /
        # Service._get_record_rulse.
    return synthesized


def compile_field_acl_policies(
    policies: list[Any],
    *,
    session: Any,
) -> CompiledFieldAcl:
    actor = _session_actor(session)
    compiled: list[CompiledFieldAclPolicy] = []
    for policy in policies:
        data = _obj_to_dict(policy)
        if not data:
            continue
        if data.get("active") is False or data.get("deleted") not in (None, 0, "0"):
            continue
        if not _selector_matches(data.get("actor_selector"), actor):
            continue
        operation = str(data.get("operation") or FieldAclOperation.READ.value)
        effect = str(data.get("effect") or FieldAclEffect.ALLOW.value)
        try:
            operation = FieldAclOperation(operation).value
            effect = FieldAclEffect(effect).value
        except ValueError:
            continue
        compiled.append(
            CompiledFieldAclPolicy(
                model_key=str(data.get("model_key") or data.get("model") or ""),
                field_path=str(data.get("field_path") or WILDCARD),
                operation=operation,
                effect=effect,
                app_key=str(data.get("app_key") or data.get("app_code") or ""),
                form_key=str(data.get("form_key") or ""),
                workflow_stage=str(data.get("workflow_stage") or ""),
                task_key=str(data.get("task_key") or ""),
                priority=int(data.get("priority") or 100),
            )
        )
    return CompiledFieldAcl(compiled)


async def audit_denied_fields(
    env: Any,
    *,
    session: Any,
    model_key: str,
    operation: str,
    denied_fields: list[str],
    payload: dict[str, Any] | None = None,
) -> None:
    if not denied_fields:
        return
    try:
        collection = env.db.engine.get_collection("field_acl_audit")
        await collection.insert_one(
            {
                "uid": _field(session, "uid", ""),
                "app_code": _field(session, "app_code", ""),
                "model_key": model_key,
                "operation": operation,
                "denied_fields": denied_fields,
                "payload_keys": sorted((payload or {}).keys()),
                "created_at": datetime.now(ZoneInfo("UTC")),
            }
        )
    except Exception:
        return


async def enforce_write_acl(
    acl: CompiledFieldAcl,
    env: Any,
    *,
    session: Any,
    model_key: str,
    operation: str,
    payload: dict[str, Any],
) -> None:
    denied_fields = acl.denied_fields(
        operation=operation,
        model_key=model_key,
        field_paths=iter_payload_paths(payload),
        app_key=str(_field(session, "app_code", "")),
    )
    if not denied_fields:
        return
    await audit_denied_fields(
        env,
        session=session,
        model_key=model_key,
        operation=operation,
        denied_fields=denied_fields,
        payload=payload,
    )
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={
            "message": "Field ACL denied",
            "model": model_key,
            "operation": operation,
            "fields": denied_fields,
        },
    )
