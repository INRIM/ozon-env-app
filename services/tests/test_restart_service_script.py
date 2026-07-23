import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest


SERVICES_DIR = Path(__file__).resolve().parents[1]
RESTART_IMPLEMENTATION = SERVICES_DIR / "restart-service.sh"
SERVICE_NAMES = (
    "calendar_scheduler",
    "identity_manager",
    "mail_sender",
    "mcp_search",
)


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _fixture(
    tmp_path: Path, *, with_base_env: bool, with_service_env: bool
) -> tuple[Path, Path, dict[str, str]]:
    project_dir = tmp_path / "project"
    services_dir = project_dir / "services"
    service_dir = services_dir / "example_service"
    bin_dir = tmp_path / "bin"
    service_dir.mkdir(parents=True)
    bin_dir.mkdir()

    shutil.copy2(RESTART_IMPLEMENTATION, services_dir / "restart-service.sh")
    (service_dir / "docker-compose.yml").write_text("services: {}\n")
    (service_dir / "manifest.json").write_text('{"code": "example_service"}\n')
    if with_base_env:
        (project_dir / ".env").write_text("NETWORK=base-network\n")
    if with_service_env:
        (service_dir / "service.env").write_text("NETWORK=service-network\n")

    _write_executable(
        bin_dir / "docker",
        """#!/usr/bin/env bash
printf '%q ' "$@" > "${RESTART_TEST_LOG}"
printf '\\n' >> "${RESTART_TEST_LOG}"
""",
    )
    log_file = tmp_path / "docker.log"
    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env["RESTART_TEST_LOG"] = str(log_file)
    return service_dir, log_file, env


def _run(service_dir: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    implementation = service_dir.parent / "restart-service.sh"
    return subprocess.run(
        [str(implementation), str(service_dir), service_dir.name],
        cwd=service_dir.parent,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_restart_reloads_layered_env_and_recreates_only_service(
    tmp_path: Path,
) -> None:
    service_dir, log_file, env = _fixture(
        tmp_path, with_base_env=True, with_service_env=True
    )

    result = _run(service_dir, env)

    assert result.returncode == 0, result.stderr
    assert log_file.read_text().strip().split() == [
        "compose",
        "--env-file",
        str(service_dir / "../../.env"),
        "--env-file",
        str(service_dir / "service.env"),
        "up",
        "-d",
        "--force-recreate",
        "--no-deps",
    ]
    assert "example_service riavviato" in result.stdout


def test_restart_supports_service_with_only_its_own_env(tmp_path: Path) -> None:
    service_dir, log_file, env = _fixture(
        tmp_path, with_base_env=False, with_service_env=True
    )

    result = _run(service_dir, env)

    assert result.returncode == 0, result.stderr
    assert log_file.read_text().strip().split() == [
        "compose",
        "--env-file",
        str(service_dir / "service.env"),
        "up",
        "-d",
        "--force-recreate",
        "--no-deps",
    ]


@pytest.mark.parametrize("service_name", SERVICE_NAMES)
def test_every_tracked_service_exposes_executable_restart(service_name: str) -> None:
    restart_script = SERVICES_DIR / service_name / "restart.sh"

    assert restart_script.is_file()
    assert os.access(restart_script, os.X_OK)
    assert f'"{service_name}"' in restart_script.read_text()
