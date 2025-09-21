"""Custom strategy that delegates backup creation to external scripts."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..exceptions import ConfigError
from .command import CommandBasedStrategy, ensure_command_list


class CustomStrategy(CommandBasedStrategy):
    """Invoke user-provided scripts to generate the backup artifact."""

    def __init__(self, *, artifact_config, paths, options: dict[str, Any]) -> None:
        pre_commands = ensure_command_list(options.get("pre"), field="strategy.config.pre")
        post_commands = ensure_command_list(options.get("post"), field="strategy.config.post")
        command_value = options.get("command")
        commands_value = options.get("commands")
        if command_value and commands_value:
            raise ConfigError("Specify either strategy.config.command or strategy.config.commands, not both")
        backup_commands = ensure_command_list(
            command_value if command_value is not None else commands_value,
            field="strategy.config.command",
        )
        if not backup_commands:
            raise ConfigError("strategy.config.command is required for custom strategies")
        capture_stdout = bool(options.get("capture_stdout", False))
        environment = options.get("env") or {}
        if not isinstance(environment, dict):
            raise ConfigError("strategy.config.env must be a mapping when provided")
        workdir_value = options.get("workdir")
        workdir = Path(workdir_value) if workdir_value else None
        super().__init__(
            artifact_config=artifact_config,
            paths=paths,
            strategy_type="custom",
            pre_commands=pre_commands,
            backup_commands=backup_commands,
            post_commands=post_commands,
            capture_stdout=capture_stdout,
            environment={str(k): str(v) for k, v in environment.items()},
            workdir=workdir,
        )

