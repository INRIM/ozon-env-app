from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from typing import Literal
from typing import Protocol

from pydantic import BaseModel
from pydantic import Field
from pydantic import field_validator

SERVICE_REGISTRY_MODEL = "service_registry"
SERVICE_REGISTRY_REPO_MODEL = "service_registry_repo"


class SharedVolume(BaseModel):
    name: str
    target: str

    @field_validator("name", "target")
    @classmethod
    def _required(cls, value: str) -> str:
        value = str(value or "").strip()
        if not value:
            raise ValueError("value required")
        return value


class ServiceManifest(BaseModel):
    code: str
    kind: Literal["worker", "scheduler", "gateway"]
    network: str = "ozn-network"
    shared_volumes: list[SharedVolume] = Field(default_factory=list)
    env_requires: list[str] = Field(default_factory=list)
    endpoint: str = ""
    image: str
    build_context: str = "."
    repo: str = ""
    version: str = "local"
    compose_file: str = "docker-compose.yml"
    health: dict[str, Any] | str | None = None

    @field_validator("code")
    @classmethod
    def _valid_code(cls, value: str) -> str:
        value = str(value or "").strip()
        if not value:
            raise ValueError("code required")
        if not value.replace("_", "").replace("-", "").isalnum():
            raise ValueError("code accepts only letters, digits, '-' and '_'")
        return value

    @field_validator("network", "image", "build_context", "compose_file")
    @classmethod
    def _required_text(cls, value: str) -> str:
        value = str(value or "").strip()
        if not value:
            raise ValueError("value required")
        return value

    @field_validator("env_requires")
    @classmethod
    def _clean_env_requires(cls, value: list[str]) -> list[str]:
        cleaned = [str(item or "").strip() for item in value]
        return list(dict.fromkeys([item for item in cleaned if item]))


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class ComposeRunner(Protocol):
    async def compose(
        self,
        *,
        compose_file: Path,
        cwd: Path,
        action: Literal["up", "down"],
        build: bool = True,
    ) -> CommandResult: ...


class DockerComposeRunner:
    async def compose(
        self,
        *,
        compose_file: Path,
        cwd: Path,
        action: Literal["up", "down"],
        build: bool = True,
    ) -> CommandResult:
        argv = ["docker", "compose", "-f", str(compose_file)]
        if action == "up":
            argv.extend(["up", "-d"])
            if build:
                argv.append("--build")
        else:
            argv.append("down")
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        return CommandResult(
            returncode=proc.returncode,
            stdout=stdout.decode("utf-8", errors="replace"),
            stderr=stderr.decode("utf-8", errors="replace"),
        )


def repo_rec_name(url: str) -> str:
    normalized = str(url or "").strip()
    if not normalized:
        raise ValueError("repo url required")
    digest = hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:12]
    tail = normalized.rstrip("/").rsplit("/", 1)[-1].replace(".git", "")
    safe_tail = "".join(
        ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in tail
    )
    safe_tail = safe_tail.strip("_-") or "repo"
    return f"{safe_tail}_{digest}"


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


