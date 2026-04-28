from __future__ import annotations

import asyncio
import time
from abc import ABC
from abc import abstractmethod
from collections.abc import Callable
from typing import Any
from typing import Protocol

from app.app_settings import EnvSettings
from app.core.camunda import create_camunda_zeebe_channel

try:
    import httpx
except ImportError:  # pragma: no cover - exercised only when Camunda is enabled
    httpx = None

try:
    from pyzeebe import ZeebeClient
except ImportError:  # pragma: no cover - exercised only when Camunda is enabled
    ZeebeClient = None


class WorkflowDefinition(Protocol):
    @property
    def process_id(self) -> str:
        ...

    def build_start_variables(
        self,
        request: Any,
        *,
        execution_id: str,
    ) -> dict[str, Any]:
        ...


class CamundaAccessTokenProvider:
    def __init__(self, settings: EnvSettings) -> None:
        self.settings = settings
        self._cached_token: str | None = None
        self._expires_at: float = 0.0
        self._lock = asyncio.Lock()

    async def get_access_token(self) -> str:
        if self._cached_token and time.time() < self._expires_at:
            return self._cached_token

        async with self._lock:
            if self._cached_token and time.time() < self._expires_at:
                return self._cached_token
            self._cached_token, self._expires_at = await self._fetch_token()
            return self._cached_token

    async def _fetch_token(self) -> tuple[str, float]:
        if httpx is None:
            raise RuntimeError("httpx is required to fetch Camunda access tokens")
        async with httpx.AsyncClient(
            timeout=20.0,
            verify=self.settings.camunda_verify_tls,
        ) as client:
            response = await client.post(
                self.settings.camunda_oauth_token_url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self.settings.camunda_client_id,
                    "client_secret": self.settings.camunda_client_secret,
                },
            )
            response.raise_for_status()
            payload = response.json()

        access_token = payload["access_token"]
        expires_in = int(payload.get("expires_in", 300))
        expires_at = time.time() + max(expires_in - 30, 30)
        return access_token, expires_at


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
    def __init__(
        self,
        settings: EnvSettings,
        *,
        token_provider: CamundaAccessTokenProvider | None = None,
        channel_factory: Callable[[], Any] | None = None,
        zeebe_client_factory: Callable[[Any], Any] | None = None,
        http_client_factory: Callable[[], Any] | None = None,
    ) -> None:
        super().__init__(settings)
        self.token_provider = token_provider or CamundaAccessTokenProvider(settings)
        self.channel_factory = channel_factory or self._default_channel_factory
        self.zeebe_client_factory = (
            zeebe_client_factory or self._default_zeebe_client_factory
        )
        self.http_client_factory = (
            http_client_factory or self._default_http_client_factory
        )

    def _default_channel_factory(self) -> Any:
        return create_camunda_zeebe_channel(
            grpc_address=self.settings.camunda_zeebe_address,
            client_id=self.settings.camunda_client_id,
            client_secret=self.settings.camunda_client_secret,
            authorization_server=self.settings.camunda_oauth_token_url,
            verify_tls=self.settings.camunda_verify_tls,
            secure=self.settings.camunda_zeebe_secure,
            audience=None,
        )

    def _default_zeebe_client_factory(self, channel: Any) -> Any:
        if ZeebeClient is None:
            raise RuntimeError("pyzeebe is required to use Camunda Zeebe")
        return ZeebeClient(channel)

    def _default_http_client_factory(self) -> Any:
        if httpx is None:
            raise RuntimeError("httpx is required to use Camunda Tasklist")
        return httpx.AsyncClient(
            timeout=20.0,
            verify=self.settings.camunda_verify_tls,
        )

    async def _tasklist_headers(self) -> dict[str, str]:
        token = await self.token_provider.get_access_token()
        return {"Authorization": f"Bearer {token}"}

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
        channel = self.channel_factory()
        try:
            client = self.zeebe_client_factory(channel)
            instance = await client.run_process(
                process_id,
                variables=variables,
                tenant_id=self.settings.camunda_tenant_id,
            )
            return str(instance.process_instance_key)
        finally:
            close = getattr(channel, "close", None)
            if close:
                result = close()
                if hasattr(result, "__await__"):
                    await result

    async def complete_task(
        self,
        *,
        process_instance_key: str,
        variables: dict[str, Any],
    ) -> None:
        if not self.settings.camunda_enabled or not process_instance_key:
            return
        task = await self._find_current_task(process_instance_key)
        headers = await self._tasklist_headers()
        async with self.http_client_factory() as client:
            response = await client.post(
                f"{self.settings.camunda_tasklist_url.rstrip('/')}/v1/tasks/{task['id']}/complete",
                json={"variables": variables},
                headers=headers,
            )
            response.raise_for_status()

    async def _find_current_task(self, process_instance_key: str) -> dict[str, Any]:
        headers = await self._tasklist_headers()
        payload: dict[str, Any] = {
            "state": "CREATED",
            "processInstanceKey": process_instance_key,
            "pageSize": 10,
        }
        if self.settings.camunda_tenant_id:
            payload["tenantIds"] = [self.settings.camunda_tenant_id]

        async with self.http_client_factory() as client:
            response = await client.post(
                f"{self.settings.camunda_tasklist_url.rstrip('/')}/v1/tasks/search",
                json=payload,
                headers=headers,
            )
            response.raise_for_status()
            response_payload = response.json()
        if isinstance(response_payload, list):
            tasks = response_payload
        else:
            tasks = response_payload.get("items", [])
        if not tasks:
            raise LookupError("No active Camunda task found for process instance")
        return tasks[0]


CamundaGateway = Camunda8Gateway
