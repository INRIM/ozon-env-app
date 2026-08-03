import os
import shutil
import stat
import subprocess
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP_SCRIPT = PROJECT_ROOT / "bootstrap.sh"


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def test_bootstrap_runs_workers_repository_entry_point(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    workers_dir = project_dir / "workers"
    bin_dir = tmp_path / "bin"
    workers_dir.mkdir(parents=True)
    bin_dir.mkdir()
    shutil.copy2(BOOTSTRAP_SCRIPT, project_dir / "bootstrap.sh")
    (project_dir / "bootstrap.py").write_text("")

    _write_executable(
        workers_dir / "start-workers.sh",
        """#!/usr/bin/env bash
printf 'start-workers|%s\n' "$PWD" >> "${BOOTSTRAP_WORKERS_TEST_LOG}"
""",
    )
    _write_executable(
        bin_dir / "docker",
        """#!/usr/bin/env bash
printf 'docker|%s\n' "$*" >> "${BOOTSTRAP_WORKERS_TEST_LOG}"
""",
    )

    log_file = tmp_path / "commands.log"
    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env["BOOTSTRAP_WORKERS_TEST_LOG"] = str(log_file)

    result = subprocess.run(
        [str(project_dir / "bootstrap.sh"), "admin-uid"],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert log_file.read_text().splitlines()[-1] == (
        f"start-workers|{tmp_path}"
    )
