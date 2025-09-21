"""Sidecar backup service exposing capabilities and export endpoints."""

from __future__ import annotations

import hmac
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import yaml
from flask import Flask, Response, jsonify, request, stream_with_context

from .exceptions import ConfigError, StrategyExecutionError, UnauthorizedError
from .strategies import create_strategy
from .strategies.base import ArtifactMetadata, BackupStrategy

CONFIG_PATH_ENV = "SIDECAR_CONFIG_PATH"
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "config.yaml"


@dataclass
class CapabilitiesConfig:
    version: str
    types: list[str]
    est_seconds: Optional[int] = None
    est_size: Optional[int] = None


@dataclass
class StrategyArtifactConfig:
    filename: str
    format: str
    content_type: str


@dataclass
class StrategyConfig:
    type: str
    artifact: StrategyArtifactConfig
    config: dict[str, Any]


@dataclass
class PathsConfig:
    workdir: Path
    artifacts: Path
    temp_dump: Path


@dataclass
class SecretsConfig:
    api_token: str


@dataclass
class AppConfig:
    port: int


@dataclass
class SidecarConfig:
    app: AppConfig
    capabilities: CapabilitiesConfig
    strategy: StrategyConfig
    paths: PathsConfig
    secrets: SecretsConfig


_ENV_VAR_PATTERN = re.compile(r"\$\{(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?::-(?P<default>[^}]*))?\}")


def _substitute_env_vars(raw_text: str) -> str:
    """Replace ${VAR} or ${VAR:-default} occurrences with environment values."""

    def replace(match: re.Match[str]) -> str:
        name = match.group("name")
        default = match.group("default")
        if default is not None:
            return os.getenv(name, default)
        if name not in os.environ:
            raise ConfigError(f"Missing required environment variable: {name}")
        return os.environ[name]

    return _ENV_VAR_PATTERN.sub(replace, raw_text)


def _to_int(value: object, *, field: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"Invalid integer for {field}: {value!r}") from exc


def _load_yaml(path: Path) -> dict:
    try:
        raw_text = path.read_text()
    except OSError as exc:
        raise ConfigError(f"Unable to read config file {path}: {exc}") from exc
    substituted = _substitute_env_vars(raw_text)
    try:
        data = yaml.safe_load(substituted) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML configuration: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError("Configuration root must be a mapping")
    return data


def _load_capabilities(data: dict) -> CapabilitiesConfig:
    capabilities = data.get("capabilities") or {}
    version = capabilities.get("version")
    if not version:
        raise ConfigError("capabilities.version is required")
    types_value = capabilities.get("types")
    if not isinstance(types_value, list) or not all(isinstance(t, str) for t in types_value):
        raise ConfigError("capabilities.types must be a list of strings")
    est_seconds = capabilities.get("est_seconds")
    if est_seconds is not None:
        est_seconds = _to_int(est_seconds, field="capabilities.est_seconds")
    est_size = capabilities.get("est_size")
    if est_size is not None:
        est_size = _to_int(est_size, field="capabilities.est_size")
    return CapabilitiesConfig(version=version, types=[str(t) for t in types_value], est_seconds=est_seconds, est_size=est_size)


def _load_strategy(data: dict) -> StrategyConfig:
    strategy = data.get("strategy") or {}
    strategy_type = strategy.get("type")
    if not strategy_type:
        raise ConfigError("strategy.type is required")
    artifact_data = strategy.get("artifact") or {}
    filename = artifact_data.get("filename", "backup.dat")
    artifact_format = artifact_data.get("format", "binary")
    content_type = artifact_data.get("content_type", "application/octet-stream")
    if not isinstance(filename, str) or not filename:
        raise ConfigError("strategy.artifact.filename must be a non-empty string")
    if not isinstance(artifact_format, str) or not artifact_format:
        raise ConfigError("strategy.artifact.format must be a non-empty string")
    if not isinstance(content_type, str) or not content_type:
        raise ConfigError("strategy.artifact.content_type must be a non-empty string")
    config_section = strategy.get("config") or {}
    if not isinstance(config_section, dict):
        raise ConfigError("strategy.config must be a mapping if provided")
    return StrategyConfig(
        type=str(strategy_type),
        artifact=StrategyArtifactConfig(
            filename=filename,
            format=artifact_format,
            content_type=content_type,
        ),
        config=config_section,
    )


def _load_paths(data: dict) -> PathsConfig:
    paths = data.get("paths") or {}
    try:
        workdir = Path(paths["workdir"]).expanduser()
        artifacts = Path(paths["artifacts"]).expanduser()
        temp_dump = Path(paths["temp_dump"]).expanduser()
    except KeyError as exc:
        raise ConfigError(f"Missing paths.{exc.args[0]}") from exc
    return PathsConfig(workdir=workdir, artifacts=artifacts, temp_dump=temp_dump)


