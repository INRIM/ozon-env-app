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
    INSERT = "insert"
    UPDATE = "update"


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
