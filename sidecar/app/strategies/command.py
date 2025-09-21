"""Generic helpers for command-driven strategies."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any, Iterable, Optional

from ..exceptions import ConfigError, StrategyExecutionError
from .base import FileBasedStrategy


def ensure_command_list(value: Any, *, field: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError(f"{field} must be a string or list of strings")
    return [item for item in value if item]


class CommandBasedStrategy(FileBasedStrategy):
    """Execute shell commands to generate a file-based artifact."""

    def __init__(
        self,
        *,
        artifact_config,
        paths,
        strategy_type: str,
        pre_commands: Iterable[str] = (),
        backup_commands: Iterable[str] = (),
        post_commands: Iterable[str] = (),
        capture_stdout: bool = False,
        environment: Optional[dict[str, str]] = None,
        workdir: Optional[Path] = None,
    ) -> None:
        super().__init__(artifact_config=artifact_config, paths=paths)
        self._strategy_type = strategy_type
        self._pre_commands = [cmd for cmd in pre_commands if cmd]
        self._backup_commands = [cmd for cmd in backup_commands if cmd]
        self._post_commands = [cmd for cmd in post_commands if cmd]
        self._capture_stdout = capture_stdout
        self._extra_env = {str(k): str(v) for k, v in (environment or {}).items()}
        self._workdir = Path(workdir) if workdir else paths.workdir
        if not self._backup_commands:
            raise ConfigError(f"strategy.config.backup must contain at least one command for {strategy_type}")
        if self._capture_stdout and len(self._backup_commands) != 1:
            raise ConfigError("capture_stdout requires exactly one backup command")

    def _build_env(self, drive_folder_id: Optional[str]) -> dict[str, str]:
        env = os.environ.copy()
        env.update(
            {
                "SIDE_CAR_WORKDIR": str(self.paths.workdir),
                "SIDE_CAR_ARTIFACTS_DIR": str(self.paths.artifacts),
                "SIDE_CAR_TEMP_DUMP": str(self.paths.temp_dump),
                "SIDE_CAR_STRATEGY": self._strategy_type,
            }
        )
        if drive_folder_id:
            env["SIDE_CAR_DRIVE_FOLDER_ID"] = drive_folder_id
        env.update(self._extra_env)
        return env

    def _run_simple_commands(self, commands: Iterable[str], env: dict[str, str]) -> None:
        for command in commands:
            if not command.strip():
                continue
            try:
                subprocess.run(
                    command,
                    shell=True,
                    check=True,
                    cwd=str(self._workdir),
                    env=env,
                )
            except subprocess.CalledProcessError as exc:
                raise StrategyExecutionError(
                    f"Command failed with exit code {exc.returncode}: {command}"
                ) from exc

    def _run_and_capture(self, command: str, env: dict[str, str]) -> None:
        self.paths.temp_dump.parent.mkdir(parents=True, exist_ok=True)
        with self.paths.temp_dump.open("wb") as handle:
            process = subprocess.Popen(
                command,
                shell=True,
                cwd=str(self._workdir),
                env=env,
                stdout=handle,
                stderr=subprocess.PIPE,
            )
            _, stderr_data = process.communicate()
        if process.returncode != 0:
            message = stderr_data.decode("utf-8", errors="replace") if stderr_data else ""
            raise StrategyExecutionError(
                f"Command failed with exit code {process.returncode}: {command}\n{message.strip()}"
            )

    def prepare(self, drive_folder_id: Optional[str] = None):  # type: ignore[override]
        self._ensure_workspace()
        env = self._build_env(drive_folder_id)
        self._run_simple_commands(self._pre_commands, env)
        if self._capture_stdout:
            command = self._backup_commands[0]
            self._run_and_capture(command, env)
        else:
            self._run_simple_commands(self._backup_commands, env)
        self._run_simple_commands(self._post_commands, env)
        if not self.paths.temp_dump.exists():
            raise StrategyExecutionError(
                f"Strategy did not generate expected artifact at {self.paths.temp_dump}"
            )
        artifact_path = self._move_to_artifact(self.paths.temp_dump)
        checksum, size = self._compute_checksum(artifact_path)
        return self._register_metadata(artifact_path, size=size, checksum=checksum)

