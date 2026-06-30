from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC
from abc import abstractmethod
from collections.abc import Callable
from typing import Any
from typing import Protocol

from app.app_settings import EnvSettings

logger = logging.getLogger("uvicorn.error")

# SDK ufficiale Camunda 8 (gia' dipendenza). Sincrono: le chiamate vengono
# eseguite in thread (`asyncio.to_thread`) per non bloccare l'event loop.
try:  # pragma: no cover - dipende dall'ambiente
    from camunda_orchestration_sdk import CamundaClient
    from camunda_orchestration_sdk.models.process_creation_by_id import (
        ProcessCreationById,
    )
    from camunda_orchestration_sdk.models.user_task_completion_request import (
        UserTaskCompletionRequest,
    )
    from camunda_orchestration_sdk.models.user_task_search_query import (
        UserTaskSearchQuery,
    )
    from camunda_orchestration_sdk.models.user_task_search_query_filter import (
        UserTaskSearchQueryFilter,
    )
    from camunda_orchestration_sdk.models.process_instance_creation_instruction_by_id_variables import (  # noqa: E501
        ProcessInstanceCreationInstructionByIdVariables,
    )
    from camunda_orchestration_sdk.models.user_task_completion_request_variables import (  # noqa: E501
        UserTaskCompletionRequestVariables,
    )
    from camunda_orchestration_sdk.models.user_task_state_exact_match import (
        UserTaskStateExactMatch,
    )
    from camunda_orchestration_sdk.models.variable_search_query import (
        VariableSearchQuery,
    )
    from camunda_orchestration_sdk.models.variable_search_query_filter import (
        VariableSearchQueryFilter,
    )
    from camunda_orchestration_sdk.types import Unset
except ImportError:  # pragma: no cover - SDK assente
    CamundaClient = None
    ProcessCreationById = None
    UserTaskCompletionRequest = None
    UserTaskSearchQuery = None
    UserTaskSearchQueryFilter = None
    ProcessInstanceCreationInstructionByIdVariables = None
    UserTaskCompletionRequestVariables = None
    UserTaskStateExactMatch = None
    VariableSearchQuery = None
    VariableSearchQueryFilter = None
    Unset = None


def _sdk_value(value: Any, default: Any = None) -> Any:
    """Normalizza i valori SDK: Unset/None -> default."""
    if value is None:
        return default
    if Unset is not None and isinstance(value, Unset):
        return default
    return value


class WorkflowDefinition(Protocol):
    @property
    def process_id(self) -> str: ...

    def build_start_variables(
        self,
        request: Any,
        *,
        execution_id: str,
    ) -> dict[str, Any]: ...


class BaseCamundaGateway(ABC):
    def __init__(self, settings: EnvSettings) -> None:
        self.settings = settings

    @abstractmethod
    async def start_process(
        self,
        *,
        workflow_definition: WorkflowDefinition,
        request: Any,
        execution_id: str,
    ) -> str:
        raise NotImplementedError

    @abstractmethod
    async def start_process_raw(
        self,
        *,
        process_id: str,
        variables: dict[str, Any],
    ) -> str:
        raise NotImplementedError

    @abstractmethod
    async def complete_task(
        self,
        *,
        process_instance_key: str,
        variables: dict[str, Any],
    ) -> None:
        raise NotImplementedError