def _load_secrets(data: dict) -> SecretsConfig:
    secrets = data.get("secrets") or {}
    api_token = secrets.get("api_token")
    if not api_token:
        raise ConfigError("secrets.api_token is required")
    return SecretsConfig(api_token=str(api_token))


def _load_app(data: dict) -> AppConfig:
    app_data = data.get("app") or {}
    port = app_data.get("port", 8000)
    port = _to_int(port, field="app.port")
    return AppConfig(port=port)


def load_config(path: Optional[os.PathLike[str] | str] = None) -> SidecarConfig:
    """Load sidecar configuration from disk, applying environment substitutions."""

    config_path = Path(path or os.environ.get(CONFIG_PATH_ENV, DEFAULT_CONFIG_PATH))
    if not config_path.exists():
        raise ConfigError(f"Configuration file not found: {config_path}")
    data = _load_yaml(config_path)
    return SidecarConfig(
        app=_load_app(data),
        capabilities=_load_capabilities(data),
        strategy=_load_strategy(data),
        paths=_load_paths(data),
        secrets=_load_secrets(data),
    )


def _validate_token(provided: str, expected: str) -> None:
    if not provided:
        raise UnauthorizedError("Missing authorization token")
    if not hmac.compare_digest(provided, expected):
        raise UnauthorizedError("Invalid authorization token")


def _extract_bearer_token(header_value: Optional[str]) -> str:
    if not header_value or " " not in header_value:
        raise UnauthorizedError("Missing Bearer token")
    scheme, token = header_value.split(" ", 1)
    if scheme.lower() != "bearer" or not token.strip():
        raise UnauthorizedError("Missing Bearer token")
    return token.strip()


def _execute_strategy(
    config: SidecarConfig, drive_folder_id: Optional[str]
) -> tuple[ArtifactMetadata, BackupStrategy]:
    strategy = create_strategy(config.strategy, config.paths)
    metadata = strategy.prepare(drive_folder_id=drive_folder_id)
    return metadata, strategy


def create_app(*, config: Optional[SidecarConfig] = None, config_path: Optional[os.PathLike[str] | str] = None) -> Flask:
    """Factory returning a configured Flask application."""

    if config is None:
        config = load_config(config_path)
    app = Flask(__name__)
    app.config["SIDECAR_CONFIG"] = config

    @app.errorhandler(UnauthorizedError)
    def _handle_unauthorized(error: UnauthorizedError):
        return jsonify({"error": str(error)}), 401

    @app.errorhandler(StrategyExecutionError)
    def _handle_strategy_error(error: StrategyExecutionError):
        return jsonify({"error": str(error)}), 500

    @app.errorhandler(ConfigError)
    def _handle_config_error(error: ConfigError):
        return jsonify({"error": str(error)}), 500

    def _require_token() -> str:
        header = request.headers.get("Authorization")
        token = _extract_bearer_token(header)
        expected = app.config["SIDECAR_CONFIG"].secrets.api_token
        _validate_token(token, expected)
        return token

    @app.route("/backup/capabilities", methods=["GET"])
    def capabilities() -> Response:
        _require_token()
        config = app.config["SIDECAR_CONFIG"]
        payload = {
            "version": config.capabilities.version,
            "types": config.capabilities.types,
        }
        if config.capabilities.est_seconds is not None:
            payload["est_seconds"] = config.capabilities.est_seconds
        if config.capabilities.est_size is not None:
            payload["est_size"] = config.capabilities.est_size
        return jsonify(payload)

    @app.route("/backup/export", methods=["POST"])
    def export() -> Response:
        _require_token()
        config = app.config["SIDECAR_CONFIG"]
        drive_folder_id = request.args.get("drive_folder_id")
        metadata, strategy = _execute_strategy(config, drive_folder_id)

        def generate():
            try:
                for chunk in strategy.stream():
                    if chunk:
                        yield chunk
            finally:
                try:
                    strategy.cleanup()
                except Exception:
                    pass

        response = Response(stream_with_context(generate()), mimetype=metadata.content_type)
        response.headers["Content-Disposition"] = f'attachment; filename="{metadata.filename}"'
        if metadata.size is not None:
            response.headers["Content-Length"] = str(metadata.size)
        response.headers["X-Backup-Format"] = metadata.format
        if metadata.checksum is not None:
            response.headers["X-Checksum-Sha256"] = metadata.checksum
        if drive_folder_id:
            response.headers["X-Drive-Folder-Id"] = drive_folder_id
        return response

    return app


def main() -> None:
    config = load_config()
    app = create_app(config=config)
    app.run(host="0.0.0.0", port=config.app.port)


if __name__ == "__main__":
    main()
