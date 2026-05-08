from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import HTTPException
from fastapi import UploadFile
from fastapi import status
from ozonenv.OzonEnv import OzonEnv

from app.app_settings import EnvSettings
from app.core.models import AttachmentMetadata
from ozonenv.core.BaseModels import User
from app.core.runtime import ActionCommand
from app.core.runtime import ActionType
from app.core.runtime import RuntimeExecution
from app.core.runtime import RuntimeExecutionStatus
from app.core.runtime import RuntimeItem
from app.core.timezone import now_utc
from app.services.antivirus import AntivirusFileInfectedError
from app.services.antivirus import ClamAVScanner
from app.services.antivirus import scan_upload_non_blocking
from app.services.camunda import BaseCamundaGateway


def _normalize(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _normalize(nested) for key, nested in value.items()}
    if isinstance(value, list):
        return [_normalize(item) for item in value]
    return value


def _safe_file_name(name: str) -> str:
    sanitized = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")
    return sanitized or "upload.bin"


class RuntimeService:
    def __init__(
        self,
        env: OzonEnv,
        settings: EnvSettings,
        camunda_gateway: BaseCamundaGateway,
        antivirus: ClamAVScanner | None = None,
    ) -> None:
        self.env = env
        self.settings = settings
        self.camunda_gateway = camunda_gateway
        self.antivirus = antivirus

    async def create_item(
        self,
        user: User,
        payload: dict[str, Any],
        config: dict[str, Any],
    ) -> RuntimeItem:
        now = now_utc()
        app_key, form_key = self._resolve_app_form_keys(config)
        item = RuntimeItem(
            id=str(uuid4()),
            owner_uid=user.uid,
            app_key=app_key,
            form_key=form_key,
            payload=payload,
            config=config,
            runtime={
                "stage": self._draft_stage(config),
                "execution_status": RuntimeExecutionStatus.COMPLETED.value,
                "message": "Bozza salvata",
                "synced_at": now.isoformat(),
            },
            created_at=now,
            updated_at=now,
        )
        await self._upsert_model("runtime_item", item.id, item.model_dump(mode="python"))
        return item

    async def list_items(self, user: User) -> list[RuntimeItem]:
        query: dict[str, Any] = {"deleted": 0, "active": True}
        if not self._is_admin(user):
            query["owner_uid"] = user.uid
        model = self.env.get("runtime_item")
        rows = await model.find(domain=query, sort="updated_at:desc", limit=0)
        return [RuntimeItem.model_validate(_normalize(row)) for row in rows]

    async def get_item(self, user: User, item_id: str) -> RuntimeItem:
        item = await self._load_item(item_id)
        self._ensure_access(user, item)
        return item

    async def update_item(
        self,
        user: User,
        item_id: str,
        payload: dict[str, Any],
    ) -> RuntimeItem:
        item = await self.get_item(user, item_id)
        self._ensure_edit_access(user, item)
        updated = item.model_copy(
            update={
                "payload": payload,
                "runtime": {
                    **(item.runtime or {}),
                    "stage": str(
                        (item.runtime or {}).get("stage")
                        or self._draft_stage(item.config)
                    ),
                    "execution_status": RuntimeExecutionStatus.COMPLETED.value,
                    "message": "Bozza aggiornata",
                    "synced_at": now_utc().isoformat(),
                },
                "updated_at": now_utc(),
            }
        )
        await self._save_item(updated)
        return updated

    async def run_action(
        self,
        user: User,
        item_id: str,
        command: ActionCommand,
    ) -> tuple[RuntimeItem, RuntimeExecution]:
        item = await self.get_item(user, item_id)
        execution = await self._create_execution(item.id, command.action)

        if command.action == ActionType.AVVIA:
            self._ensure_edit_access(user, item)
            process_id = self._resolve_process_id(item, command.action)
            if not process_id:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Nessun processo configurato per l'azione avvia",
                )
            process_key = await self.camunda_gateway.start_process_raw(
                process_id=process_id,
                variables={
                    "itemId": item.id,
                    "payload": item.payload,
                    "config": item.config,
                    "action": command.action.value,
                    "note": command.note,
                    "executionId": execution.id,
                },
            )
            item = item.model_copy(
                update={
                    "process_instance_key": process_key,
                    "runtime": {
                        "stage": "STARTUP",
                        "execution_id": execution.id,
                        "execution_status": RuntimeExecutionStatus.PENDING.value,
                        "message": "Avvio workflow in corso",
                        "synced_at": now_utc().isoformat(),
                    },
                    "updated_at": now_utc(),
                }
            )
            execution = execution.model_copy(
                update={
                    "process_instance_key": process_key,
                    "updated_at": now_utc(),
                }
            )
            await self._save_item(item)
            await self._save_execution(execution)
            return item, execution

        self._ensure_review_access(user, item)
        await self.camunda_gateway.complete_task(
            process_instance_key=item.process_instance_key,
            variables={
                "itemId": item.id,
                "executionId": execution.id,
                "action": command.action.value,
                "decisionNote": command.note,
                "payloadPatch": command.payload,
            },
        )
        return item, execution

    async def upload_attachment(
        self,
        user: User,
        item_id: str,
        attachment_type: str,
        upload: UploadFile,
    ) -> AttachmentMetadata:
        item = await self.get_item(user, item_id)
        self._ensure_edit_access(user, item)

        content = await upload.read()
        if len(content) > self.settings.max_upload_size_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail="Attachment too large",
            )

        try:
            scan_result = await scan_upload_non_blocking(self.antivirus, content)
        except AntivirusFileInfectedError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"Attachment rejected by antivirus: {exc.signature}",
            ) from exc

        attachment_id = str(uuid4())
        folder = Path(self.settings.upload_root) / item_id
        folder.mkdir(parents=True, exist_ok=True)
        stored_name = f"{attachment_id}-{_safe_file_name(upload.filename or 'upload.bin')}"
        destination = folder / stored_name
        destination.write_bytes(content)

        metadata = AttachmentMetadata(
            entity_kind="runtime-item",
            entity_id=item_id,
            attachment_type=attachment_type,
            original_name=upload.filename or stored_name,
            stored_name=stored_name,
            content_type=upload.content_type or "application/octet-stream",
            size_bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
            uploaded_by_uid=user.uid,
            uploaded_at=now_utc(),
            scan_status=scan_result.status,
            scan_signature=scan_result.signature,
            scanned_at=scan_result.scanned_at,
            scanner_engine=scan_result.engine,
            create_datetime=now_utc(),
            update_datetime=now_utc(),
            owner_uid=user.uid,
            owner_name=user.rec_name or user.uid,
            owner_mail=user.mail,
        )
        await self._upsert_model(
            "attachment",
            str(metadata.id),
            metadata.model_dump(by_alias=True, mode="python"),
        )
        return metadata

    async def _load_item(self, item_id: str) -> RuntimeItem:
        model = self.env.get("runtime_item")
        document = await model.by_name(item_id)
        if not document:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Item non trovato",
            )
        return RuntimeItem.model_validate(_normalize(document))

    async def _save_item(self, item: RuntimeItem) -> None:
        await self._upsert_model("runtime_item", item.id, item.model_dump(mode="python"))

    async def _create_execution(
        self,
        item_id: str,
        action: ActionType,
    ) -> RuntimeExecution:
        now = now_utc()
        execution = RuntimeExecution(
            id=f"rex_{uuid4().hex[:12]}",
            item_id=item_id,
            action=action,
            status=RuntimeExecutionStatus.PENDING,
            message=f"Azione {action.value} in corso",
            created_at=now,
            updated_at=now,
        )
        await self._save_execution(execution)
        return execution

    async def _save_execution(self, execution: RuntimeExecution) -> None:
        await self._upsert_model(
            "runtime_execution",
            execution.id,
            execution.model_dump(mode="python"),
        )

    async def _upsert_model(
        self,
        model_name: str,
        rec_name: str,
        payload: dict[str, Any],
    ) -> Any:
        model = self.env.get(model_name)
        data = _normalize({**payload, "rec_name": rec_name})
        return await model.upsert(data=data, rec_name=rec_name)

    def _resolve_process_id(self, item: RuntimeItem, action: ActionType) -> str:
        config_process = item.config.get("process")
        if isinstance(config_process, dict):
            action_process = config_process.get(action.value)
            if isinstance(action_process, str) and action_process:
                return action_process
        mapped = self.settings.runtime_action_process_map.get(action.value)
        return mapped or self.settings.runtime_default_process_id or self.settings.camunda_process_id

    def _draft_stage(self, config: dict[str, Any]) -> str:
        return str(config.get("draft_stage") or config.get("draftStage") or "BOZZA")

    def _is_admin(self, user: User) -> bool:
        return bool(
            user.tech_admin or user.user_role in self.settings.runtime_admin_roles
        )

    def _ensure_access(self, user: User, item: RuntimeItem) -> None:
        if self._is_admin(user) or item.owner_uid == user.uid:
            return
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Accesso negato all'item runtime",
        )

    def _ensure_edit_access(self, user: User, item: RuntimeItem) -> None:
        if self._is_admin(user):
            return
        current_stage = str(
            (item.runtime or {}).get("stage") or self._draft_stage(item.config)
        )
        if item.owner_uid == user.uid and current_stage == self._draft_stage(item.config):
            return
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Item modificabile solo in bozza",
        )

    def _ensure_review_access(self, user: User, item: RuntimeItem) -> None:
        if self._is_admin(user):
            return
        runtime = item.runtime if isinstance(item.runtime, dict) else {}
        reviewer_uid = str(runtime.get("reviewer_uid") or "")
        if reviewer_uid and reviewer_uid == user.uid:
            return
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Task non assegnato all'utente corrente",
        )

    def _resolve_app_form_keys(self, config: dict[str, Any]) -> tuple[str, str]:
        app_key = str(
            config.get("app_key")
            or config.get("appKey")
            or self.settings.runtime_default_app_key
        ).strip().lower()
        form_key = str(
            config.get("form_key")
            or config.get("formKey")
            or self.settings.runtime_default_form_key
        ).strip().lower()
        return app_key, form_key
