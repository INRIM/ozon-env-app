from __future__ import annotations

import asyncio
import os
from typing import Any

import httpx


def env(name: str, default: str = "") -> str:
    value = os.getenv(name, default)
    if value == "":
        raise RuntimeError(f"Missing required env var: {name}")
    return value


class AppClient:
    def __init__(self) -> None:
        self.base_url = env("APP_BASE_URL").rstrip("/")
        # Il worker e' un attore di sistema: token admin (e2e-admin) mintato da
        # keycloak. APP_TOKEN statico resta come override opzionale.
        token = os.getenv("APP_TOKEN", "")
        if not token:
            from tests.camunda_e2e.auth import mint_token

            token = mint_token(os.getenv("E2E_ADMIN_USER", "e2e-admin"))
        self.token = token

    @property
    def headers(self) -> dict[str, str]:
        return {"authorization": f"Bearer {self.token}"}

    async def get_record(self, model: str, rec_name: str) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.get(
                f"{self.base_url}/record/{model}/{rec_name}",
                headers=self.headers,
            )
            response.raise_for_status()
            return response.json()["content"]["data"]

    async def save_record(
        self,
        model: str,
        rec_name: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(
                f"{self.base_url}/record/{model}/{rec_name}",
                headers=self.headers,
                json=payload,
            )
            response.raise_for_status()
            return response.json()["content"]["data"]


class CamundaJobClient:
    def __init__(self) -> None:
        self.rest_url = env("CAMUNDA_REST_ADDRESS").rstrip("/")
        self.worker_name = os.getenv("WORKER_NAME", "ozon-env-app-e2e-worker")
        self.timeout_ms = int(os.getenv("WORKER_TIMEOUT_MS", "30000"))

    async def headers(self) -> dict[str, str]:
        return {"content-type": "application/json"}

    async def activate(self, job_type: str) -> list[dict[str, Any]]:
        async with httpx.AsyncClient(timeout=35.0) as client:
            response = await client.post(
                f"{self.rest_url}/jobs/activation",
                headers=await self.headers(),
                json={
                    "type": job_type,
                    "maxJobsToActivate": 10,
                    "timeout": self.timeout_ms,
                    "worker": self.worker_name,
                },
            )
            response.raise_for_status()
            data = response.json()
        if isinstance(data, dict):
            return data.get("jobs", [])
        return data if isinstance(data, list) else []

    async def complete(self, job_key: str, variables: dict[str, Any]) -> None:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(
                f"{self.rest_url}/jobs/{job_key}/completion",
                headers=await self.headers(),
                json={"variables": variables},
            )
            response.raise_for_status()


async def run_check_user(
    job: dict[str, Any], app: AppClient
) -> dict[str, Any]:
    variables = dict(job.get("variables") or {})
    # il documento e' annidato sotto variables[model]; le var di routing
    # (assignee/uid) sono flat (settate dai task precedenti). Si legge flat con
    # fallback al documento (1° giro: dati iniziali dal form sotto il model).
    model = str(variables.get("model") or "")
    doc = variables.get(model) if isinstance(variables.get(model), dict) else {}
    current_uid = str(
        variables.get("uid")
        or variables.get("assignee")
        or doc.get("assignee")
        or doc.get("uid")
        or variables.get("requester_uid")
        or doc.get("requester_uid")
        or ""
    )
    if not current_uid:
        raise RuntimeError("ckeck_user job missing uid/assignee/requester_uid")

    user = await app.get_record("user", current_uid)
    next_uid = str(user.get("responsible_uid") or "").strip()
    if not next_uid:
        next_uid = current_uid

    requester_uid = str(
        variables.get("requester_uid")
        or doc.get("requester_uid")
        or current_uid
    )
    is_resp = current_uid != requester_uid
    variables.update(
        {
            "uid": next_uid,
            "assignee": next_uid,
            "previous_assignee": current_uid,
            "requester_uid": requester_uid,
            "is_resp": is_resp,
            "stato_richiesta": "in_corso",
            "stato_verifica": (
                "visto_responsabile" if is_resp else "attesa_responsabile"
            ),
        }
    )
    print(variables)
    return variables


async def run_notification(
    job: dict[str, Any],
    *,
    topic: str,
    app: AppClient,
) -> dict[str, Any]:
    variables = dict(job.get("variables") or {})
    approved = topic == "sed_message_approved"
    variables.update(
        {
            "stato_richiesta": "completata",
            "stato_verifica": "approvata" if approved else "rifiutata",
            "notification_topic": topic,
        }
    )
    model = str(variables.get("model") or "")
    rec_name = str(variables.get("rec_name") or "")
    if model and rec_name:
        record = await app.get_record(model, rec_name)
        record.update(
            {
                "stato_richiesta": variables["stato_richiesta"],
                "stato_verifica": variables["stato_verifica"],
                "notification_topic": topic,
            }
        )
        await app.save_record(model, rec_name, record)
    print(f"[camunda-e2e-worker] invio notifica topic={topic} rec={rec_name}")
    # struttura risultato task (contratto ProcessServiceCamunda): il client
    # viene rediretto alla lista del model. last_task = job_type.
    variables[topic] = {
        "completato": True,
        "error": False,
        "msg": "approvata" if approved else "rifiutata",
        "next_action": "redirect",
        "next_page": f"list_{model}" if model else "self",
        "document_type": "",
        "update_data": True,
        "model": model,
        "rec_name": rec_name,
    }
    variables["last_task"] = topic
    return variables


async def main() -> None:
    job_type = env("SERVICE_TASK_TYPE")
    app = AppClient()
    camunda = CamundaJobClient()
    while True:
        try:
            jobs = await camunda.activate(job_type)
            if not jobs:
                await asyncio.sleep(1.0)
                continue
            for job in jobs:
                if job_type == "ckeck_user":
                    variables = await run_check_user(job, app)
                elif job_type in {
                    "sed_message_approved",
                    "sed_message_refused",
                }:
                    variables = await run_notification(
                        job, topic=job_type, app=app
                    )
                else:
                    variables = dict(job.get("variables") or {})
                await camunda.complete(str(job["jobKey"]), variables)
                print(
                    "[camunda-e2e-worker] completed "
                    f"type={job_type} job={job['jobKey']}"
                )
        except httpx.HTTPStatusError as exc:
            print(
                "[camunda-e2e-worker] http error "
                f"{exc.response.status_code}: {exc.response.text}"
            )
            await asyncio.sleep(2.0)
        except Exception as exc:
            print(f"[camunda-e2e-worker] error: {exc!r}")
            await asyncio.sleep(2.0)


if __name__ == "__main__":
    asyncio.run(main())
