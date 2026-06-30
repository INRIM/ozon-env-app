from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.camunda_e2e.auth import TokenCache
from tests.camunda_e2e.rest import CamundaRest

pytestmark = pytest.mark.camunda


def _require_enabled() -> None:
    if os.getenv("CAMUNDA_E2E_ENABLED", "").lower() not in {
        "1",
        "true",
        "yes",
    }:
        pytest.skip("Camunda E2E disabled; set CAMUNDA_E2E_ENABLED=true")


class AppClient:
    """Client app per UN utente: l'APP_TOKEN (JWT keycloak) determina lo uid
    della sessione, quindi ogni chiamata risulta fatta da quell'utente."""

    def __init__(self, token: str) -> None:
        self.base_url = os.getenv(
            "APP_BASE_URL", "http://app:8000"
        ).rstrip("/")
        if not token:
            raise RuntimeError("token required for AppClient")
        self.token = token

    @property
    def headers(self) -> dict[str, str]:
        return {"authorization": f"Bearer {self.token}"}

    @staticmethod
    def _raise(response: httpx.Response) -> None:
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise AssertionError(
                f"{response.request.method} {response.request.url} failed "
                f"with {response.status_code}: {response.text}"
            ) from exc

    def post_record(
        self,
        model: str,
        rec_name: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        response = httpx.post(
            f"{self.base_url}/record/{model}/{rec_name}",
            headers=self.headers,
            json=payload,
            timeout=20.0,
        )
        self._raise(response)
        return response.json()["content"]["data"]

    def import_component(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = httpx.post(
            f"{self.base_url}/import/component",
            headers=self.headers,
            json=payload,
            timeout=30.0,
        )
        self._raise(response)
        return response.json()["content"]["data"]

    def get_record(self, model: str, rec_name: str) -> dict[str, Any]:
        response = httpx.get(
            f"{self.base_url}/record/{model}/{rec_name}",
            headers=self.headers,
            timeout=20.0,
        )
        self._raise(response)
        return response.json()["content"]["data"]

    def start_process(self, rec_name: str) -> str:
        payload = {
            "rec_name": rec_name,
            "nome": "Utente",
            "cognome": "E2E",
            "testo_richiesta": "Richiesta test Camunda",
            "stato_richiesta": "nuova",
            "stato_verifica": "attesa_responsabile",
            "uid": "utente",
            "requester_uid": "utente",
            "assignee": "utente",
        }
        response = httpx.post(
            f"{self.base_url}/gateway/camunda/start/Test_Process",
            params={"update-data": "true"},
            headers=self.headers,
            json=payload,
            timeout=30.0,
        )
        self._raise(response)
        body = response.json()
        # se il task successivo e' uno user task la risposta e' "self" = il form
        # rletto (mode=form, no process_id top-level): il process_id e' stato
        # salvato nel record (update-data).
        content = body.get("content") if isinstance(body, dict) else None
        if isinstance(content, dict):
            # user task successivo -> redirect "#" (reload pagina corrente)
            assert content.get("mode") == "redirect", body
            assert content.get("data", {}).get("next_page") == "#", body
            record = self.get_record("test_request", rec_name)
            process_id = str(record.get("process_id") or "")
        else:
            process_id = str(body.get("process_id") or "")
        if not process_id:
            raise RuntimeError("Camunda start returned empty process_id")
        return process_id

    def complete(self, process_id: str, variables: dict[str, Any]) -> None:
        response = httpx.post(
            f"{self.base_url}/gateway/camunda/complete/{process_id}",
            headers=self.headers,
            json={"variables": variables},
            timeout=30.0,
        )
        self._raise(response)

    def decide(
        self, process_id: str, decision: str, variables: dict[str, Any]
    ) -> dict[str, Any]:
        response = httpx.post(
            f"{self.base_url}/gateway/camunda/action/{process_id}/{decision}",
            headers=self.headers,
            json={"variables": variables},
            timeout=30.0,
        )
        self._raise(response)
        return response.json()


def _seed_app(app: AppClient) -> None:
    app.import_component(
        {
            "rec_name": "test_request",
            "title": "Test Request",
            "components": [
                {
                    "label": "Nome",
                    "key": "nome",
                    "type": "textfield",
                    "input": True,
                },
                {
                    "label": "Cognome",
                    "key": "cognome",
                    "type": "textfield",
                    "input": True,
                },
                {
                    "label": "Testo Richiesta",
                    "key": "testo_richiesta",
                    "type": "textarea",
                    "input": True,
                },
                {
                    "label": "Stato Richiesta",
                    "key": "stato_richiesta",
                    "type": "textfield",
                    "input": True,
                },
                {
                    "label": "Stato Verifica",
                    "key": "stato_verifica",
                    "type": "textfield",
                    "input": True,
                },
                {
                    "label": "Assignee",
                    "key": "assignee",
                    "type": "textfield",
                    "input": True,
                },
                {
                    "label": "Requester UID",
                    "key": "requester_uid",
                    "type": "textfield",
                    "input": True,
                },
                {
                    "label": "Process ID",
                    "key": "process_id",
                    "type": "textfield",
                    "input": True,
                },
                {
                    "label": "Notification Topic",
                    "key": "notification_topic",
                    "type": "textfield",
                    "input": True,
                },
            ],
            "active": True,
            "make_virtual_model": False,
            "display": "form",
        }
    )
    app.post_record(
        "ext_service",
        "camunda_local",
        {
            "rec_name": "camunda_local",
            "title": "Camunda Local E2E",
            "status": "active",
            "tipo": "camunda",
        },
    )
    app.post_record(
        "ext_service_process",
        "Test_Process",
        {
            "rec_name": "Test_Process",
            "parent": "camunda_local",
            "model": "test_request",
            "variables": "{}",
        },
    )
    for uid, responsible_uid in (
        ("utente", "responsible"),
        ("responsible", "manager"),
        ("manager", ""),
    ):
        app.post_record(
            "user",
            uid,
            {
                "rec_name": uid,
                "uid": uid,
                "responsible_uid": responsible_uid,
                "active": True,
                "deleted": 0,
            },
        )


def _deploy_bpmn(camunda: CamundaRest) -> None:
    if os.getenv("CAMUNDA_E2E_DEPLOY_BPMN", "true").lower() not in {
        "1",
        "true",
        "yes",
    }:
        return
    camunda.deploy_bpmn(Path("tests/camunda_e2e/test_request.bpmn"))


def _wait_record_state(
    app: AppClient,
    rec_name: str,
    *,
    expected_status: str,
    expected_check: str,
) -> dict[str, Any]:
    for _ in range(60):
        record = app.get_record("test_request", rec_name)
        if (
            record.get("stato_richiesta") == expected_status
            and record.get("stato_verifica") == expected_check
        ):
            return record
        time.sleep(1)
    raise TimeoutError(f"record {rec_name} did not reach expected final state")


@pytest.mark.parametrize(
    ("decision", "expected_check"),
    [
        ("approved", "approvata"),
        ("refused", "rifiutata"),
    ],
)
def test_camunda_real_process_approval_and_refusal(
    decision: str, expected_check: str
):
    _require_enabled()
    # Un APP_TOKEN per utente (mintato da keycloak): ogni step parla all'app
    # come l'utente giusto. e2e-admin (admin) fa il seed.
    tokens = TokenCache()
    admin = AppClient(tokens.get(os.getenv("E2E_ADMIN_USER", "e2e-admin")))
    utente = AppClient(tokens.get("utente"))
    responsible = AppClient(tokens.get("responsible"))
    manager = AppClient(tokens.get("manager"))

    camunda = CamundaRest.from_env()
    _deploy_bpmn(camunda)
    _seed_app(admin)

    rec_name = f"camunda-e2e-{decision}-{int(time.time())}"
    # avvio: il richiedente
    process_id = utente.start_process(rec_name)

    # start_request: lo completa il richiedente
    camunda.wait_user_task(process_id, element_id="start_request")
    utente.complete(
        process_id,
        {
            "model": "test_request",
            "rec_name": rec_name,
            "uid": "utente",
            "requester_uid": "utente",
            "assignee": "utente",
            "stato_richiesta": "in_corso",
            "stato_verifica": "attesa_responsabile",
        },
    )

    # resp_see: lo completa il responsabile
    camunda.wait_user_task(process_id, element_id="resp_see")
    responsible.complete(
        process_id,
        {
            "model": "test_request",
            "rec_name": rec_name,
            "uid": "responsible",
            "requester_uid": "utente",
            "assignee": "responsible",
            "stato_verifica": "visto_responsabile",
        },
    )

    # manager_appvive_refuse: decide il manager (approva/rifiuta)
    camunda.wait_user_task(process_id, element_id="manager_appvive_refuse")
    decide_resp = manager.decide(
        process_id,
        decision,
        {
            "model": "test_request",
            "rec_name": rec_name,
            "uid": "manager",
            "requester_uid": "utente",
            "assignee": "manager",
        },
    )

    # la response del complete riflette il risultato dell'external task
    # (notification): redirect alla lista del model (dalla struct last_task).
    content = decide_resp.get("content", decide_resp)
    assert content.get("mode") == "redirect", decide_resp
    assert content.get("data", {}).get("next_page") == "list_test_request"

    final = _wait_record_state(
        admin,
        rec_name,
        expected_status="completata",
        expected_check=expected_check,
    )
    assert final["notification_topic"] == f"sed_message_{decision}"
