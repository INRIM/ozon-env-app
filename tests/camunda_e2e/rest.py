from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import httpx


def env(name: str, default: str = "") -> str:
    value = os.getenv(name, default)
    if value == "":
        raise RuntimeError(f"Missing required env var: {name}")
    return value


class CamundaRest:
    def __init__(
        self,
        *,
        base_url: str,
        timeout: float = 20.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    @classmethod
    def from_env(cls) -> "CamundaRest":
        return cls(
            base_url=env("CAMUNDA_REST_ADDRESS"),
        )

    def headers(self) -> dict[str, str]:
        return {"content-type": "application/json"}

    def deploy_bpmn(self, bpmn_path: Path) -> dict[str, Any]:
        with bpmn_path.open("rb") as handle:
            files = {"resources": (bpmn_path.name, handle, "application/xml")}
            response = httpx.post(
                f"{self.base_url}/deployments",
                headers={
                    k: v
                    for k, v in self.headers().items()
                    if k != "content-type"
                },
                files=files,
                timeout=self.timeout,
            )
        response.raise_for_status()
        return response.json()

    def search_user_tasks(
        self, process_instance_key: str
    ) -> list[dict[str, Any]]:
        response = httpx.post(
            f"{self.base_url}/user-tasks/search",
            headers=self.headers(),
            json={
                "filter": {
                    "processInstanceKey": process_instance_key,
                    "state": "CREATED",
                },
                "page": {"from": 0, "limit": 20},
            },
            timeout=self.timeout,
        )
        response.raise_for_status()
        data = response.json()
        if isinstance(data, dict):
            return data.get("items", [])
        return data if isinstance(data, list) else []

    def wait_user_task(
        self,
        process_instance_key: str,
        *,
        element_id: str = "",
        attempts: int = 60,
        delay: float = 1.0,
    ) -> dict[str, Any]:
        for _ in range(attempts):
            tasks = self.search_user_tasks(process_instance_key)
            for task in tasks:
                if not element_id or task.get("elementId") == element_id:
                    return task
            time.sleep(delay)
        raise TimeoutError(
            f"user task not found process={process_instance_key} element={element_id}"
        )

    def wait_no_open_tasks(
        self,
        process_instance_key: str,
        *,
        attempts: int = 60,
        delay: float = 1.0,
    ) -> None:
        for _ in range(attempts):
            if not self.search_user_tasks(process_instance_key):
                return
            time.sleep(delay)
        raise TimeoutError(
            f"process still has open user tasks: {process_instance_key}"
        )
