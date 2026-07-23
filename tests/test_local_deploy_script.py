import os
import shutil
import stat
import subprocess
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEPLOY_SCRIPT = PROJECT_ROOT / "deploy.sh"


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _deployment_fixture(
    tmp_path: Path, *, network_exists: bool = False
) -> tuple[Path, Path, dict[str, str]]:
    deploy_dir = tmp_path / "project"
    bin_dir = tmp_path / "bin"
    deploy_dir.mkdir()
    bin_dir.mkdir()

    shutil.copy2(DEPLOY_SCRIPT, deploy_dir / "deploy.sh")
    (deploy_dir / "docker-compose.yml").write_text("services: {}\n")
    (deploy_dir / ".env.example").write_text("APP_CODE=test\n")

    _write_executable(
        deploy_dir / "build_imges.sh",
        '#!/usr/bin/env bash\nprintf "build\\n" >> "${DEPLOY_TEST_LOG}"\n',
    )
    _write_executable(
        deploy_dir / "bootstrap.sh",
        '#!/usr/bin/env bash\nprintf "bootstrap\\n" >> "${DEPLOY_TEST_LOG}"\n',
    )

    inspect_status = 0 if network_exists else 1
    _write_executable(
        bin_dir / "docker",
        f"""#!/usr/bin/env bash
printf 'docker' >> "${{DEPLOY_TEST_LOG}}"
printf ' %q' "$@" >> "${{DEPLOY_TEST_LOG}}"
printf '\n' >> "${{DEPLOY_TEST_LOG}}"
if [[ "${{1:-}} ${{2:-}}" == "network inspect" ]]; then
    exit {inspect_status}
fi
""",
    )

    log_file = tmp_path / "commands.log"
    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env["DEPLOY_TEST_LOG"] = str(log_file)
    return deploy_dir, log_file, env


def _run(
    deploy_dir: Path, env: dict[str, str], *args: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(deploy_dir / "deploy.sh"), *args],
        cwd=deploy_dir,
        env=env,
        stdin=subprocess.DEVNULL,
        text=True,
        capture_output=True,
        check=False,
    )


def test_deploy_builds_creates_env_and_network_then_starts_compose(
    tmp_path: Path,
) -> None:
    deploy_dir, log_file, env = _deployment_fixture(tmp_path)

    result = _run(deploy_dir, env)

    assert result.returncode == 0, result.stderr
    assert (deploy_dir / ".env").read_text() == "APP_CODE=test\n"
    assert stat.S_IMODE((deploy_dir / ".env").stat().st_mode) == 0o600
    assert log_file.read_text().splitlines() == [
        "build",
        "docker network inspect ozn-network",
        "docker network create ozn-network",
        f"docker compose -f {deploy_dir / 'docker-compose.yml'} up -d",
    ]
    assert "Input non interattivo" in result.stdout


def test_deploy_preserves_env_and_can_skip_build(tmp_path: Path) -> None:
    deploy_dir, log_file, env = _deployment_fixture(tmp_path, network_exists=True)
    (deploy_dir / ".env").write_text("SECRET=preserve-me\n")

    result = _run(deploy_dir, env, "--skip-build", "--no-bootstrap")

    assert result.returncode == 0, result.stderr
    assert (deploy_dir / ".env").read_text() == "SECRET=preserve-me\n"
    assert log_file.read_text().splitlines() == [
        "docker network inspect ozn-network",
        f"docker compose -f {deploy_dir / 'docker-compose.yml'} up -d",
    ]


def test_bootstrap_can_be_requested_explicitly(tmp_path: Path) -> None:
    deploy_dir, log_file, env = _deployment_fixture(tmp_path, network_exists=True)

    result = _run(deploy_dir, env, "--skip-build", "--bootstrap")

    assert result.returncode == 0, result.stderr
    assert log_file.read_text().splitlines()[-1] == "bootstrap"


def test_unknown_option_fails_before_deployment(tmp_path: Path) -> None:
    deploy_dir, log_file, env = _deployment_fixture(tmp_path)

    result = _run(deploy_dir, env, "local")

    assert result.returncode == 2
    assert "opzione non supportata: local" in result.stderr
    assert not log_file.exists()
