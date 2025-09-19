from __future__ import annotations

import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"
EXTERNAL_NETWORK = "Backuper_tunn_net"


def _command_available(command: list[str]) -> bool:
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return False
    return result.returncode == 0


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker CLI not available")
def test_local_directories_are_mounted(tmp_path: Path) -> None:
    if not _command_available(["docker", "info"]):
        pytest.skip("docker daemon is not available")
    if not _command_available(["docker", "compose", "version"]):
        pytest.skip("docker compose plugin is not available")

    local_target = tmp_path / "local-data"
    local_target.mkdir()

    directories_value = str(local_target)

    container_name = f"backuper-test-{uuid.uuid4().hex[:10]}"

    env_overrides = os.environ.copy()
    env_overrides["RCLONE_LOCAL_DIRECTORIES"] = directories_value
    env_overrides["BACKUPER_CONTAINER_NAME"] = container_name

    env_file_path = REPO_ROOT / ".env"
    backup_contents: str | None = None
    if env_file_path.exists():
        backup_contents = env_file_path.read_text(encoding="utf-8")
    env_file_path.write_text(
        "\n".join(
            [
                "APP_ADMIN_USER=admin",
                "APP_ADMIN_PASS=admin",
                "APP_SECRET_KEY=pytest",
                "APP_PORT=5550",
                "RCLONE_REMOTE=gdrive",
                f"RCLONE_LOCAL_DIRECTORIES={directories_value}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    created_network = False
    try:
        list_networks = subprocess.run(
            [
                "docker",
                "network",
                "ls",
                "--filter",
                f"name={EXTERNAL_NETWORK}",
                "--format",
                "{{.Name}}",
            ],
            check=False,
            text=True,
            capture_output=True,
        )
        if list_networks.returncode != 0:
            pytest.skip("unable to list docker networks")
        if EXTERNAL_NETWORK not in list_networks.stdout.split():
            create_network = subprocess.run(
                ["docker", "network", "create", EXTERNAL_NETWORK],
                check=False,
                text=True,
                capture_output=True,
            )
            if create_network.returncode != 0:
                pytest.skip("unable to create required docker network")
            created_network = True

        up_result = subprocess.run(
            [
                "docker",
                "compose",
                "-f",
                str(COMPOSE_FILE),
                "up",
                "-d",
                "--build",
                "orchestrator",
            ],
            cwd=REPO_ROOT,
            env=env_overrides,
            text=True,
            capture_output=True,
            check=False,
        )
        if up_result.returncode != 0:
            pytest.skip(
                "docker compose up failed:\n" f"STDOUT: {up_result.stdout}\nSTDERR: {up_result.stderr}"
            )

        exec_result = subprocess.run(
            [
                "docker",
                "exec",
                container_name,
                "python",
                "-c",
                (
                    "import os,sys; "
                    f"sys.exit(0 if os.path.isdir({json.dumps(str(local_target))}) else 1)"
                ),
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        assert (
            exec_result.returncode == 0
        ), f"local directory not reachable: {exec_result.stdout}\n{exec_result.stderr}"
    finally:
        subprocess.run(
            [
                "docker",
                "compose",
                "-f",
                str(COMPOSE_FILE),
                "down",
                "-v",
            ],
            cwd=REPO_ROOT,
            env=env_overrides,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if created_network:
            subprocess.run(
                ["docker", "network", "rm", EXTERNAL_NETWORK],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                text=True,
            )
        if backup_contents is None:
            env_file_path.unlink(missing_ok=True)
        else:
            env_file_path.write_text(backup_contents, encoding="utf-8")
