from __future__ import annotations

import json
from datetime import date
from datetime import datetime
from decimal import Decimal
from typing import Any
from typing import Protocol

from bson import Decimal128
from pydantic import BaseModel
from pydantic import Field
from pydantic import field_validator

EXT_SERVICE_MODEL = "ext_service"
EXT_SERVICE_PROCESS_MODEL = "ext_service_process"


class CamundaGatewayClient(Protocol):
    async def start_process_raw(
        self,
        *,
        process_id: str,
        variables: dict[str, Any],
    ) -> str: ...

    async def complete_task(
        self,
        *,
        process_instance_key: str,
        variables: dict[str, Any],
    ) -> None: ...

    async def process_status(
        self, process_instance_key: str
    ) -> dict[str, Any]: ...


class ExtServiceConfig(BaseModel):
    rec_name: str
    title: str = ""
    endpoint: str = ""
    status: str = "active"
    tipo: str = "rest"

    @field_validator("rec_name", "tipo")
    @classmethod
    def _required_text(cls, value: str) -> str:
        value = str(value or "").strip()
        if not value:
            raise ValueError("value required")
        return value

    @property
    def active(self) -> bool:
        return str(self.status or "").strip().lower() in {
            "active",
            "attivo",
            "live",
            "enabled",
            "1",
            "true",
        }


class ExtServiceProcessConfig(BaseModel):
    parent: str
    rec_name: str
    model: str = ""
    tenant_id: str = ""
    business_key: str = ""
    variables: dict[str, Any] = Field(default_factory=dict)

    @field_validator("parent", "rec_name")
    @classmethod
    def _required_text(cls, value: str) -> str:
        value = str(value or "").strip()
        if not value:
            raise ValueError("value required")
        return value

    @field_validator("variables", mode="before")
    @classmethod
    def _parse_variables(cls, value: Any) -> dict[str, Any]:
        if value in (None, ""):
            return {}
        if isinstance(value, dict):
            return value.copy()
        if isinstance(value, str):
            parsed = json.loads(value)
            if isinstance(parsed, dict):
                return parsed
        raise ValueError("variables must be a JSON object")


def record_to_dict(record: Any) -> dict[str, Any]:
    if record is None:
        return {}
    if isinstance(record, dict):
        return record.copy()
    if hasattr(record, "get_dict"):
        return record.get_dict()
    if hasattr(record, "model_dump"):
        return record.model_dump(mode="python")
    if hasattr(record, "dict"):
        return record.dict()
    return {}


class ServiceManagerCore:
    def __init__(self, env: Any) -> None:
        self.env = env

    async def load_process(
        self,
        process_key: str,
    ) -> tuple[ExtServiceConfig, ExtServiceProcessConfig]:
        process_model = self._model(EXT_SERVICE_PROCESS_MODEL)
        service_model = self._model(EXT_SERVICE_MODEL)
        process_record = await process_model.by_name(process_key)
        process_data = record_to_dict(process_record)
        if not process_data:
            raise LookupError(
                f"External service process '{process_key}' not found"
            )
        process = ExtServiceProcessConfig.model_validate(process_data)

        service_record = await service_model.by_name(process.parent)
        service_data = record_to_dict(service_record)
        if not service_data:
            raise LookupError(f"External service '{process.parent}' not found")
        service = ExtServiceConfig.model_validate(service_data)
        if not service.active:
            raise ValueError(
                f"External service '{service.rec_name}' is inactive"
            )
        return service, process

    async def start_camunda_process(
        self,
        process_key: str,
        payload: dict[str, Any] | None,
        *,
        gateway: CamundaGatewayClient,
    ) -> dict[str, Any]:
        service, process = await self.load_process(process_key)
        if service.tipo.lower() != "camunda":
            raise ValueError(
                f"External service '{service.rec_name}' is not a Camunda service"
            )
        variables = self._build_variables(process, payload or {})
        print(f"START PROC: {process.rec_name} ")
        process_id = await gateway.start_process_raw(
            process_id=process.rec_name,
            variables=variables,
        )
        return {
            "stato": {
                "status": "started",
                "service": service.rec_name,
                "process_key": process.rec_name,
            },
            "variables": variables,
            "process_id": process_id,
        }

    async def camunda_status(
        self,
        process_id: str,
        *,
        gateway: CamundaGatewayClient,
    ) -> dict[str, Any]:
        status = await gateway.process_status(process_id)
        return {
            "stato": status,
            "variables": status.get("variables", {}),
        }

    async def complete_camunda_task(
        self,
        process_id: str,
        payload: dict[str, Any] | None,
        *,
        gateway: CamundaGatewayClient,
        decision: str = "",
    ) -> dict[str, Any]:
        variables = self._complete_variables(payload or {}, decision=decision)
        completed_task_key = await gateway.complete_task(
            process_instance_key=process_id,
            variables=variables,
        )
        return {
            "stato": {
                "status": "completed",
                "process_id": process_id,
                "decision": decision,
            },
            "variables": variables,
            "completed_task_key": str(completed_task_key or ""),
        }

    def _model(self, model_name: str) -> Any:
        model = self.env.get(model_name)
        if model is None:
            raise LookupError(f"Model '{model_name}' not registered")
        return model

    def _build_variables(
        self,
        process: ExtServiceProcessConfig,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Costruisce le variabili di processo.

        Il **form** va annidato sotto la chiave = nome del model
        (`variables[model] = {...}`), non flat: cosi nei worker si accede al
        documento tramite il model, e se il processo agisce su piu' model non
        si crea conflitto di chiavi. Restano flat: le variabili esplicite
        (`payload["variables"]`), le var di controllo (`model`, `rec_name`,
        `tenant_id`, `business_key`) e i default di `process.variables`.
        """
        variables = process.variables.copy()

        # variabili esplicite (routing/controllo) -> flat
        explicit = payload.get("variables")
        if isinstance(explicit, dict):
            variables.update(explicit)

        # il form: tutto il payload tranne la chiave "variables" (gia' gestita).
        form = {
            k: v for k, v in payload.items() if k != "variables"
        }
        model = str(
            process.model
            or form.get("model")
            or form.get("data_model")
            or ""
        ).strip()
        if model and form:
            # documento annidato sotto il nome del model
            variables[model] = form
            variables["model"] = model
            # rec_name flat per comodita' dei worker (record update/notifiche)
            rec_name = str(form.get("rec_name") or "").strip()
            if rec_name:
                variables.setdefault("rec_name", rec_name)
        elif form:
            # nessun model noto -> fallback flat (compat)
            variables.update(form)

        if process.tenant_id:
            variables.setdefault("tenant_id", process.tenant_id)
        if process.business_key:
            variables.setdefault("business_key", process.business_key)
        if model:
            variables.setdefault("model", model)
        return _json_safe(variables)

    def _complete_variables(
        self,
        payload: dict[str, Any],
        *,
        decision: str = "",
    ) -> dict[str, Any]:
        variables: dict[str, Any] = {}
        if isinstance(payload.get("var"), dict):
            variables.update(payload["var"])
        if isinstance(payload.get("variables"), dict):
            variables.update(payload["variables"])
        if "form" in payload:
            variables["form"] = payload["form"]
        if "user" in payload:
            variables["user"] = payload["user"]
        if decision == "approved":
            variables["approved"] = True
        elif decision == "refused":
            variables["approved"] = False
            variables["refused"] = True
        return _json_safe(variables)


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal128):
        return str(value.to_decimal())
    if isinstance(value, Decimal):
        return str(value)
    return value
