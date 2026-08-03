import os
import shutil
import stat
import subprocess
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
UPDATE_SCRIPT = PROJECT_ROOT / "update-uv-locks.sh"


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _fixture(tmp_path: Path) -> tuple[Path, Path, dict[str, str]]:
    project_dir = tmp_path / "project"
    bin_dir = tmp_path / "bin"
    project_dir.mkdir()
    bin_dir.mkdir()

    shutil.copy2(UPDATE_SCRIPT, project_dir / UPDATE_SCRIPT.name)
    (project_dir / "pyproject.toml").write_text("[project]\nname = 'root'\n")
    (project_dir / ".gitignore").write_text(
        "workers/\nservices/ignored_service/\n"
    )

    for service_name in ("alpha", "zeta", "ignored_service"):
        service_dir = project_dir / "services" / service_name
        service_dir.mkdir(parents=True)
        (service_dir / "pyproject.toml").write_text(
            f"[project]\nname = '{service_name}'\n"
        )

    worker_dir = project_dir / "workers" / "separate_repo"
    worker_dir.mkdir(parents=True)
    (worker_dir / "pyproject.toml").write_text(
        "[project]\nname = 'worker'\n"
    )

    subprocess.run(["git", "init", "-q"], cwd=project_dir, check=True)
    subprocess.run(
        [
            "git",
            "add",
            "pyproject.toml",
            ".gitignore",
            "services/alpha/pyproject.toml",
            "services/zeta/pyproject.toml",
        ],
        cwd=project_dir,
        check=True,
    )

    _write_executable(
        bin_dir / "uv",
        """#!/usr/bin/env bash
printf '%s|%s\n' "$PWD" "$*" >> "${UV_LOCK_TEST_LOG}"
if [[ "$PWD" == "${UV_LOCK_FAIL_IN:-}" ]]; then
    exit 42
fi
""",
    )

    log_file = tmp_path / "uv.log"
    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env["UV_LOCK_TEST_LOG"] = str(log_file)
    return project_dir, log_file, env


def _run(
    project_dir: Path, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(project_dir / UPDATE_SCRIPT.name)],
        cwd=project_dir.parent,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_updates_root_and_tracked_services_only(tmp_path: Path) -> None:
    project_dir, log_file, env = _fixture(tmp_path)

    result = _run(project_dir, env)

    assert result.returncode == 0, result.stderr
    assert log_file.read_text().splitlines() == [
        f"{project_dir}|lock --upgrade",
        f"{project_dir / 'services/alpha'}|lock --upgrade",
        f"{project_dir / 'services/zeta'}|lock --upgrade",
    ]
    assert "Aggiornati 3 file uv.lock." in result.stdout


def test_stops_on_first_failed_lock_update(tmp_path: Path) -> None:
    project_dir, log_file, env = _fixture(tmp_path)
    env["UV_LOCK_FAIL_IN"] = str(project_dir / "services/alpha")

    result = _run(project_dir, env)

    assert result.returncode == 42
    assert log_file.read_text().splitlines() == [
        f"{project_dir}|lock --upgrade",
        f"{project_dir / 'services/alpha'}|lock --upgrade",
    ]
