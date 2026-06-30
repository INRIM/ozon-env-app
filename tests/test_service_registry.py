from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.core.service_registry import CommandResult
from app.core.service_registry import ServiceRegistryCore
from app.core.service_registry import repo_rec_name


class FakeModel:
    def __init__(self) -> None:
        self.records: dict[str, dict] = {}

    async def upsert(self, data=None, rec_name="", **kwargs):
        payload = dict(data or {})
        key = rec_name or payload["rec_name"]
        payload["rec_name"] = key
        self.records[key] = payload
        return payload

    async def by_name(self, rec_name: str):
        return self.records.get(rec_name)

    async def find(self, domain=None, sort=""):
        records = list(self.records.values())
        return sorted(records, key=lambda item: item.get("rec_name", ""))

    def get_domain(self, query):
        return query or {}


class FakeEnv:
    def __init__(self) -> None:
        self.models = {
            "service_registry": FakeModel(),
            "service_registry_repo": FakeModel(),
        }

    def get(self, name: str):
        return self.models.get(name)


class FakeRunner:
    def __init__(self, result: CommandResult | None = None) -> None:
        self.calls = []
        self.result = result or CommandResult(returncode=0, stdout="ok")

    async def compose(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


def test_repo_rec_name_is_stable_and_safe():
    url = "git@gitlab.example:team/mail_sender.git"

    assert repo_rec_name(url) == repo_rec_name(url)
    assert repo_rec_name(url).startswith("mail_sender_")


def test_register_manifest_stores_validated_service():
    env = FakeEnv()
    registry = ServiceRegistryCore(env)

    payload = asyncio.run(
        registry.register_manifest(
            {
                "code": "mail_sender",
                "kind": "worker",
                "image": "ozonapp.mail_sender:latest",
                "env_requires": ["APP_CODE", "APP_CODE", ""],
            },
            manifest_path="services/mail_sender/manifest.json",
            source_path="services/mail_sender",
        )
    )

    assert payload["rec_name"] == "mail_sender"
    assert payload["network"] == "ozn-network"
    assert payload["env_requires"] == ["APP_CODE"]
    assert (
        env.models["service_registry"].records["mail_sender"]["status"]
        == "registered"
    )


def test_register_manifest_rejects_invalid_kind():
    registry = ServiceRegistryCore(FakeEnv())

    with pytest.raises(Exception):
        asyncio.run(
            registry.register_manifest(
                {
                    "code": "bad",
                    "kind": "daemon",
                    "image": "ozonapp.bad:latest",
                }
            )
        )


def test_register_repo_stores_repo_record():
    env = FakeEnv()
    registry = ServiceRegistryCore(env)

    payload = asyncio.run(
        registry.register_repo(
            url="https://example.test/services/calendar_scheduler.git",
            version="main",
        )
    )

    assert payload["rec_name"].startswith("calendar_scheduler_")
    assert payload["manifest_path"] == "manifest.json"
    assert (
        env.models["service_registry_repo"].records[payload["rec_name"]][
            "active"
        ]
        is True
    )


def test_up_uses_compose_runner_and_marks_running():
    env = FakeEnv()
    registry = ServiceRegistryCore(env)
    runner = FakeRunner()
    asyncio.run(
        registry.register_manifest(
            {
                "code": "mail_sender",
                "kind": "worker",
                "image": "ozonapp.mail_sender:latest",
                "compose_file": "docker-compose.yml",
            },
            source_path="services/mail_sender",
        )
    )

    result = asyncio.run(
        registry.up(
            "mail_sender",
            runner=runner,
            project_root=Path("/repo"),
        )
    )

    assert result["status"] == "running"
    assert runner.calls[0]["action"] == "up"
    assert runner.calls[0]["build"] is True
    assert (
        runner.calls[0]["cwd"] == Path("/repo/services/mail_sender").resolve()
    )
    assert (
        env.models["service_registry"].records["mail_sender"]["status"]
        == "running"
    )


def test_down_marks_error_when_compose_fails():
    env = FakeEnv()
    registry = ServiceRegistryCore(env)
    runner = FakeRunner(CommandResult(returncode=1, stderr="compose failed"))
    asyncio.run(
        registry.register_manifest(
            {
                "code": "calendar_scheduler",
                "kind": "scheduler",
                "image": "ozonapp.calendar_scheduler:latest",
            }
        )
    )

    result = asyncio.run(
        registry.down(
            "calendar_scheduler", runner=runner, project_root=Path("/repo")
        )
    )

    assert result["status"] == "error"
    assert (
        env.models["service_registry"].records["calendar_scheduler"][
            "last_error"
        ]
        == "compose failed"
    )
