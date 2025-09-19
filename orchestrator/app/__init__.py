import datetime
import json
import os
import posixpath
import shutil
import subprocess
import tempfile
import uuid
from functools import wraps
from flask import (
    Flask,
    render_template,
    request,
    jsonify,
    redirect,
    session,
    url_for,
)
from dotenv import load_dotenv
from sqlalchemy import inspect, text
from apscheduler.triggers.cron import CronTrigger
from dataclasses import dataclass
from typing import Callable

from .database import Base, SessionLocal, engine
from .models import App, RcloneRemote
from orchestrator.scheduler import (
    start as start_scheduler,
    schedule_app_backups,
    run_backup,
)
from orchestrator.services.client import _normalize_remote
from orchestrator.local_dirs import (
    parse_local_directory_config,
    strip_enclosing_quotes,
)


DEFAULT_RCLONE_CONFIG = "/config/rclone/rclone.conf"


class DefaultDriveRemoteError(RuntimeError):
    """Raised when the default Google Drive remote cannot be prepared."""

    pass


class DriveShareLinkError(Exception):
    """Raised when a public Google Drive link cannot be generated."""

    pass


def create_app() -> Flask:
    """Application factory for the backup orchestrator UI."""
    load_dotenv()
    app = Flask(__name__)
    Base.metadata.create_all(bind=engine)
    inspector = inspect(engine)
    columns = [col["name"] for col in inspector.get_columns("apps")]
    with engine.begin() as conn:
        if "drive_folder_id" not in columns:
            conn.execute(text("ALTER TABLE apps ADD COLUMN drive_folder_id VARCHAR"))
        if "rclone_remote" not in columns:
            conn.execute(text("ALTER TABLE apps ADD COLUMN rclone_remote VARCHAR"))
        if "retention" not in columns:
            conn.execute(text("ALTER TABLE apps ADD COLUMN retention INTEGER"))
    remote_columns = [col["name"] for col in inspector.get_columns("rclone_remotes")]
    with engine.begin() as conn:
        if "route" not in remote_columns:
            conn.execute(text("ALTER TABLE rclone_remotes ADD COLUMN route VARCHAR"))
        if "share_url" not in remote_columns:
            conn.execute(text("ALTER TABLE rclone_remotes ADD COLUMN share_url VARCHAR"))
        if "config" not in remote_columns:
            conn.execute(text("ALTER TABLE rclone_remotes ADD COLUMN config TEXT"))
        if "created_at" not in remote_columns:
            conn.execute(text("ALTER TABLE rclone_remotes ADD COLUMN created_at DATETIME"))
    start_scheduler()

    @dataclass
    class RemotePlan:
        command: list[str]
        pre_commands: list[list[str]]
        post_commands: list[list[str]]
        cleanup_on_error: bool = False
        error_translator: Callable[[str], str] | None = None
        drive_mode: str | None = None
        drive_remote_path: str | None = None
        drive_current_path: str | None = None
        drive_requires_creation: bool = False
        share_url: str | None = None
        local_target_path: str | None = None
        local_source_path: str | None = None
        local_base_path: str | None = None
        local_move_mode: str | None = None
        config_snapshot: dict[str, str] | None = None

    class RemoteOperationError(Exception):
        """Raised when a remote operation fails for a known reason."""

        def __init__(self, message: str, status_code: int = 400) -> None:
            super().__init__(message)
            self.status_code = status_code

    admin_user = os.getenv("APP_ADMIN_USER")
    admin_pass = os.getenv("APP_ADMIN_PASS")
    app.secret_key = os.getenv("APP_SECRET_KEY", "devkey")

    def get_local_directories() -> list[dict[str, str]]:
        return parse_local_directory_config(os.getenv("RCLONE_LOCAL_DIRECTORIES", ""))

    def _ensure_absolute_path(value: str | None) -> str | None:
        if value is None:
            return None
        candidate = strip_enclosing_quotes(value)
        if not candidate:
            return None
        candidate = os.path.expanduser(candidate)
        if not candidate:
            return None
        return os.path.abspath(candidate)

    def _normalize_filesystem_path(path: str | None) -> str:
        candidate = _ensure_absolute_path(path)
        if not candidate:
            return ""
        if candidate != os.sep:
            candidate = candidate.rstrip(os.sep)
        return os.path.normcase(candidate)

    def _format_local_error(action: str, path: str, exc: OSError) -> str:
        base = f"No se pudo {action} la carpeta \"{path}\"."
        if isinstance(exc, FileExistsError):
            return f"{base} Ya existe un archivo o carpeta con ese nombre."
        if isinstance(exc, FileNotFoundError):
            return f"{base} No se encontró la ruta especificada."
        if isinstance(exc, PermissionError):
            return (
                f"{base} El sistema denegó el acceso. Verificá los permisos en el servidor."
            )
        detail = (exc.strerror or str(exc) or "").strip()
        if detail:
            return f"{base} {detail}"
        return f"{base} Revisá los permisos en el servidor."

    def _get_local_directory_roots() -> set[str]:
        roots: set[str] = set()
        for entry in get_local_directories():
            raw_path = (entry.get("path") or "") if isinstance(entry, dict) else ""
            candidate = _ensure_absolute_path(raw_path)
            if not candidate:
                continue
            normalized = _normalize_filesystem_path(candidate)
            if normalized:
                roots.add(normalized)
        return roots

    def _rollback_local_changes(
        move_mode: str | None,
        target_path: str | None,
        source_path: str | None,
        moved_entries: list[str],
        created_path: str | None,
    ) -> list[str]:
        errors: list[str] = []
        if move_mode == "rename" and target_path and source_path:
            try:
                shutil.move(target_path, source_path)
            except OSError as exc:
                errors.append(_format_local_error("restaurar", source_path, exc))
        elif move_mode == "move_contents" and target_path and source_path:
            for entry in moved_entries:
                src = os.path.join(target_path, entry)
                dst = os.path.join(source_path, entry)
                try:
                    shutil.move(src, dst)
                except OSError as exc:
                    errors.append(_format_local_error("restaurar", dst, exc))
            if created_path:
                try:
                    shutil.rmtree(created_path)
                except OSError as exc:
                    errors.append(_format_local_error("eliminar", created_path, exc))
                created_path = None
        if created_path and move_mode != "move_contents":
            try:
                shutil.rmtree(created_path)
            except OSError as exc:
                errors.append(_format_local_error("eliminar", created_path, exc))
        return errors

    def _normalize_remote_name(value: str | None) -> str:
        cleaned = (value or "").strip()
        if not cleaned:
            return ""
        return _normalize_remote(cleaned).rstrip(":")

    def _normalize_drive_folder_name(value: str | None) -> str:
        cleaned = (value or "").strip()
        if not cleaned:
            return ""
        cleaned = cleaned.replace(":", " ")
        cleaned = cleaned.replace("\\", "/")
        return cleaned.strip().strip("/")

    def _normalize_drive_path(path: str | None) -> str:
        candidate = (path or "").strip()
        if not candidate:
            return ""
        candidate = candidate.replace("\\", "/")
        remote, sep, suffix = candidate.partition(":")
        if not sep:
            base_remote = _normalize_remote(os.getenv("RCLONE_REMOTE", "gdrive"))
            cleaned_suffix = candidate.strip("/")
            if cleaned_suffix:
                return f"{base_remote}{cleaned_suffix}"
            return base_remote
        normalized_remote = _normalize_remote(remote)
        cleaned_suffix = suffix.strip().strip("/")
        if cleaned_suffix:
            return f"{normalized_remote}{cleaned_suffix}"
        return normalized_remote

    def _collect_drive_root_entries(remote: str) -> set[str]:
        entries: set[str] = set()
        base_args = ["lsf", remote, "--max-depth", "1"]
        for flag in ("--dirs-only", "--files-only"):
            try:
                result = run_rclone(
                    [*base_args, flag], capture_output=True, text=True, check=True
                )
            except subprocess.CalledProcessError as exc:
                message = (exc.stderr or exc.stdout or "").strip()
                detail = (
                    message
                    or "No se pudo verificar si la carpeta ya existe en Google Drive."
                )
                raise RemoteOperationError(detail) from exc
            for raw in (result.stdout or "").splitlines():
                item = raw.strip().rstrip("/")
                if item:
                    entries.add(item.lower())
        return entries

    def _ensure_drive_folder_available(
        base_remote: str,
        folder_name: str,
        current_path: str | None = None,
    ) -> None:
        normalized_base = _normalize_remote(base_remote.rstrip(":"))
        normalized_name = _normalize_drive_folder_name(folder_name)
        if not normalized_name:
            raise RemoteOperationError("El nombre de la carpeta es obligatorio.")
        target_path = _normalize_drive_path(f"{normalized_base}{normalized_name}")
        current_normalized = _normalize_drive_path(current_path)
        if current_normalized and current_normalized == target_path:
            return
        existing = _collect_drive_root_entries(normalized_base)
        if normalized_name.lower() in existing:
            raise RemoteOperationError(
                "Ya existe una carpeta o archivo con ese nombre en Google Drive. "
                "Elegí otro nombre."
            )

    def _build_drive_temp_path(path: str) -> str:
        normalized = _normalize_drive_path(path)
        remote, sep, _ = normalized.partition(":")
        if not sep:
            raise ValueError("invalid drive path")
        base_remote = _normalize_remote(remote)
        return f"{base_remote}__rollback__{uuid.uuid4().hex[:8]}"

    def _move_drive_path(source: str, target: str) -> None:
        try:
            run_rclone(
                ["moveto", source, target],
                capture_output=True,
                text=True,
                check=True,
            )
        except subprocess.CalledProcessError as exc:
            message = (exc.stderr or exc.stdout or "").strip()
            detail = (
                message
                or "No se pudo actualizar la carpeta en Google Drive. Intentá con otro nombre."
            )
            raise RemoteOperationError(detail) from exc

    def _restore_drive_path(source: str, target: str) -> str | None:
        try:
            run_rclone(
                ["moveto", source, target],
                capture_output=True,
                text=True,
                check=True,
            )
        except subprocess.CalledProcessError as exc:
            message = (exc.stderr or exc.stdout or "").strip()
            detail = (
                message
                or "No se pudo restaurar la carpeta original en Google Drive."
            )
            return detail
        except RemoteOperationError as exc:
            return str(exc)
        return None

    def _purge_drive_path(target: str) -> None:
        try:
            run_rclone(
                ["purge", target],
                capture_output=True,
                text=True,
                check=True,
            )
        except subprocess.CalledProcessError as exc:
            message = (exc.stderr or exc.stdout or "").strip()
            detail = (
                message
                or "No se pudo eliminar la carpeta de Google Drive asociada al remote."
            )
            raise RemoteOperationError(detail) from exc

    def _normalize_sftp_base_path(value: str | None) -> str:
        candidate = (value or "/").strip()
        if not candidate or candidate in {".", "./"}:
            return "/"
        candidate = candidate.replace("\\", "/")
        if not candidate.startswith("/"):
            candidate = f"/{candidate}"
        while "//" in candidate:
            candidate = candidate.replace("//", "/")
        if len(candidate) > 1 and candidate.endswith("/"):
            candidate = candidate.rstrip("/")
        return candidate or "/"

    def _join_sftp_folder(base_path: str, folder: str) -> str:
        safe_folder = (folder or "").strip().strip("/")
        if not safe_folder:
            raise ValueError("invalid folder name")
        normalized_base = _normalize_sftp_base_path(base_path)
        if normalized_base == "/":
            return f"/{safe_folder}"
        return f"{normalized_base}/{safe_folder}"

    def _parent_sftp_path(path: str | None) -> str:
        normalized = _normalize_sftp_base_path(path)
        if normalized == "/":
            return "/"
        parent = posixpath.dirname(normalized)
        if not parent:
            return "/"
        return parent

    def _translate_sftp_error(message: str) -> str:
        text = (message or "").strip()
        if not text:
            return "No se pudo completar la operación con el servidor SFTP."
        lowered = text.lower()
        if "permission denied" in lowered:
            return (
                "El usuario SFTP no tiene permisos suficientes en esa carpeta. "
                "Probá con otra ubicación o ajustá los permisos en el servidor."
            )
        if "authentication" in lowered or "access denied" in lowered or "auth failed" in lowered:
            return "No se pudo autenticar en el servidor SFTP. Verificá el usuario y la contraseña."
        if "no such host" in lowered or "name or service not known" in lowered or "could not resolve" in lowered:
            return "No se pudo resolver el host del servidor SFTP. Revisá la dirección ingresada."
        if "connection refused" in lowered or "connection timed out" in lowered or "network is unreachable" in lowered:
            return "No fue posible conectarse al servidor SFTP. Asegurate de que esté en línea y accesible."
        return text

    def _build_remote_plan(
        name: str,
        remote_type: str,
        settings: dict,
        *,
        current_remote_type: str | None = None,
        current_remote_route: str | None = None,
    ) -> RemotePlan:
        base_args = ["config", "create", "--non-interactive", name]
        normalized_type = (remote_type or "").strip().lower()
        plan = RemotePlan(command=[], pre_commands=[], post_commands=[])

        if normalized_type == "drive":
            mode = (settings.get("mode") or "").strip().lower()
            token = (settings.get("token") or "").strip()
            if not mode:
                mode = "custom" if token else "shared"
            if mode not in {"shared", "custom"}:
                raise RemoteOperationError("invalid drive mode")
            if mode == "shared":
                base_remote = _normalize_remote(os.getenv("RCLONE_REMOTE", "gdrive"))
                folder_name = _normalize_drive_folder_name(
                    settings.get("folder_name") or name or ""
                )
                if not folder_name:
                    raise RemoteOperationError("folder name is required")
                drive_remote_path = _normalize_drive_path(
                    f"{base_remote}{folder_name}"
                )
                current_drive_path = _normalize_drive_path(current_remote_route)
                ensure_default_drive_remote()
                if drive_remote_path != current_drive_path:
                    _ensure_drive_folder_available(
                        base_remote, folder_name, current_drive_path
                    )
                    plan.pre_commands.append(["mkdir", drive_remote_path])
                    plan.drive_requires_creation = True
                else:
                    plan.drive_requires_creation = False
                plan.drive_current_path = current_drive_path or None
                plan.command = [
                    *base_args,
                    "alias",
                    "remote",
                    drive_remote_path,
                ]
                plan.cleanup_on_error = True
                plan.drive_mode = "shared"
                plan.drive_remote_path = drive_remote_path
                plan.config_snapshot = {
                    "type": "alias",
                    "remote": drive_remote_path,
                }
            else:
                if not token:
                    raise RemoteOperationError("token is required")
                plan.command = [
                    *base_args,
                    "drive",
                    "token",
                    token,
                    "scope",
                    os.getenv("RCLONE_DRIVE_SCOPE", "drive"),
                    "--no-auto-auth",
                ]
                client_id = (settings.get("client_id") or "").strip() or os.getenv(
                    "RCLONE_DRIVE_CLIENT_ID"
                )
                client_secret = (
                    (settings.get("client_secret") or "").strip()
                    or os.getenv("RCLONE_DRIVE_CLIENT_SECRET")
                )
                if client_id:
                    plan.command.extend(["client_id", client_id])
                if client_secret:
                    plan.command.extend(["client_secret", client_secret])
                config_snapshot: dict[str, str] = {
                    "type": "drive",
                    "token": token,
                    "scope": os.getenv("RCLONE_DRIVE_SCOPE", "drive"),
                }
                if client_id:
                    config_snapshot["client_id"] = client_id
                if client_secret:
                    config_snapshot["client_secret"] = client_secret
                plan.config_snapshot = config_snapshot
        elif normalized_type == "local":
            directory_entries = get_local_directories()
            if not directory_entries:
                raise RemoteOperationError("no local directories configured", 500)
            raw_path_setting = settings.get("path")
            if isinstance(raw_path_setting, str):
                path = strip_enclosing_quotes(raw_path_setting)
            else:
                path = strip_enclosing_quotes(str(raw_path_setting or ""))
            if not path:
                raise RemoteOperationError(
                    "Seleccioná la carpeta local donde guardar los respaldos."
                )
            available_paths = {
                strip_enclosing_quotes((entry.get("path") or ""))
                for entry in directory_entries
            }
            if path not in available_paths:
                raise RemoteOperationError("invalid path")
            base_path = _ensure_absolute_path(path)
            if not base_path or not os.path.isdir(base_path):
                raise RemoteOperationError(
                    "La carpeta seleccionada no existe o no es accesible desde el servidor."
                )
            safe_name = name.strip()
            if not safe_name:
                raise RemoteOperationError(
                    "El nombre del remote no es válido para crear la carpeta local."
                )
            for separator in (os.sep, os.altsep):
                if separator and separator in safe_name:
                    raise RemoteOperationError(
                        "El nombre del remote no puede contener separadores de carpeta."
                    )
            if safe_name in {".", ".."}:
                raise RemoteOperationError(
                    "Elegí otro nombre para la carpeta del remote."
                )
            target_path = os.path.abspath(os.path.join(base_path, safe_name))
            try:
                common_root = os.path.commonpath([base_path, target_path])
            except ValueError as exc:
                raise RemoteOperationError(
                    "El nombre del remote genera una ruta inválida dentro de la carpeta seleccionada."
                ) from exc
            if os.path.normcase(common_root) != os.path.normcase(base_path):
                raise RemoteOperationError(
                    "El nombre del remote genera una ruta inválida dentro de la carpeta seleccionada."
                )
            normalized_target = _normalize_filesystem_path(target_path)
            normalized_base = _normalize_filesystem_path(base_path)
            existing_path: str | None = None
            move_mode: str | None = None
            if (current_remote_type or "").strip().lower() == "local" and current_remote_route:
                candidate_existing = _ensure_absolute_path(current_remote_route)
                if candidate_existing:
                    normalized_existing = _normalize_filesystem_path(candidate_existing)
                    if normalized_existing == normalized_target:
                        existing_path = candidate_existing
                    elif normalized_existing == normalized_base:
                        existing_path = candidate_existing
                        move_mode = "move_contents"
                    else:
                        existing_path = candidate_existing
                        move_mode = "rename"
            if os.path.exists(target_path):
                normalized_existing = (
                    _normalize_filesystem_path(existing_path) if existing_path else ""
                )
                if normalized_existing != normalized_target:
                    raise RemoteOperationError(
                        "Ya existe una carpeta con ese nombre en la ruta seleccionada. Elegí otro nombre."
                    )
            plan.share_url = target_path
            plan.command = [*base_args, "alias", "remote", target_path]
            plan.local_target_path = target_path
            plan.local_source_path = existing_path
            plan.local_base_path = base_path
            plan.local_move_mode = move_mode
            plan.config_snapshot = {"type": "alias", "remote": target_path}
        elif normalized_type == "sftp":
            host = (settings.get("host") or "").strip()
            username = (settings.get("username") or settings.get("user") or "").strip()
            password = (settings.get("password") or "").strip()
            port = (settings.get("port") or "").strip()
            base_path = (settings.get("base_path") or "").strip()
            if not host:
                raise RemoteOperationError("host is required")
            if not username:
                raise RemoteOperationError("username is required")
            if not password:
                raise RemoteOperationError("password is required")
            if port and not port.isdigit():
                raise RemoteOperationError("invalid port")
            if not base_path:
                raise RemoteOperationError(
                    "Seleccioná la carpeta del servidor SFTP donde se crearán los respaldos."
                )
            normalized_base = _normalize_sftp_base_path(base_path)
            plan.share_url = normalized_base
            try:
                target_path = _join_sftp_folder(normalized_base, name)
            except ValueError:
                raise RemoteOperationError(
                    "El nombre del remote no es válido para crear una carpeta en SFTP."
                )
            obscured_password = _obscure_rclone_secret(password)

            plan.command = [
                *base_args,
                "sftp",
                "host",
                host,
                "user",
                username,
            ]
            if port:
                plan.command.extend(["port", port])
            plan.command.extend(["path", target_path, "pass", obscured_password])
            plan.post_commands = [["mkdir", f"{name}:"], ["lsd", f"{name}:"]]
            plan.cleanup_on_error = True
            plan.error_translator = _translate_sftp_error
            config_snapshot: dict[str, str] = {
                "type": "sftp",
                "host": host,
                "user": username,
                "path": target_path,
                "pass": obscured_password,
            }
            if port:
                config_snapshot["port"] = port
            plan.config_snapshot = config_snapshot
        elif normalized_type == "onedrive":
            raise RemoteOperationError("OneDrive aún está en construcción")
        else:
            raise RemoteOperationError("unsupported remote type")

        if not plan.command:
            raise RemoteOperationError("unsupported remote type")

        return plan

    def _execute_remote_plan(name: str, plan: RemotePlan) -> str | None:
        for extra in plan.pre_commands:
            try:
                run_rclone(extra, capture_output=True, text=True, check=True)
            except subprocess.CalledProcessError as exc:
                error = (exc.stderr or exc.stdout or "").strip() or "failed to prepare remote"
                raise RemoteOperationError(error) from exc

        try:
            run_rclone(plan.command, capture_output=True, text=True, check=True)
        except subprocess.CalledProcessError as exc:
            error = (exc.stderr or exc.stdout or "").strip() or "failed to create remote"
            if plan.error_translator:
                error = plan.error_translator(error)
            raise RemoteOperationError(error) from exc

        for extra in plan.post_commands:
            try:
                run_rclone(extra, capture_output=True, text=True, check=True)
            except subprocess.CalledProcessError as exc:
                if plan.cleanup_on_error:
                    try:
                        run_rclone(
                            ["config", "delete", name],
                            capture_output=True,
                            text=True,
                            check=True,
                        )
                    except Exception:
                        pass
                error = (exc.stderr or exc.stdout or "").strip() or "failed to create remote"
                if plan.error_translator:
                    error = plan.error_translator(error)
                raise RemoteOperationError(error) from exc

        share_url = plan.share_url
        if plan.drive_mode == "shared" and plan.drive_remote_path:
            try:
                share_url = _generate_drive_share_link(plan.drive_remote_path)
            except DriveShareLinkError as exc:
                if plan.cleanup_on_error:
                    try:
                        run_rclone(
                            ["config", "delete", name],
                            capture_output=True,
                            text=True,
                            check=True,
                        )
                    except Exception:
                        pass
                raise RemoteOperationError(str(exc), 500) from exc

        plan.share_url = share_url
        return share_url

    def _restore_remote_backup(remote_name: str, backup_name: str) -> bool:
        try:
            backup_config = _load_remote_configuration(backup_name)
        except (RemoteOperationError, RuntimeError):
            return False
        if not backup_config:
            return False
        try:
            run_rclone(
                ["config", "delete", remote_name],
                capture_output=True,
                text=True,
                check=True,
            )
        except subprocess.CalledProcessError:
            pass
        except RuntimeError:
            return False

        try:
            _apply_remote_configuration(remote_name, backup_config)
        except (RemoteOperationError, subprocess.CalledProcessError, RuntimeError):
            return False
        return True

    def _delete_remote_safely(remote_name: str) -> None:
        try:
            run_rclone(
                ["config", "delete", remote_name],
                capture_output=True,
                text=True,
                check=True,
            )
        except (subprocess.CalledProcessError, RuntimeError):
            pass

    def _load_remote_configuration(remote_name: str) -> dict[str, str] | None:
        normalized_name = (remote_name or "").strip()
        if not normalized_name:
            return None

        with SessionLocal() as db:
            stored = (
                db.query(RcloneRemote)
                .filter_by(name=normalized_name)
                .one_or_none()
            )
            if stored and (stored.config or "").strip():
                try:
                    payload = json.loads(stored.config or "{}")
                except json.JSONDecodeError:
                    payload = None
                if isinstance(payload, dict) and payload.get("type"):
                    config: dict[str, str] = {}
                    for key, value in payload.items():
                        if value is None:
                            continue
                        config[str(key)] = str(value)
                    return config

        try:
            result = run_rclone(
                ["config", "dump"],
                capture_output=True,
                text=True,
                check=True,
            )
        except subprocess.CalledProcessError as exc:
            message = (exc.stderr or exc.stdout or "").strip() or "No se pudo leer la configuración de rclone."
            raise RemoteOperationError(message, 500) from exc
        raw_output = (result.stdout or "").strip()
        try:
            payload = json.loads(raw_output or "{}")
        except json.JSONDecodeError as exc:
            raise RemoteOperationError(
                "No se pudo interpretar la configuración de rclone.",
                500,
            ) from exc
        entry = payload.get(normalized_name)
        if entry is None:
            return None
        if not isinstance(entry, dict):
            raise RemoteOperationError(
                "La configuración del remote tiene un formato desconocido.",
                500,
            )
        config: dict[str, str] = {}
        for key, value in entry.items():
            if value is None:
                continue
            config[str(key)] = str(value)
        return config

    def _apply_remote_configuration(remote_name: str, config: dict[str, str]) -> None:
        remote_type = (config.get("type") or "").strip()
        if not remote_type:
            raise RemoteOperationError(
                "No se pudo determinar el tipo del remote en la copia de seguridad.",
                500,
            )
        args: list[str] = [
            "config",
            "create",
            "--non-interactive",
            remote_name,
            remote_type,
        ]
        for key, value in config.items():
            if key in {"type", "name"}:
                continue
            args.extend([key, str(value)])
        run_rclone(args, capture_output=True, text=True, check=True)

    def _clone_remote_configuration(
        source_name: str,
        target_name: str,
        *,
        error_message: str | None = None,
    ) -> None:
        config = _load_remote_configuration(source_name)
        if config is None:
            raise RemoteOperationError(
                error_message or "No se pudo replicar la configuración del remote.",
            )
        _apply_remote_configuration(target_name, config)

    def login_required(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            if session.get("logged_in"):
                return func(*args, **kwargs)
            accept = request.accept_mimetypes
            if accept["application/json"] >= accept["text/html"]:
                return {"error": "unauthorized"}, 401
            return redirect(url_for("login"))

        return wrapper

    @app.route("/login", methods=["GET", "POST"])
    def login():
        error = None
        if request.method == "POST":
            username = request.form.get("username")
            password = request.form.get("password")
            if username == admin_user and password == admin_pass:
                session["logged_in"] = True
                return redirect(url_for("index"))
            error = "invalid credentials"
        return render_template("login.html", error=error), (401 if error else 200)

    @app.get("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.route("/")
    @login_required
    def index() -> str:
        """Render main panel."""
        return render_template("index.html")

    @app.route("/rclone/config")
    @login_required
    def rclone_config() -> str:
        """Render rclone remote configuration page."""
        admin_email = (os.getenv("APP_ADMIN_EMAIL") or "").strip()
        return render_template("rclone_config.html", admin_email=admin_email)

    @app.route("/logs")
    @login_required
    def logs() -> str:
        """Display application logs."""
        path = os.getenv("APP_LOG_FILE", "app.log")
        try:
            with open(path, "r", encoding="utf-8") as fh:
                content = fh.read()
        except FileNotFoundError:
            content = ""
        return render_template("logs.html", logs=content)

    def run_rclone(args: list[str], **kwargs):
        """Execute an rclone command, raising RuntimeError if missing."""
        config_file = os.getenv("RCLONE_CONFIG", DEFAULT_RCLONE_CONFIG)
        supplied_config = any(
            arg == "--config" or arg.startswith("--config=") for arg in args
        )
        cmd = ["rclone"]
        if not supplied_config:
            config_dir = os.path.dirname(config_file)
            if config_dir:
                os.makedirs(config_dir, exist_ok=True)
            cmd.extend(["--config", config_file])
        cmd.extend(args)
        try:
            return subprocess.run(cmd, **kwargs)
        except FileNotFoundError as exc:
            raise RuntimeError("rclone is not installed") from exc
        except subprocess.CalledProcessError as exc:
            message = (exc.stderr or exc.stdout or "").lower()
            if (
                "--no-auto-auth" in cmd
                and "--no-auto-auth" in message
                and "unknown flag" in message
            ):
                cleaned_cmd = [part for part in cmd if part != "--no-auto-auth"]
                return subprocess.run(cleaned_cmd, **kwargs)
            raise

    def _obscure_rclone_secret(secret: str, *, config_path: str | None = None) -> str:
        """Return the obscured representation of *secret* using rclone."""

        args: list[str] = []
        if config_path:
            args.extend(["--config", config_path])
        args.extend(["obscure", secret])
        try:
            result = run_rclone(
                args, capture_output=True, text=True, check=True
            )
        except RuntimeError as exc:
            raise RemoteOperationError("rclone is not installed", 500) from exc
        except subprocess.CalledProcessError as exc:
            raise RemoteOperationError(
                "No se pudo cifrar la contraseña para rclone. Reintentá más tarde.",
                500,
            ) from exc

        obscured = (result.stdout or "").strip()
        if not obscured:
            raise RemoteOperationError(
                "No se pudo cifrar la contraseña para rclone. Reintentá más tarde.",
                500,
            )
        return obscured

    def _generate_drive_share_link(target: str) -> str:
        """Create or fetch a public sharing link for *target* in Google Drive."""

        commands = [["link", target, "--create-link"], ["link", target]]
        last_error: subprocess.CalledProcessError | None = None
        for command in commands:
            try:
                result = run_rclone(
                    command, capture_output=True, text=True, check=True
                )
            except subprocess.CalledProcessError as exc:
                error_text = (exc.stderr or exc.stdout or "").lower()
                if "--create-link" in command and "unknown flag" in error_text:
                    last_error = exc
                    continue
                raise DriveShareLinkError(
                    (exc.stderr or exc.stdout or "").strip()
                    or "No se pudo generar el enlace compartido de Google Drive."
                ) from exc
            output_lines = [
                line.strip()
                for line in (result.stdout or "").splitlines()
                if line.strip()
            ]
            if output_lines:
                return output_lines[0]
        if last_error is not None:
            raise DriveShareLinkError(
                (last_error.stderr or last_error.stdout or "").strip()
                or "No se pudo generar el enlace compartido de Google Drive."
            ) from last_error
        raise DriveShareLinkError(
            "No se pudo generar el enlace compartido de Google Drive."
        )

    def fetch_configured_remotes() -> list[str]:
        """Return the list of remotes configured in rclone."""

        result = run_rclone(
            ["listremotes"], capture_output=True, text=True, check=True
        )
        return [r.strip().rstrip(":") for r in result.stdout.splitlines() if r.strip()]

    def restore_persisted_remotes() -> None:
        """Ensure stored remotes exist in the local rclone configuration."""

        try:
            ensure_default_drive_remote()
        except (DefaultDriveRemoteError, RuntimeError):
            pass

        try:
            configured = set(fetch_configured_remotes())
        except RuntimeError:
            return

        with SessionLocal() as db:
            for remote in db.query(RcloneRemote).all():
                name = (remote.name or "").strip()
                if not name:
                    continue
                raw_config = (remote.config or "").strip()
                if not raw_config:
                    continue
                if name in configured:
                    continue
                try:
                    config_payload = json.loads(raw_config)
                except json.JSONDecodeError:
                    continue
                if not isinstance(config_payload, dict):
                    continue
                normalized_config: dict[str, str] = {}
                for key, value in config_payload.items():
                    if value is None:
                        continue
                    normalized_config[str(key)] = str(value)
                try:
                    _apply_remote_configuration(name, normalized_config)
                except (RemoteOperationError, RuntimeError, subprocess.CalledProcessError):
                    continue
                configured.add(name)

    def ensure_default_drive_remote() -> None:
        """Ensure the default Drive remote exists and matches environment settings."""

        base_remote = _normalize_remote(os.getenv("RCLONE_REMOTE", "gdrive"))
        remote_name = base_remote.rstrip(":")
        try:
            configured = fetch_configured_remotes()
        except subprocess.CalledProcessError as exc:
            error = (exc.stderr or exc.stdout or "").strip()
            message = (
                error or "No se pudieron listar los remotes configurados en rclone."
            )
            raise DefaultDriveRemoteError(message) from exc
        if remote_name in configured:
            return
        client_id = (os.getenv("RCLONE_DRIVE_CLIENT_ID") or "").strip()
        client_secret = (os.getenv("RCLONE_DRIVE_CLIENT_SECRET") or "").strip()
        token = (os.getenv("RCLONE_DRIVE_TOKEN") or "").strip()
        scope = os.getenv("RCLONE_DRIVE_SCOPE", "drive")
        if not client_id or not client_secret or not token:
            raise DefaultDriveRemoteError(
                "La cuenta global de Google Drive no está configurada. Revisá las "
                "variables RCLONE_DRIVE_CLIENT_ID, RCLONE_DRIVE_CLIENT_SECRET y "
                "RCLONE_DRIVE_TOKEN."
            )
        args = [
            "config",
            "create",
            "--non-interactive",
            remote_name,
            "drive",
            "token",
            token,
            "scope",
            scope,
            "--no-auto-auth",
        ]
        if client_id:
            args.extend(["client_id", client_id])
        if client_secret:
            args.extend(["client_secret", client_secret])
        try:
            run_rclone(args, capture_output=True, text=True, check=True)
        except subprocess.CalledProcessError as exc:
            error = (exc.stderr or exc.stdout or "").strip()
            message = (
                error
                or "No se pudo inicializar la cuenta global de Google Drive."
            )
            raise DefaultDriveRemoteError(message) from exc

    @app.get("/apps")
    @login_required
    def list_apps() -> list[dict]:
        """Return registered apps as JSON."""
        with SessionLocal() as db:
            apps = db.query(App).all()
            return jsonify([
                {
                    "id": a.id,
                    "name": a.name,
                    "url": a.url,
                    "token": a.token,
                    "schedule": a.schedule,
                    "drive_folder_id": a.drive_folder_id,
                    "rclone_remote": a.rclone_remote,
                    "retention": a.retention,
                }
                for a in apps
            ])

    @app.get("/rclone/remotes")
    @login_required
    def list_rclone_remotes() -> list[dict]:
        """Return available rclone remotes with stored metadata."""

        try:
            configured = set(fetch_configured_remotes())
        except RuntimeError:
            return {"error": "rclone is not installed"}, 500

        entries: list[dict[str, str]] = []
        with SessionLocal() as db:
            for remote in db.query(RcloneRemote).all():
                if remote.name not in configured:
                    continue
                item: dict[str, str] = {"name": remote.name}
                if remote.id is not None:
                    item["id"] = remote.id
                if remote.type:
                    item["type"] = remote.type
                route_value = (remote.route or "").strip()
                if route_value:
                    item["route"] = route_value
                share_value = (remote.share_url or "").strip()
                if share_value:
                    item["share_url"] = share_value
                if remote.created_at:
                    try:
                        item["created_at"] = remote.created_at.isoformat()
                    except AttributeError:
                        pass
                entries.append(item)

        entries.sort(key=lambda entry: entry["name"].lower())
        return jsonify(entries)

    @app.get("/rclone/remotes/options/<remote_type>")
    @login_required
    def remote_options(remote_type: str) -> tuple[dict, int] | dict:
        """Return UI options for the requested remote *remote_type*."""

        normalized = (remote_type or "").lower()
        if normalized == "local":
            return jsonify({"directories": get_local_directories()})
        if normalized == "sftp":
            return jsonify({"requires_credentials": True})
        if normalized == "drive":
            return jsonify({"supports_validation": True})
        if normalized == "onedrive":
            return jsonify({"status": "under_construction"})
        return {"error": "unsupported remote type"}, 400

    @app.post("/rclone/remotes/sftp/browse")
    @login_required
    def browse_sftp_directories() -> tuple[dict, int]:
        """Connect to an SFTP server and list directories for selection."""

        data = request.get_json(force=True) or {}
        host = (data.get("host") or "").strip()
        username = (data.get("username") or data.get("user") or "").strip()
        password = (data.get("password") or "").strip()
        port = (data.get("port") or "").strip()
        path = data.get("path")

        if not host:
            return {"error": "El host del servidor SFTP es obligatorio."}, 400
        if not username:
            return {"error": "Indicá el usuario del servidor SFTP."}, 400
        if not password:
            return {"error": "Ingresá la contraseña para conectarte por SFTP."}, 400
        if port and not port.isdigit():
            return {"error": "El puerto SFTP debe ser un número válido."}, 400

        normalized_path = _normalize_sftp_base_path(path)
        temp_path: str | None = None
        try:
            tmp = tempfile.NamedTemporaryFile(delete=False)
            temp_path = tmp.name
            tmp.close()
            args = [
                "--config",
                temp_path,
                "config",
                "create",
                "--non-interactive",
                "__probe__",
                "sftp",
                "host",
                host,
                "user",
                username,
            ]
            if port:
                args.extend(["port", port])
            obscured_password = _obscure_rclone_secret(password, config_path=temp_path)
            args.extend(["pass", obscured_password])
            run_rclone(args, capture_output=True, text=True, check=True)
            target = "__probe__:"
            if normalized_path != "/":
                target = f"__probe__:{normalized_path.lstrip('/')}"
            result = run_rclone(
                [
                    "--config",
                    temp_path,
                    "lsjson",
                    target,
                    "--dirs-only",
                ],
                capture_output=True,
                text=True,
                check=True,
            )
        except RemoteOperationError as exc:
            return {"error": str(exc)}, exc.status_code
        except RuntimeError:
            return {"error": "rclone is not installed"}, 500
        except subprocess.CalledProcessError as exc:
            message = (exc.stderr or exc.stdout or "").strip()
            return {"error": _translate_sftp_error(message)}, 400
        finally:
            if temp_path and os.path.exists(temp_path):
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass

        try:
            items = json.loads(result.stdout or "[]")
        except json.JSONDecodeError:
            return {"error": "No se pudieron interpretar las carpetas devueltas por el servidor SFTP."}, 502

        directories: list[dict[str, str]] = []
        for entry in items:
            name = (entry.get("Name") or "").strip()
            if not name:
                continue
            try:
                directories.append(
                    {"name": name, "path": _join_sftp_folder(normalized_path, name)}
                )
            except ValueError:
                continue
        directories.sort(key=lambda item: item["path"].lower())

        return (
            {
                "current_path": normalized_path,
                "parent_path": _parent_sftp_path(normalized_path),
                "directories": directories,
            },
            200,
        )

    @app.post("/rclone/remotes/drive/validate")
    @login_required
    def validate_drive_token() -> tuple[dict, int]:
        """Validate a Google Drive token without persisting configuration."""

        data = request.get_json(force=True) or {}
        token = (data.get("token") or "").strip()
        if not token:
            return {"error": "token is required"}, 400

        client_id = (data.get("client_id") or os.getenv("RCLONE_DRIVE_CLIENT_ID") or "").strip()
        client_secret = (
            data.get("client_secret")
            or os.getenv("RCLONE_DRIVE_CLIENT_SECRET")
            or ""
        ).strip()

        temp_path: str | None = None
        try:
            tmp = tempfile.NamedTemporaryFile(delete=False)
            temp_path = tmp.name
            tmp.close()
            args = [
                "--config",
                temp_path,
                "config",
                "create",
                "__validate__",
                "drive",
                "token",
                token,
                "scope",
                os.getenv("RCLONE_DRIVE_SCOPE", "drive"),
                "--no-auto-auth",
                "--non-interactive",
            ]
            if client_id:
                args.extend(["client_id", client_id])
            if client_secret:
                args.extend(["client_secret", client_secret])
            run_rclone(args, capture_output=True, text=True, check=True)
        except RuntimeError:
            return {"error": "rclone is not installed"}, 500
        except subprocess.CalledProcessError as exc:
            error = (exc.stderr or exc.stdout or "").strip() or "failed to validate token"
            return {"error": error}, 400
        finally:
            if temp_path and os.path.exists(temp_path):
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass
        return {"status": "ok"}, 200

    @app.post("/rclone/remotes")
    @login_required
    def create_rclone_remote() -> tuple[dict, int]:
        """Create a new rclone remote."""

        data = request.get_json(force=True) or {}
        name = _normalize_remote_name(data.get("name"))
        remote_type = (data.get("type") or "").strip().lower()
        if not name or not remote_type:
            return {"error": "invalid payload"}, 400
        allowed_types = {"drive", "onedrive", "sftp", "local"}
        if remote_type not in allowed_types:
            return {"error": "unsupported remote type"}, 400

        settings = data.get("settings") or {}

        try:
            configured = set(fetch_configured_remotes())
        except RuntimeError:
            return {"error": "rclone is not installed"}, 500
        if name in configured:
            return {"error": "Ya existe un remote con ese nombre."}, 400

        with SessionLocal() as db:
            conflict = db.query(RcloneRemote).filter_by(name=name).one_or_none()
            if conflict:
                return {"error": "Ya existe un remote con ese nombre."}, 400

        try:
            plan = _build_remote_plan(name, remote_type, settings)
        except RemoteOperationError as exc:
            return {"error": str(exc)}, exc.status_code
        except DefaultDriveRemoteError as exc:
            return {"error": str(exc)}, 500

        local_created_path: str | None = None
        config_payload: str | None = None
        remote_created = False
        try:
            local_target_path = getattr(plan, "local_target_path", None)
            local_source_path = getattr(plan, "local_source_path", None)
            if local_target_path and not local_source_path:
                try:
                    os.makedirs(local_target_path, exist_ok=False)
                except OSError as exc:
                    return {"error": _format_local_error("crear", local_target_path, exc)}, 400
                local_created_path = local_target_path
            share_url = _execute_remote_plan(name, plan)
            remote_created = True
            snapshot = plan.config_snapshot or {}
            if not snapshot or not snapshot.get("type"):
                raise RemoteOperationError(
                    "No se pudo guardar la configuración del remote.",
                    500,
                )
            config_payload = json.dumps(snapshot, ensure_ascii=False)
        except RemoteOperationError as exc:
            if remote_created:
                _delete_remote_safely(name)
            if local_created_path:
                shutil.rmtree(local_created_path, ignore_errors=True)
            return {"error": str(exc)}, exc.status_code
        except RuntimeError:
            if remote_created:
                _delete_remote_safely(name)
            if local_created_path:
                shutil.rmtree(local_created_path, ignore_errors=True)
            return {"error": "rclone is not installed"}, 500

        route_value = (
            getattr(plan, "drive_remote_path", None)
            or getattr(plan, "local_target_path", None)
            or (plan.share_url or None)
            or share_url
        )

        with SessionLocal() as db:
            new_remote = RcloneRemote(
                name=name,
                type=remote_type,
                route=(route_value or None),
                share_url=(share_url or None),
                config=config_payload,
                created_at=datetime.datetime.utcnow(),
            )
            db.add(new_remote)
            db.commit()
            db.refresh(new_remote)

        response: dict[str, str] = {"status": "ok", "name": name, "id": new_remote.id}
        if route_value:
            response["route"] = route_value
        if share_url:
            response["share_url"] = share_url
        return response, 201

    @app.put("/rclone/remotes/<remote_name>")
    @login_required
    def update_rclone_remote(remote_name: str) -> tuple[dict, int]:
        """Update an existing rclone remote."""

        data = request.get_json(force=True) or {}
        normalized_name = _normalize_remote_name(remote_name)
        remote_type = (data.get("type") or "").strip().lower()
        requested_name = (data.get("name") or "").strip()
        target_name = _normalize_remote_name(requested_name or remote_name)
        if not normalized_name:
            return {"error": "remote not found"}, 404
        if not remote_type:
            return {"error": "invalid payload"}, 400
        if not target_name:
            return {"error": "Completá un nombre válido para el remote."}, 400

        allowed_types = {"drive", "onedrive", "sftp", "local"}
        if remote_type not in allowed_types:
            return {"error": "unsupported remote type"}, 400

        settings = data.get("settings") or {}
        try:
            configured = fetch_configured_remotes()
        except RuntimeError:
            return {"error": "rclone is not installed"}, 500
        if normalized_name not in configured:
            return {"error": "remote not found"}, 404
        if target_name != normalized_name and target_name in configured:
            return {"error": "Ya existe un remote con ese nombre."}, 400

        stored_type: str | None = None
        stored_route: str | None = None
        with SessionLocal() as db:
            stored_remote = db.query(RcloneRemote).filter_by(name=normalized_name).one_or_none()
            if stored_remote:
                stored_type = (stored_remote.type or "").strip().lower() or None
                stored_route = (stored_remote.route or "").strip() or None

        try:
            plan = _build_remote_plan(
                target_name,
                remote_type,
                settings,
                current_remote_type=stored_type,
                current_remote_route=stored_route,
            )
        except RemoteOperationError as exc:
            return {"error": str(exc)}, exc.status_code
        except DefaultDriveRemoteError as exc:
            return {"error": str(exc)}, 500

        drive_current_path = _normalize_drive_path(stored_route)
        drive_target_path = _normalize_drive_path(getattr(plan, "drive_remote_path", None))
        drive_mode = getattr(plan, "drive_mode", None)
        drive_renamed = False
        drive_original_path_for_rollback = drive_current_path or None

        allowed_roots = _get_local_directory_roots()
        local_target_path = getattr(plan, "local_target_path", None)
        local_source_path = getattr(plan, "local_source_path", None)
        move_mode = getattr(plan, "local_move_mode", None)
        created_local_path: str | None = None
        renamed_source_path: str | None = None
        move_contents_source: str | None = None
        moved_entries: list[str] = []

        if local_target_path:
            target_parent = _normalize_filesystem_path(
                os.path.dirname(local_target_path.rstrip(os.sep)) or os.sep
            )
            if target_parent and target_parent not in allowed_roots:
                return {"error": "La carpeta seleccionada no forma parte de las rutas permitidas."}, 400
            if move_mode == "rename" and local_source_path:
                source_parent = _normalize_filesystem_path(
                    os.path.dirname(local_source_path.rstrip(os.sep)) or os.sep
                )
                if source_parent and source_parent not in allowed_roots:
                    return {"error": "La carpeta actual del remote no forma parte de las rutas permitidas."}, 400
            if move_mode == "move_contents" and local_source_path:
                source_base = _normalize_filesystem_path(local_source_path)
                if source_base and source_base not in allowed_roots:
                    return {"error": "La carpeta actual del remote no forma parte de las rutas permitidas."}, 400

        backup_name = f"__backup__{uuid.uuid4().hex[:8]}"
        try:
            _clone_remote_configuration(
                normalized_name,
                backup_name,
                error_message="No se pudo preparar la edición del remote.",
            )
        except RuntimeError:
            return {"error": "rclone is not installed"}, 500
        except RemoteOperationError as exc:
            _delete_remote_safely(backup_name)
            return {"error": str(exc)}, exc.status_code
        except subprocess.CalledProcessError as exc:
            message = (exc.stderr or exc.stdout or "").strip() or "No se pudo preparar la edición del remote."
            _delete_remote_safely(backup_name)
            return {"error": message}, 400

        try:
            if local_target_path:
                if move_mode == "move_contents":
                    if not local_source_path or not os.path.isdir(local_source_path):
                        _delete_remote_safely(backup_name)
                        return {
                            "error": "No encontramos la carpeta actual del remote en el servidor. Verificá que siga existiendo."
                        }, 400
                    try:
                        os.makedirs(local_target_path, exist_ok=False)
                    except OSError as exc:
                        _delete_remote_safely(backup_name)
                        return {"error": _format_local_error("crear", local_target_path, exc)}, 400
                    created_local_path = local_target_path
                    move_contents_source = local_source_path
                    for entry in os.listdir(local_source_path):
                        source_entry = os.path.join(local_source_path, entry)
                        target_entry = os.path.join(local_target_path, entry)
                        try:
                            shutil.move(source_entry, target_entry)
                        except OSError as exc:
                            revert_errors = _rollback_local_changes(
                                move_mode,
                                local_target_path,
                                move_contents_source,
                                moved_entries,
                                created_local_path,
                            )
                            _delete_remote_safely(backup_name)
                            message = _format_local_error("mover", target_entry, exc)
                            if revert_errors:
                                message = f"{message} {' '.join(revert_errors)}"
                            return {"error": message}, 400
                        moved_entries.append(entry)
                elif move_mode == "rename":
                    if not local_source_path or not os.path.exists(local_source_path):
                        _delete_remote_safely(backup_name)
                        return {
                            "error": "No se encontró la carpeta actual del remote. Verificá que siga existiendo."
                        }, 400
                    try:
                        shutil.move(local_source_path, local_target_path)
                    except OSError as exc:
                        _delete_remote_safely(backup_name)
                        return {"error": _format_local_error("mover", local_target_path, exc)}, 400
                    renamed_source_path = local_source_path
                elif not local_source_path:
                    try:
                        os.makedirs(local_target_path, exist_ok=False)
                    except OSError as exc:
                        _delete_remote_safely(backup_name)
                        return {"error": _format_local_error("crear", local_target_path, exc)}, 400
                    created_local_path = local_target_path

            if remote_type == "drive" and drive_mode == "shared":
                if not drive_target_path:
                    revert_errors = _rollback_local_changes(
                        move_mode,
                        local_target_path,
                        move_contents_source if move_mode == "move_contents" else renamed_source_path,
                        moved_entries,
                        created_local_path,
                    )
                    _delete_remote_safely(backup_name)
                    message = (
                        "No se pudo determinar la carpeta destino en Google Drive para actualizar el remote."
                    )
                    if revert_errors:
                        message = f"{message} {' '.join(revert_errors)}"
                    return {"error": message}, 400
                if not drive_current_path:
                    revert_errors = _rollback_local_changes(
                        move_mode,
                        local_target_path,
                        move_contents_source if move_mode == "move_contents" else renamed_source_path,
                        moved_entries,
                        created_local_path,
                    )
                    _delete_remote_safely(backup_name)
                    message = (
                        "No se encontró la carpeta actual del remote en Google Drive. "
                        "Verificá que siga existiendo antes de cambiar el nombre."
                    )
                    if revert_errors:
                        message = f"{message} {' '.join(revert_errors)}"
                    return {"error": message}, 400
                normalized_target = _normalize_drive_path(drive_target_path)
                normalized_current = _normalize_drive_path(drive_current_path)
                if normalized_target and normalized_current and normalized_target != normalized_current:
                    plan.pre_commands = [
                        cmd
                        for cmd in plan.pre_commands
                        if not (
                            len(cmd) >= 2
                            and cmd[0] == "mkdir"
                            and _normalize_drive_path(cmd[1]) == normalized_target
                        )
                    ]
                    try:
                        _move_drive_path(normalized_current, normalized_target)
                    except RemoteOperationError as exc:
                        revert_errors = _rollback_local_changes(
                            move_mode,
                            local_target_path,
                            move_contents_source if move_mode == "move_contents" else renamed_source_path,
                            moved_entries,
                            created_local_path,
                        )
                        _delete_remote_safely(backup_name)
                        message = str(exc)
                        if revert_errors:
                            message = f"{message} {' '.join(revert_errors)}"
                        return {"error": message}, 400
                    drive_renamed = True
                    drive_current_path = normalized_target

            run_rclone(
                ["config", "delete", normalized_name],
                capture_output=True,
                text=True,
                check=True,
            )
        except RuntimeError:
            revert_errors = _rollback_local_changes(
                move_mode,
                local_target_path,
                move_contents_source if move_mode == "move_contents" else renamed_source_path,
                moved_entries,
                created_local_path,
            )
            _delete_remote_safely(backup_name)
            message = "rclone is not installed"
            if drive_renamed and drive_current_path and drive_original_path_for_rollback:
                restore_error = _restore_drive_path(
                    drive_current_path, drive_original_path_for_rollback
                )
                if restore_error:
                    message = f"{message} {restore_error}".strip()
                else:
                    drive_renamed = False
            if revert_errors:
                message = f"{message} {' '.join(revert_errors)}"
            return {"error": message}, 500
        except subprocess.CalledProcessError as exc:
            revert_errors = _rollback_local_changes(
                move_mode,
                local_target_path,
                move_contents_source if move_mode == "move_contents" else renamed_source_path,
                moved_entries,
                created_local_path,
            )
            _delete_remote_safely(backup_name)
            message = (exc.stderr or exc.stdout or "").strip() or "No se pudo reemplazar el remote."
            if drive_renamed and drive_current_path and drive_original_path_for_rollback:
                restore_error = _restore_drive_path(
                    drive_current_path, drive_original_path_for_rollback
                )
                if restore_error:
                    message = f"{message} {restore_error}".strip()
                else:
                    drive_renamed = False
            if revert_errors:
                message = f"{message} {' '.join(revert_errors)}"
            return {"error": message}, 400

        share_url: str | None = None
        config_payload: str | None = None
        try:
            share_url = _execute_remote_plan(target_name, plan)
            snapshot = plan.config_snapshot or {}
            if not snapshot or not snapshot.get("type"):
                raise RemoteOperationError(
                    "No se pudo guardar la configuración del remote.",
                    500,
                )
            config_payload = json.dumps(snapshot, ensure_ascii=False)
        except RemoteOperationError as exc:
            revert_errors = _rollback_local_changes(
                move_mode,
                local_target_path,
                move_contents_source if move_mode == "move_contents" else renamed_source_path,
                moved_entries,
                created_local_path,
            )
            drive_restore_error: str | None = None
            if drive_renamed and drive_current_path and drive_original_path_for_rollback:
                drive_restore_error = _restore_drive_path(
                    drive_current_path, drive_original_path_for_rollback
                )
                if not drive_restore_error:
                    drive_renamed = False
            restored = _restore_remote_backup(normalized_name, backup_name)
            _delete_remote_safely(backup_name)
            message = str(exc)
            if drive_restore_error:
                message = f"{message} {drive_restore_error}".strip()
            if revert_errors:
                message = f"{message} {' '.join(revert_errors)}"
            if not restored:
                return {
                    "error": f"{message} No se pudo restaurar la configuración original.",
                }, 500
            return {"error": message}, exc.status_code
        except RuntimeError:
            revert_errors = _rollback_local_changes(
                move_mode,
                local_target_path,
                move_contents_source if move_mode == "move_contents" else renamed_source_path,
                moved_entries,
                created_local_path,
            )
            drive_restore_error: str | None = None
            if drive_renamed and drive_current_path and drive_original_path_for_rollback:
                drive_restore_error = _restore_drive_path(
                    drive_current_path, drive_original_path_for_rollback
                )
                if not drive_restore_error:
                    drive_renamed = False
            restored = _restore_remote_backup(normalized_name, backup_name)
            _delete_remote_safely(backup_name)
            message = "rclone is not installed"
            if drive_restore_error:
                message = f"{message} {drive_restore_error}".strip()
            if revert_errors:
                message = f"{message} {' '.join(revert_errors)}"
            if not restored:
                return {
                    "error": f"{message}. No se pudo restaurar la configuración original.",
                }, 500
            return {"error": message}, 500

        _delete_remote_safely(backup_name)

        normalized_old_remote = _normalize_remote(normalized_name)
        normalized_new_remote = _normalize_remote(target_name)
        route_value = (
            getattr(plan, "drive_remote_path", None)
            or getattr(plan, "local_target_path", None)
            or (plan.share_url or None)
            or share_url
        )
        with SessionLocal() as db:
            existing = db.query(RcloneRemote).filter_by(name=normalized_name).one_or_none()
            conflict = None
            if target_name != normalized_name:
                conflict = db.query(RcloneRemote).filter_by(name=target_name).one_or_none()
            if conflict and conflict is not existing:
                db.delete(conflict)
            if existing:
                existing.name = target_name
                existing.type = remote_type
                existing.route = route_value
                existing.share_url = share_url
                existing.config = config_payload
            else:
                db.add(
                    RcloneRemote(
                        name=target_name,
                        type=remote_type,
                        route=(route_value or None),
                        share_url=(share_url or None),
                        config=config_payload,
                        created_at=datetime.datetime.utcnow(),
                    )
                )
            if target_name != normalized_name:
                apps_to_update = db.query(App).filter_by(rclone_remote=normalized_old_remote).all()
                for app_obj in apps_to_update:
                    app_obj.rclone_remote = normalized_new_remote
            db.commit()

        response: dict[str, str] = {"status": "ok", "name": target_name}
        if route_value:
            response["route"] = route_value
        if share_url:
            response["share_url"] = share_url
        return response, 200

    @app.delete("/rclone/remotes/<remote_name>")
    @login_required
    def delete_rclone_remote(remote_name: str) -> tuple[dict, int]:
        """Remove an rclone remote and stored metadata."""

        normalized_name = _normalize_remote_name(remote_name)
        if not normalized_name:
            return {"error": "remote not found"}, 404

        stored_type: str | None = None
        stored_route: str | None = None
        with SessionLocal() as db:
            stored_remote = db.query(RcloneRemote).filter_by(name=normalized_name).one_or_none()
            if stored_remote:
                stored_type = (stored_remote.type or "").strip().lower() or None
                stored_route = (stored_remote.route or "").strip() or None
        drive_current_path = _normalize_drive_path(stored_route) if stored_type == "drive" else ""

        try:
            configured = fetch_configured_remotes()
        except RuntimeError:
            return {"error": "rclone is not installed"}, 500
        if normalized_name not in configured:
            return {"error": "remote not found"}, 404

        allowed_roots = _get_local_directory_roots()
        local_path_to_remove: str | None = None
        drive_temp_path: str | None = None
        drive_renamed = False
        if stored_type == "drive" and not drive_current_path:
            return {
                "error": (
                    "No se pudo identificar la carpeta de Google Drive asociada al remote. "
                    "Actualizalo antes de eliminarlo o contactá al administrador."
                )
            }, 400
        if stored_type == "local" and stored_route:
            candidate = _ensure_absolute_path(stored_route)
            if candidate:
                parent_path = os.path.dirname(candidate.rstrip(os.sep)) or os.sep
                normalized_parent = _normalize_filesystem_path(parent_path)
                normalized_candidate = _normalize_filesystem_path(candidate)
                if (
                    normalized_parent
                    and normalized_parent in allowed_roots
                    and normalized_candidate
                    and normalized_candidate != normalized_parent
                ):
                    local_path_to_remove = candidate
                else:
                    return {
                        "error": (
                            "No se pudo identificar la carpeta asociada al remote. "
                            "Actualizalo antes de eliminarlo o contactá al administrador."
                        )
                    }, 400

        backup_name: str | None = None
        requires_backup = bool(local_path_to_remove) or (stored_type == "drive" and drive_current_path)
        if requires_backup:
            backup_name = f"__delete__{uuid.uuid4().hex[:8]}"
            try:
                _clone_remote_configuration(
                    normalized_name,
                    backup_name,
                    error_message="No se pudo preparar la eliminación del remote.",
                )
            except RuntimeError:
                return {"error": "rclone is not installed"}, 500
            except RemoteOperationError as exc:
                _delete_remote_safely(backup_name)
                return {"error": str(exc)}, exc.status_code
            except subprocess.CalledProcessError as exc:
                message = (exc.stderr or exc.stdout or "").strip() or "No se pudo preparar la eliminación del remote."
                _delete_remote_safely(backup_name)
                return {"error": message}, 400

        try:
            if stored_type == "drive" and drive_current_path:
                drive_temp_path = _build_drive_temp_path(drive_current_path)
                try:
                    _move_drive_path(drive_current_path, drive_temp_path)
                except RemoteOperationError as exc:
                    if backup_name:
                        _delete_remote_safely(backup_name)
                    return {"error": str(exc)}, 400
                drive_renamed = True
            run_rclone(
                ["config", "delete", normalized_name],
                capture_output=True,
                text=True,
                check=True,
            )
        except RuntimeError:
            restore_error: str | None = None
            if drive_renamed and drive_temp_path and drive_current_path:
                restore_error = _restore_drive_path(drive_temp_path, drive_current_path)
            if backup_name:
                _delete_remote_safely(backup_name)
            message = "rclone is not installed"
            if restore_error:
                message = f"{message} {restore_error}".strip()
            return {"error": message}, 500
        except subprocess.CalledProcessError as exc:
            restore_error: str | None = None
            if drive_renamed and drive_temp_path and drive_current_path:
                restore_error = _restore_drive_path(drive_temp_path, drive_current_path)
            if backup_name:
                _delete_remote_safely(backup_name)
            message = (exc.stderr or exc.stdout or "").strip() or "failed to delete remote"
            if restore_error:
                message = f"{message} {restore_error}".strip()
            return {"error": message}, 400

        if drive_renamed and drive_temp_path and drive_current_path:
            purge_error: str | None = None
            try:
                _purge_drive_path(drive_temp_path)
                drive_renamed = False
                drive_temp_path = None
            except RemoteOperationError as exc:
                purge_error = str(exc)
            if purge_error:
                restore_error = _restore_drive_path(drive_temp_path, drive_current_path)
                restored_config = False
                if backup_name:
                    restored_config = _restore_remote_backup(normalized_name, backup_name)
                if backup_name:
                    _delete_remote_safely(backup_name)
                    backup_name = None
                message = purge_error
                if restore_error:
                    message = f"{message} {restore_error}".strip()
                if restored_config:
                    return {
                        "error": f"{message} La configuración original del remote se restauró.",
                    }, 400
                return {
                    "error": f"{message} No se pudo restaurar la configuración original.",
                }, 500

        removal_error: str | None = None
        if local_path_to_remove:
            if os.path.exists(local_path_to_remove):
                try:
                    shutil.rmtree(local_path_to_remove)
                except OSError as exc:
                    removal_error = _format_local_error("eliminar", local_path_to_remove, exc)
            if removal_error:
                restored = False
                if backup_name:
                    restored = _restore_remote_backup(normalized_name, backup_name)
                    _delete_remote_safely(backup_name)
                message = removal_error
                if restored:
                    return {
                        "error": f"{message} La configuración original del remote se restauró.",
                    }, 400
                return {
                    "error": f"{message} No se pudo restaurar la configuración original."
                }, 500

        if backup_name:
            _delete_remote_safely(backup_name)

        normalized_remote_value = _normalize_remote(normalized_name)
        with SessionLocal() as db:
            existing = db.query(RcloneRemote).filter_by(name=normalized_name).one_or_none()
            if existing:
                db.delete(existing)
            apps = db.query(App).filter_by(rclone_remote=normalized_remote_value).all()
            for app_obj in apps:
                app_obj.rclone_remote = None
            db.commit()

        response = {"status": "ok"}
        if local_path_to_remove:
            response["removed_path"] = local_path_to_remove
        return response, 200

    @app.post("/apps")
    @login_required
    def register_app() -> tuple[dict, int]:
        """Register a new app from JSON payload."""
        data = request.get_json(force=True)
        if not data:
            return {"error": "invalid payload"}, 400
        schedule = data.get("schedule") or None
        if schedule:
            try:
                CronTrigger.from_crontab(schedule)
            except ValueError:
                return {"error": "invalid schedule"}, 400
        remote = data.get("rclone_remote")
        if remote:
            try:
                result = run_rclone(
                    ["listremotes"], capture_output=True, text=True, check=True
                )
            except RuntimeError:
                return {"error": "rclone is not installed"}, 500
            available = [r.strip() for r in result.stdout.splitlines() if r.strip()]
            normalized = _normalize_remote(remote)
            if normalized not in available:
                return {"error": "unknown rclone remote"}, 400
            remote = normalized
        new_app = App(
            name=data.get("name"),
            url=data.get("url"),
            token=data.get("token"),
            schedule=schedule,
            drive_folder_id=data.get("drive_folder_id"),
            rclone_remote=remote,
            retention=data.get("retention"),
        )
        with SessionLocal() as db:
            db.add(new_app)
            db.commit()
        schedule_app_backups()
        return {"status": "ok"}, 201

    @app.put("/apps/<int:app_id>")
    @login_required
    def update_app(app_id: int):
        data = request.get_json(force=True)
        if not data:
            return {"error": "invalid payload"}, 400
        schedule = data.get("schedule") or None
        if schedule:
            try:
                CronTrigger.from_crontab(schedule)
            except ValueError:
                return {"error": "invalid schedule"}, 400
        remote = data.get("rclone_remote")
        if remote:
            try:
                result = run_rclone(
                    ["listremotes"], capture_output=True, text=True, check=True
                )
            except RuntimeError:
                return {"error": "rclone is not installed"}, 500
            available = [r.strip() for r in result.stdout.splitlines() if r.strip()]
            normalized = _normalize_remote(remote)
            if normalized not in available:
                return {"error": "unknown rclone remote"}, 400
            remote = normalized
        with SessionLocal() as db:
            app_obj = db.get(App, app_id)
            if not app_obj:
                return {"error": "not found"}, 404
            app_obj.name = data.get("name")
            app_obj.url = data.get("url")
            app_obj.token = data.get("token")
            app_obj.schedule = schedule
            app_obj.drive_folder_id = data.get("drive_folder_id")
            app_obj.rclone_remote = remote
            app_obj.retention = data.get("retention")
            db.commit()
        schedule_app_backups()
        return {"status": "ok"}, 200

    @app.delete("/apps/<int:app_id>")
    @login_required
    def delete_app(app_id: int):
        with SessionLocal() as db:
            app_obj = db.get(App, app_id)
            if not app_obj:
                return {"error": "not found"}, 404
            db.delete(app_obj)
            db.commit()
        schedule_app_backups()
        return {"status": "ok"}, 200

    @app.post("/apps/<int:app_id>/run")
    @login_required
    def run_app_backup(app_id: int):
        run_backup(app_id)
        return {"status": "started"}, 202

    app.restore_persisted_remotes = restore_persisted_remotes  # type: ignore[attr-defined]
    restore_persisted_remotes()

    return app


if __name__ == "__main__":
    app = create_app()
    port = int(os.getenv("PORT", "5550"))
    app.run(host="0.0.0.0", port=port, debug=True)
