from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Any

from bson import Decimal128
from ozonenv.core.BaseModels import BasicModel
from ozonenv.core.BaseModels import CoreModel, User
from pydantic import Field


class MailTemplate(CoreModel):
    template_key: str
    subject_template: str
    body_template: str
    description: str = ""
    enabled: bool = True


class AttachmentScanStatus(StrEnum):
    CLEAN = "clean"
    SKIPPED = "skipped"
    INFECTED = "infected"
    ERROR = "error"


class FieldAclOperation(StrEnum):
    READ = "read"
    INSERT = "create"
    UPDATE = "update"
    DELETE = "delete"
    EXPORT = "export"


class FieldAclEffect(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    OBFUSCATE = "obfuscate"


class FieldAclPolicy(BasicModel):
    app_key: str = ""
    form_key: str = ""
    model_key: str = ""
    field_path: str = "*"
    operation: FieldAclOperation = FieldAclOperation.READ
    actor_selector: dict[str, Any] | str = Field(default_factory=dict)
    workflow_stage: str = ""
    task_key: str = ""
    effect: FieldAclEffect = FieldAclEffect.ALLOW
    reason: str = ""
    priority: int | Decimal | Decimal128 = 100


class AppUser(User):
    avatar_url: str = ""


class ModelGroupsRule(BasicModel):
    """Riga flat di component.properties.models_groups (vedi
    app.ozon_env_acl.model_rules_sync) — permessi CRUD+export per
    (app_code, model, group)."""

    model: str = ""
    group: str = ""
    read: bool = False
    create: bool = False
    update: bool = False
    delete: bool = False
    export: bool = False


class ModelFieldsRule(BasicModel):
    """Riga flat di component.properties.models_restricted_fields (vedi
    app.ozon_env_acl.model_rules_sync):
    - rule_type="fields": campi ristretti per gruppo (da fields_rule.allowed_groups)
    - rule_type="record": filtro riga-per-riga (da record_rulse), group vuoto,
      filters e' una query mongo verbatim (puo' contenere nodi {"var": ...}
      non ancora risolti — vedi app.ozon_env_acl.render_query_vars)
    """

    model: str = ""
    rule_type: str = "fields"
    group: str = ""
    restricted_fields: list[str] = Field(default_factory=list)
    filters: dict[str, Any] = Field(default_factory=dict)
    read: bool = False
    create: bool = False
    update: bool = False
    delete: bool = False