class ServiceRegistryCore:
    def __init__(self, env: Any) -> None:
        self.env = env

    def _model(self, model_name: str) -> Any:
        model = self.env.get(model_name)
        if model is None:
            raise ValueError(f"Model '{model_name}' not registered")
        return model

    async def register_repo(
        self,
        *,
        url: str,
        version: str = "main",
        manifest_path: str = "manifest.json",
        active: bool = True,
    ) -> dict[str, Any]:
        payload = {
            "rec_name": repo_rec_name(url),
            "url": str(url or "").strip(),
            "version": str(version or "main").strip(),
            "manifest_path": str(manifest_path or "manifest.json").strip(),
            "active": bool(active),
        }
        await self._upsert(SERVICE_REGISTRY_REPO_MODEL, payload)
        return payload

    async def register_manifest(
        self,
        manifest_data: dict[str, Any],
        *,
        manifest_path: str = "",
        source_path: str = "",
    ) -> dict[str, Any]:
        manifest = ServiceManifest.model_validate(manifest_data)
        payload = manifest.model_dump(mode="python")
        payload.update(
            {
                "rec_name": manifest.code,
                "status": "registered",
                "last_error": "",
                "manifest_path": manifest_path,
                "source_path": source_path,
            }
        )
        await self._upsert(SERVICE_REGISTRY_MODEL, payload)
        return payload

    async def list_services(
        self, query: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        model = self._model(SERVICE_REGISTRY_MODEL)
        domain = query or {}
        if hasattr(model, "get_domain"):
            domain = model.get_domain(domain)
        if hasattr(model, "find"):
            records = await model.find(domain=domain, sort="rec_name:asc")
        elif hasattr(model, "search"):
            records = await model.search(domain)
        else:
            records = getattr(model, "records", {}).values()
        return [record_to_dict(record) for record in records]

    async def up(
        self,
        code: str,
        *,
        runner: ComposeRunner | None = None,
        project_root: Path | None = None,
        build: bool = True,
    ) -> dict[str, Any]:
        return await self._compose_action(
            code,
            action="up",
            runner=runner or DockerComposeRunner(),
            project_root=project_root,
            build=build,
        )

    async def down(
        self,
        code: str,
        *,
        runner: ComposeRunner | None = None,
        project_root: Path | None = None,
    ) -> dict[str, Any]:
        return await self._compose_action(
            code,
            action="down",
            runner=runner or DockerComposeRunner(),
            project_root=project_root,
            build=False,
        )

    async def _compose_action(
        self,
        code: str,
        *,
        action: Literal["up", "down"],
        runner: ComposeRunner,
        project_root: Path | None,
        build: bool,
    ) -> dict[str, Any]:
        record = await self._load_service(code)
        source_dir = self._source_dir(record, project_root=project_root)
        compose_file = source_dir / str(
            record.get("compose_file") or "docker-compose.yml"
        )
        result = await runner.compose(
            compose_file=compose_file,
            cwd=source_dir,
            action=action,
            build=build,
        )
        status = (
            "running"
            if action == "up" and result.ok
            else "stopped" if result.ok else "error"
        )
        last_error = "" if result.ok else (result.stderr or result.stdout)
        await self._update_status(str(record["rec_name"]), status, last_error)
        return {
            "rec_name": record["rec_name"],
            "status": status,
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }

    async def _load_service(self, code: str) -> dict[str, Any]:
        model = self._model(SERVICE_REGISTRY_MODEL)
        if hasattr(model, "by_name"):
            record = await model.by_name(code)
        elif hasattr(model, "load"):
            record = await model.load({"rec_name": code})
        else:
            record = getattr(model, "records", {}).get(code)
        data = record_to_dict(record)
        if not data:
            raise ValueError(f"Service '{code}' not registered")
        return data

    def _source_dir(
        self,
        record: dict[str, Any],
        *,
        project_root: Path | None,
    ) -> Path:
        source_path = str(record.get("source_path") or "").strip()
        manifest_path = str(record.get("manifest_path") or "").strip()
        if source_path:
            base = Path(source_path)
        elif manifest_path:
            base = Path(manifest_path).parent
        else:
            base = Path(".")
        # Path assoluto o risalita `..`: rifiutati. `cwd` finisce dritto in
        # `docker compose -f <file>` (vedi DockerComposeRunner), quindi un
        # record che punta fuori dall'albero dei servizi permetterebbe di
        # far eseguire un compose file arbitrario del filesystem. Il gate
        # primario e' l'admin-only sul router, questo e' il secondo strato.
        if base.is_absolute() or ".." in base.parts:
            raise ValueError(
                f"source_path must be a relative path without '..': {base}"
            )
        if project_root is not None:
            base = project_root / base
        return base.resolve()

    async def _update_status(
        self,
        rec_name: str,
        status: str,
        last_error: str = "",
    ) -> None:
        record = await self._load_service(rec_name)
        record.update({"status": status, "last_error": last_error})
        await self._upsert(SERVICE_REGISTRY_MODEL, record)

    async def _upsert(self, model_name: str, payload: dict[str, Any]) -> Any:
        model = self._model(model_name)
        rec_name = str(payload.get("rec_name") or "").strip()
        if hasattr(model, "upsert"):
            return await model.upsert(data=payload, rec_name=rec_name)
        if hasattr(model, "by_name") and hasattr(model, "new"):
            existing = await model.by_name(rec_name)
            record = await model.new(data=payload)
            if existing and hasattr(model, "update"):
                return await model.update(record)
            if hasattr(model, "insert"):
                return await model.insert(record)
        records = getattr(model, "records", None)
        if isinstance(records, dict):
            records[rec_name] = payload.copy()
            return payload
        raise ValueError(f"Model '{model_name}' does not support upsert")
