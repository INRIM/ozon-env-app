from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

import httpx
from dotenv import load_dotenv


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _rest_base() -> str:
    base = (
        _env("CAMUNDA_REST_ADDRESS")
        or _env("CAMUNDA_TASKLIST_URL")
        or _env("CAMUNDA_API_URL")
        or _env("CAMUNDA_WEB_URL")
    )
    if not base:
        raise RuntimeError(
            "Missing Camunda REST endpoint: set CAMUNDA_API_URL, "
            "CAMUNDA_TASKLIST_URL or CAMUNDA_REST_ADDRESS"
        )
    base = base.rstrip("/")
    if base.endswith("/v2"):
        return base
    return f"{base}/v2"


def _host_reachable_from_host(url: str) -> str:
    parsed = urlparse(url)
    if parsed.hostname != "host.docker.internal":
        return url
    return url.replace("host.docker.internal", "127.0.0.1", 1)


def _auth_headers(timeout: float) -> dict[str, str]:
    if _env("CAMUNDA_AUTH_ENABLED", "true").lower() in {"0", "false", "no"}:
        return {}

    token_url = _env("CAMUNDA_OAUTH_TOKEN_URL") or _env("CAMUNDA_OAUTH_URL")
    client_id = _env("CAMUNDA_CLIENT_ID")
    client_secret = _env("CAMUNDA_CLIENT_SECRET")
    if not token_url or not client_id or not client_secret:
        return {}

    data = {
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
    }
    audience = (
        _env("CAMUNDA_TOKEN_AUDIENCE")
        or _env("CAMUNDA_IDENTITY_AUDIENCE")
        or "orchestration-api"
    )
    if audience:
        data["audience"] = audience

    response = httpx.post(token_url, data=data, timeout=timeout)
    response.raise_for_status()
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def deploy_bpmn(path: Path, *, timeout: float) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"BPMN not found: {path}")

    base_url = _host_reachable_from_host(_rest_base())
    headers = _auth_headers(timeout)
    with path.open("rb") as handle:
        response = httpx.post(
            f"{base_url}/deployments",
            headers=headers,
            files={"resources": (path.name, handle, "application/xml")},
            timeout=timeout,
        )
    response.raise_for_status()
    return response.json()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "bpmn",
        nargs="?",
        default="tests/camunda_e2e/test_request.bpmn",
        help="BPMN file to deploy",
    )
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args()

    load_dotenv(".env-local", override=False)
    load_dotenv(".env", override=False)

    payload = deploy_bpmn(Path(args.bpmn), timeout=args.timeout)
    processes = payload.get("processes") or []
    if processes:
        for process in processes:
            print(
                "deployed process "
                f"id={process.get('processDefinitionId') or process.get('processDefinitionId')} "
                f"key={process.get('processDefinitionKey')} "
                f"version={process.get('version')}"
            )
    else:
        print("deployment completed")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"deploy failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
