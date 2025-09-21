"""Custom exceptions used across the sidecar service."""

from __future__ import annotations


class ConfigError(RuntimeError):
    """Raised when the configuration file is invalid."""


class UnauthorizedError(RuntimeError):
    """Raised when the provided token does not match the expected one."""


class StrategyExecutionError(RuntimeError):
    """Raised when the backup strategy fails to produce an artifact."""

