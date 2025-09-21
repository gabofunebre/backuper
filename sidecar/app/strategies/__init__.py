"""Factory helpers for backup strategies."""

from __future__ import annotations

from typing import Any

from ..exceptions import ConfigError
from .base import BackupStrategy
from .custom import CustomStrategy
from .database import DatabaseDumpStrategy
from .file_archive import FileArchiveStrategy


def create_strategy(strategy_config, paths) -> BackupStrategy:
    """Instantiate the strategy declared in configuration."""

    options: dict[str, Any] = dict(strategy_config.config or {})
    strategy_type = strategy_config.type.lower()
    if strategy_type in {"database_dump", "postgres", "mysql", "mariadb"}:
        return DatabaseDumpStrategy(
            artifact_config=strategy_config.artifact,
            paths=paths,
            options=options,
        )
    if strategy_type in {"file_archive", "filesystem"}:
        return FileArchiveStrategy(
            artifact_config=strategy_config.artifact,
            paths=paths,
            options=options,
        )
    if strategy_type == "custom":
        return CustomStrategy(
            artifact_config=strategy_config.artifact,
            paths=paths,
            options=options,
        )
    raise ConfigError(f"Unsupported strategy type: {strategy_config.type}")

