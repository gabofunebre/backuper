"""Sidecar backup service exposing capabilities and export endpoints."""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Optional

import yaml
from flask import Flask, Response, jsonify, request, stream_with_context

CONFIG_PATH_ENV = "SIDECAR_CONFIG_PATH"
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "config.yaml"


class ConfigError(RuntimeError):
    """Raised when the configuration file is invalid."""


class UnauthorizedError(RuntimeError):
    """Raised when the provided token does not match the expected one."""


class StrategyExecutionError(RuntimeError):
    """Raised when the backup strategy fails to produce an artifact."""


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
class StrategyCommands:
    pre_backup: list[str]
    backup: list[str]
    post_backup: list[str]


@dataclass
class StrategyConfig:
    type: str
    commands: StrategyCommands
    artifact: StrategyArtifactConfig


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
class ArtifactMetadata:
    path: Path
    filename: str
    size: int
    checksum: str
    format: str
    content_type: str


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


def _ensure_list(value: Optional[Iterable[str]]) -> list[str]:
    if not value:
        return []
    return [str(item) for item in value]


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
    commands_section = strategy.get("commands") or {}
    pre = _ensure_list(commands_section.get("pre_backup"))
    backup = _ensure_list(commands_section.get("backup"))
    post = _ensure_list(commands_section.get("post_backup"))
    return StrategyConfig(
        type=str(strategy_type),
        commands=StrategyCommands(pre_backup=pre, backup=backup, post_backup=post),
        artifact=StrategyArtifactConfig(
            filename=filename,
            format=artifact_format,
            content_type=content_type,
        ),
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


def _run_commands(commands: Iterable[str], *, workdir: Path, env: dict[str, str]) -> None:
    for command in commands:
        if not command.strip():
            continue
        try:
            subprocess.run(
                command,
                shell=True,
                check=True,
                cwd=str(workdir),
                env=env,
            )
        except subprocess.CalledProcessError as exc:
            raise StrategyExecutionError(f"Command failed with exit code {exc.returncode}: {command}") from exc


def _compute_checksum(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _execute_strategy(config: SidecarConfig, drive_folder_id: Optional[str]) -> ArtifactMetadata:
    paths = config.paths
    paths.workdir.mkdir(parents=True, exist_ok=True)
    paths.artifacts.mkdir(parents=True, exist_ok=True)
    if paths.temp_dump.exists():
        paths.temp_dump.unlink()
    env = os.environ.copy()
    env.update(
        {
            "SIDE_CAR_WORKDIR": str(paths.workdir),
            "SIDE_CAR_ARTIFACTS_DIR": str(paths.artifacts),
            "SIDE_CAR_TEMP_DUMP": str(paths.temp_dump),
            "SIDE_CAR_STRATEGY": config.strategy.type,
        }
    )
    if drive_folder_id:
        env["SIDE_CAR_DRIVE_FOLDER_ID"] = drive_folder_id
    else:
        env.pop("SIDE_CAR_DRIVE_FOLDER_ID", None)
    _run_commands(config.strategy.commands.pre_backup, workdir=paths.workdir, env=env)
    _run_commands(config.strategy.commands.backup, workdir=paths.workdir, env=env)
    _run_commands(config.strategy.commands.post_backup, workdir=paths.workdir, env=env)
    if not paths.temp_dump.exists():
        raise StrategyExecutionError(f"Strategy did not generate expected artifact at {paths.temp_dump}")
    artifact_path = paths.artifacts / config.strategy.artifact.filename
    if artifact_path.exists():
        if artifact_path.is_dir():
            raise StrategyExecutionError(f"Artifact path points to a directory: {artifact_path}")
        artifact_path.unlink()
    try:
        shutil.move(str(paths.temp_dump), artifact_path)
    except OSError as exc:
        raise StrategyExecutionError(f"Unable to move artifact to {artifact_path}: {exc}") from exc
    checksum, size = _compute_checksum(artifact_path)
    return ArtifactMetadata(
        path=artifact_path,
        filename=config.strategy.artifact.filename,
        size=size,
        checksum=checksum,
        format=config.strategy.artifact.format,
        content_type=config.strategy.artifact.content_type,
    )


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
        metadata = _execute_strategy(config, drive_folder_id)

        def generate() -> Iterator[bytes]:
            try:
                with metadata.path.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(65536), b""):
                        if chunk:
                            yield chunk
            finally:
                try:
                    metadata.path.unlink()
                except OSError:
                    pass

        response = Response(stream_with_context(generate()), mimetype=metadata.content_type)
        response.headers["Content-Disposition"] = f'attachment; filename="{metadata.filename}"'
        response.headers["Content-Length"] = str(metadata.size)
        response.headers["X-Backup-Format"] = metadata.format
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