class Camunda8Gateway(BaseCamundaGateway):
    """Gateway Camunda 8 basato sul SDK ufficiale `camunda_orchestration_sdk`.

    Il SDK e' sincrono: le chiamate girano in thread. `sdk_client_factory` e'
    iniettabile (i test passano un client fake -> niente Camunda reale).
    """

    def __init__(
        self,
        settings: EnvSettings,
        *,
        sdk_client_factory: Callable[[], Any] | None = None,
    ) -> None:
        super().__init__(settings)
        self.sdk_client_factory = (
            sdk_client_factory or self._default_sdk_client_factory
        )

    def _sdk_configuration(self) -> dict[str, Any]:
        base_url = self.settings.camunda_tasklist_url.rstrip("/")
        rest_address = base_url if base_url.endswith("/v2") else f"{base_url}/v2"
        cfg: dict[str, Any] = {"CAMUNDA_REST_ADDRESS": rest_address}
        if self.settings.camunda_auth_enabled:
            cfg.update(
                {
                    "CAMUNDA_AUTH_STRATEGY": "OAUTH",
                    "CAMUNDA_CLIENT_ID": self.settings.camunda_client_id,
                    "CAMUNDA_CLIENT_SECRET": self.settings.camunda_client_secret,
                    "CAMUNDA_OAUTH_URL": self.settings.camunda_oauth_token_url,
                }
            )
        else:
            cfg["CAMUNDA_AUTH_STRATEGY"] = "NONE"
        return cfg

    def _default_sdk_client_factory(self) -> Any:
        if CamundaClient is None:
            raise RuntimeError(
                "camunda_orchestration_sdk is required to use Camunda"
            )
        return CamundaClient(configuration=self._sdk_configuration())

    def _effective_tenant_id(self) -> str:
        # Tenant per start E search devono combaciare. `<default>`/vuoto = nessun
        # tenant esplicito.
        tenant_id = str(self.settings.camunda_tenant_id or "").strip()
        if not tenant_id or tenant_id == "<default>":
            return ""
        return tenant_id

    def _tenant_kw(self) -> dict[str, Any]:
        tenant_id = self._effective_tenant_id()
        return {"tenant_id": tenant_id} if tenant_id else {}

    async def start_process(
        self,
        *,
        workflow_definition: WorkflowDefinition,
        request: Any,
        execution_id: str,
    ) -> str:
        return await self.start_process_raw(
            process_id=workflow_definition.process_id,
            variables=workflow_definition.build_start_variables(
                request,
                execution_id=execution_id,
            ),
        )

    async def start_process_raw(
        self,
        *,
        process_id: str,
        variables: dict[str, Any],
    ) -> str:
        if not self.settings.camunda_enabled:
            return ""
        return await asyncio.to_thread(
            self._start_process_sync, process_id, variables
        )

    def _start_process_sync(
        self, process_id: str, variables: dict[str, Any]
    ) -> str:
        sdk_vars = (
            ProcessInstanceCreationInstructionByIdVariables.from_dict(
                variables or {}
            )
            if ProcessInstanceCreationInstructionByIdVariables is not None
            else (variables or {})
        )
        data = ProcessCreationById(
            process_definition_id=process_id,
            variables=sdk_vars,
            **self._tenant_kw(),
        )
        try:
            with self.sdk_client_factory() as client:
                result = client.create_process_instance(data=data)
        except Exception as exc:  # noqa: BLE001 - mappa il not-found a 404
            if _is_not_found(exc):
                raise LookupError(
                    f"Camunda process definition '{process_id}' is not "
                    "deployed or not visible to the configured Camunda REST API."
                ) from exc
            raise
        key = _sdk_value(getattr(result, "process_instance_key", None), "")
        return str(key or "")

    async def complete_task(
        self,
        *,
        process_instance_key: str,
        variables: dict[str, Any],
    ) -> str:
        if not self.settings.camunda_enabled or not process_instance_key:
            return ""
        task = await self._find_current_task(process_instance_key)
        task_key = task.get("user_task_key") or task.get("userTaskKey")
        if not task_key:
            raise LookupError("Camunda task response does not contain a key")
        await asyncio.to_thread(
            self._complete_task_sync, str(task_key), variables
        )
        return str(task_key)

    def _complete_task_sync(
        self, task_key: str, variables: dict[str, Any]
    ) -> None:
        sdk_vars = (
            UserTaskCompletionRequestVariables.from_dict(variables or {})
            if UserTaskCompletionRequestVariables is not None
            else (variables or {})
        )
        data = UserTaskCompletionRequest(variables=sdk_vars)
        with self.sdk_client_factory() as client:
            client.complete_user_task(task_key, data=data)

    async def process_status(
        self, process_instance_key: str
    ) -> dict[str, Any]:
        if not self.settings.camunda_enabled or not process_instance_key:
            return {
                "status": "disabled",
                "process_id": process_instance_key,
                "tasks": [],
                "variables": {},
            }
        tasks = await self._find_tasks(process_instance_key)
        return {
            "status": "running" if tasks else "unknown",
            "process_id": process_instance_key,
            "tasks": tasks,
            "variables": {},
        }

    async def process_state(self, process_instance_key: str) -> str:
        """Stato dell'istanza: ACTIVE | COMPLETED | TERMINATED | unknown.

        `unknown` se l'istanza non e' (ancora) leggibile (lag secondary storage).
        """
        if not process_instance_key:
            return "unknown"
        try:
            return await asyncio.to_thread(
                self._process_state_sync, process_instance_key
            )
        except Exception:  # noqa: BLE001 - lag/404 -> unknown, il caller ripete
            return "unknown"

    def _process_state_sync(self, process_instance_key: str) -> str:
        with self.sdk_client_factory() as client:
            result = client.get_process_instance(str(process_instance_key))
        state = _sdk_value(getattr(result, "state", None), "")
        return str(getattr(state, "value", state) or "unknown")

    async def wait_until_settled(
        self,
        process_instance_key: str,
        *,
        exclude_task_key: str = "",
        timeout_seconds: float,
        interval_seconds: float,
    ) -> dict[str, Any]:
        """Attende che il processo avanzi oltre i service/external task fino a:
        un nuovo user task CREATED, o la fine del processo (COMPLETED/TERMINATED).
        Ritorna {settled, reason, tasks, variables}. Le variabili sono catturate
        ad ogni giro (per evitare il lag del secondary storage dopo l'end del
        processo). Allo scadere del timeout: settled=False.
        """
        if not self.settings.camunda_enabled or not process_instance_key:
            return {
                "settled": True,
                "reason": "disabled",
                "tasks": [],
                "variables": {},
            }
        deadline = time.monotonic() + max(timeout_seconds, 0.0)
        exclude = str(exclude_task_key or "")
        last_vars: dict[str, Any] = {}
        while True:
            state = await self.process_state(process_instance_key)
            # cattura le variabili finche' il processo e' leggibile (prima che
            # l'end lo sposti nel secondary storage con lag).
            current_vars = await self.get_process_variables(
                process_instance_key
            )
            if current_vars:
                last_vars = current_vars
            if state in ("COMPLETED", "TERMINATED"):
                return {
                    "settled": True,
                    "reason": state.lower(),
                    "tasks": [],
                    "variables": last_vars,
                }
            tasks = await self._find_tasks(process_instance_key)
            fresh = [
                t
                for t in tasks
                if str(t.get("user_task_key") or "") != exclude
            ]
            if fresh:
                return {
                    "settled": True,
                    "reason": "user_task",
                    "tasks": fresh,
                    "variables": last_vars,
                }
            if time.monotonic() >= deadline:
                logger.warning(
                    "camunda wait timeout process=%s (external task non "
                    "completato entro %ss)",
                    process_instance_key,
                    timeout_seconds,
                )
                return {
                    "settled": False,
                    "reason": "timeout",
                    "tasks": [],
                    "variables": last_vars,
                }
            await asyncio.sleep(max(interval_seconds, 0.05))

    async def get_process_variables(
        self, process_instance_key: str
    ) -> dict[str, Any]:
        """Variabili di processo (name -> value deserializzato). Camunda 8 non
        ha local var sul completamento job: si usano le variabili normali del
        processo. I value sono JSON-encoded -> json.loads."""
        if not self.settings.camunda_enabled or not process_instance_key:
            return {}
        try:
            return await asyncio.to_thread(
                self._get_process_variables_sync, process_instance_key
            )
        except Exception:  # noqa: BLE001 - lag/404 -> {}, il caller fa fallback
            return {}

    async def get_process_history_variables(
        self, process_instance_key: str
    ) -> dict[str, Any]:
        """Variabili dell'istanza lette dalla history/search API.

        Camunda puo' spostare rapidamente un'istanza completata/incidentata fuori
        dalla vista runtime. Il caller usa questo metodo come fallback quando il
        settle non porta `last_task`, cosi la risposta client resta guidata dalla
        struct prodotta dall'external task.
        """
        if not self.settings.camunda_enabled or not process_instance_key:
            return {}
        try:
            return await asyncio.to_thread(
                self._get_process_variables_sync, process_instance_key
            )
        except Exception:  # noqa: BLE001 - history non disponibile -> fallback
            return {}

    def _get_process_variables_sync(
        self, process_instance_key: str
    ) -> dict[str, Any]:
        import json

        query = VariableSearchQuery(
            filter_=VariableSearchQueryFilter(
                process_instance_key=str(process_instance_key),
                **self._tenant_kw(),
            )
        )
        with self.sdk_client_factory() as client:
            result = client.search_variables(
                data=query, truncate_values=False
            )
        items = _sdk_value(getattr(result, "items", None), []) or []
        out: dict[str, Any] = {}
        for item in items:
            name = _sdk_value(getattr(item, "name", None))
            raw = _sdk_value(getattr(item, "value", None))
            if not name:
                continue
            if isinstance(raw, str):
                try:
                    out[name] = json.loads(raw)
                except (ValueError, TypeError):
                    out[name] = raw
            else:
                out[name] = raw
        return out

    async def _find_current_task(
        self, process_instance_key: str
    ) -> dict[str, Any]:
        tasks = await self._find_tasks(process_instance_key)
        if not tasks:
            raise LookupError(
                "No active Camunda task found for process instance"
            )
        return tasks[0]

    async def _find_tasks(
        self, process_instance_key: str
    ) -> list[dict[str, Any]]:
        tasks = await asyncio.to_thread(
            self._find_tasks_sync, process_instance_key
        )
        logger.info(
            "camunda user-tasks/search process=%s state=CREATED -> %d task",
            process_instance_key,
            len(tasks),
        )
        return tasks

    def _find_tasks_sync(
        self, process_instance_key: str
    ) -> list[dict[str, Any]]:
        state = (
            UserTaskStateExactMatch.CREATED
            if UserTaskStateExactMatch is not None
            else "CREATED"
        )
        query = UserTaskSearchQuery(
            filter_=UserTaskSearchQueryFilter(
                process_instance_key=str(process_instance_key),
                state=state,
                **self._tenant_kw(),
            )
        )
        with self.sdk_client_factory() as client:
            result = client.search_user_tasks(data=query)
        items = _sdk_value(getattr(result, "items", None), []) or []
        return [_task_to_dict(item) for item in items]


def _task_to_dict(item: Any) -> dict[str, Any]:
    if isinstance(item, dict):
        data = item
        return {
            "user_task_key": data.get("user_task_key")
            or data.get("userTaskKey"),
            "element_id": data.get("element_id") or data.get("elementId"),
            "process_instance_key": data.get("process_instance_key")
            or data.get("processInstanceKey"),
        }
    return {
        "user_task_key": _sdk_value(getattr(item, "user_task_key", None)),
        "element_id": _sdk_value(getattr(item, "element_id", None)),
        "process_instance_key": _sdk_value(
            getattr(item, "process_instance_key", None)
        ),
    }


def _is_not_found(exc: Exception) -> bool:
    text = f"{type(exc).__name__} {exc}".lower()
    return (
        "404" in text
        or "not found" in text
        or "not deployed" in text
        or "no deployed process" in text
    )


CamundaGateway = Camunda8Gateway
