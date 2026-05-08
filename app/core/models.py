from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any
from typing import Optional

from bson import Decimal128
from ozonenv.core.BaseModels import BasicModel
from ozonenv.core.BaseModels import CoreModel
from pydantic import AliasChoices
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


class AttachmentMetadata(CoreModel):
    entity_kind: str = "request"
    entity_id: str = Field(
        default="",
        validation_alias=AliasChoices("entity_id", "request_id"),
    )
    attachment_type: str
    original_name: str
    stored_name: str
    content_type: str
    size_bytes: int
    sha256: str = ""
    uploaded_by_uid: str
    uploaded_at: datetime
    scan_status: AttachmentScanStatus = AttachmentScanStatus.CLEAN
    scan_signature: str = ""
    scanned_at: Optional[datetime] = None
    scanner_engine: str = ""

    @property
    def request_id(self) -> str:
        return self.entity_id


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
