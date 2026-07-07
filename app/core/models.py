from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Any

from bson import Decimal128
from ozonenv.core.BaseModels import BasicModel
from ozonenv.core.BaseModels import User
from pydantic import Field


class MailTemplate(BasicModel):
    template_key: str
    subject_template: str
    body_template: str
    description: str = ""
    enabled: bool = True

    @classmethod
    def table_columns(cls) -> dict:
        return {
            "template_key": "Chiave",
            "subject_template": "Oggetto",
            "description": "Descrizione",
            "enabled": "Attivo",
        }


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

    @classmethod
    def table_columns(cls) -> dict:
        return {
            "app_key": "App",
            "model_key": "Model",
            "field_path": "Campo",
            "operation": "Operazione",
            "effect": "Effetto",
            "priority": "Priorita'",
        }


class AppUser(User):
    avatar_url: str = ""


class ModelGroupsRule(BasicModel):
    """Riga flat di component.properties.models_groups (vedi
    app.ozon_env_acl.model_rules_sync) — permessi CRUD+export per
    (app_code, model, group).

    NON registrato in `_STATIC_MODELS` (app/deps/app_env.py): esiste un
    component/form reale per "model_groups_rule" con field type e
    tableView gia' corretti, quindi il model ORM resta dynamic (derivato
    da quel component) invece di essere forzato statico — niente
    table_columns/schema da duplicare qui. Questa classe serve solo a
    validare/normalizzare la riga PRIMA della scrittura in
    model_rules_sync.py (model_dump), niente a che vedere con la
    registrazione ORM."""

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

    NON registrato in `_STATIC_MODELS` (stesso motivo di ModelGroupsRule):
    esiste un component/form reale per "model_fields_rule". Il campo
    `filters` nel form usa `properties.type: "json"` (mapper ModelMaker
    "jsondata" -> dict) e `restricted_fields` usa `select multiple`
    (mapper "select_multi" -> List[Any]) cosi' il model dynamic generato
    ha i tipi giusti — con `textarea` semplice generava `str`, mismatch
    coi dati Mongo reali (list/dict) che causava un ValidationError.
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
