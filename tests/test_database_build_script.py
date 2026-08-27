import os
import stat
import subprocess
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BUILD_SCRIPT = PROJECT_ROOT / "database" / "build_images.sh"


def _run_build(tmp_path: Path, mongo_version: str | None = None) -> list[str]:
    docker = tmp_path / "docker"
    docker.write_text(
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$@\" > \"${DOCKER_ARGS_LOG}\"\n"
    )
    docker.chmod(docker.stat().st_mode | stat.S_IXUSR)

    log = tmp_path / "docker-args.log"
    env = os.environ.copy()
    env["PATH"] = f"{tmp_path}{os.pathsep}{env['PATH']}"
    env["DOCKER_ARGS_LOG"] = str(log)
    if mongo_version is None:
        env.pop("MONGO_VERSION", None)
    else:
        env["MONGO_VERSION"] = mongo_version

    result = subprocess.run(
        [str(BUILD_SCRIPT)],
        cwd=PROJECT_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    return log.read_text().splitlines()


def test_database_build_pins_mongo_8_2_12_by_default(tmp_path: Path) -> None:
    args = _run_build(tmp_path)

    assert "MONGO_VERSION=8.2.12" in args


def test_database_build_allows_explicit_mongo_version(tmp_path: Path) -> None:
    args = _run_build(tmp_path, "8.2.11")

    assert "MONGO_VERSION=8.2.11" in args
