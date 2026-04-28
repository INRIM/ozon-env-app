from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel
from pydantic import Field


class ActionType(StrEnum):
    AVVIA = "avvia"
    NEGA = "nega"
    ACCETTA = "accetta"
    COMMENTA = "commenta"


class ActionCommand(BaseModel):
    action: ActionType
    note: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)


class RuntimeExecutionStatus(StrEnum):
    PENDING = "pending"
    TASK_READY = "task-ready"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMEOUT = "timeout"


class RuntimeTaskRef(BaseModel):
    task_id: str = ""
    name: str = ""
    stage: str = ""
    type: str = "user"
    label: str = ""
    assignee_group: str = ""
    form_key: str = ""


class RuntimeExecution(BaseModel):
    id: str
    item_id: str
    action: ActionType
    status: RuntimeExecutionStatus = RuntimeExecutionStatus.PENDING
    message: str = ""
    process_instance_key: str = ""
    current_task: RuntimeTaskRef | None = None
    retry_after_ms: int = 1500
    error: str = ""
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None


class RuntimeItem(BaseModel):
    id: str
    owner_uid: str
    app_key: str = ""
    form_key: str = ""
    form_version: str = ""
    schema_hash: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
    config: dict[str, Any] = Field(default_factory=dict)
    runtime: dict[str, Any] = Field(default_factory=dict)
    process_instance_key: str = ""
    created_at: datetime
    updated_at: datetime
