import subprocess
from typing import Any, Dict, List


def _run_cmd(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    """Run a command and return the completed process, raising RuntimeError on failure."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, check=True)
    except FileNotFoundError as exc:
        raise RuntimeError("rclone is not installed") from exc
    except subprocess.CalledProcessError as exc:
        msg = exc.stderr.strip() or exc.stdout.strip() or str(exc)
        raise RuntimeError(msg) from exc


def list_remotes() -> List[str]:
    """Return configured rclone remotes."""
    result = _run_cmd(["rclone", "listremotes"])
    remotes = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return [r[:-1] if r.endswith(":") else r for r in remotes]


def create_remote(name: str, params: Dict[str, Any]) -> None:
    """Create a new rclone remote with the provided parameters."""
    cmd = ["rclone", "config", "create", name]
    for key, value in params.items():
        cmd.extend([str(key), str(value)])
    _run_cmd(cmd)


def delete_remote(name: str) -> None:
    """Delete an existing rclone remote."""
    _run_cmd(["rclone", "config", "delete", name])
