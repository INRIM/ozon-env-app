from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from fastapi import status

from app.core.models import FieldAclEffect
from app.core.models import FieldAclOperation

WILDCARD = "*"


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


def _delete_path(payload: Any, field_path: str) -> None:
    if not isinstance(payload, dict):
        return
    parts = [part for part in field_path.split(".") if part]
    current = payload
    for part in parts[:-1]:
        current = current.get(part)
        if not isinstance(current, dict):
            return
    if parts:
        current.pop(parts[-1], None)


def _obfuscate_path(payload: Any, field_path: str) -> None:
    if not isinstance(payload, dict):
        return
    parts = [part for part in field_path.split(".") if part]
    current = payload
    for part in parts[:-1]:
        current = current.get(part)
        if not isinstance(current, dict):
            return
    if parts and parts[-1] in current:
        current[parts[-1]] = None


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
