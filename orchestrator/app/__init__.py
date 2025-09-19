import json
import os
import posixpath
import re
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
        share_url: str | None = None

    class RemoteOperationError(Exception):
        """Raised when a remote operation fails for a known reason."""

        def __init__(self, message: str, status_code: int = 400) -> None:
            super().__init__(message)
            self.status_code = status_code

    admin_user = os.getenv("APP_ADMIN_USER")
    admin_pass = os.getenv("APP_ADMIN_PASS")
    app.secret_key = os.getenv("APP_SECRET_KEY", "devkey")

    def _parse_directory_config(value: str) -> list[dict[str, str]]:
        """Parse a delimited list of directories from *value*."""

        entries: list[dict[str, str]] = []
        if not value:
            return entries
        for raw in re.split(r"[;,\n]+", value):
            item = raw.strip()
            if not item:
                continue
            label, sep, path = item.partition("|")
            if sep:
                label = label.strip() or path.strip()
                path = path.strip()
            else:
                path = label.strip()
                label = path
            if not path:
                continue
            entries.append({"label": label.strip(), "path": path})
        return entries

    def get_local_directories() -> list[dict[str, str]]:
        return _parse_directory_config(os.getenv("RCLONE_LOCAL_DIRECTORIES", ""))

    def _normalize_remote_name(value: str | None) -> str:
        cleaned = (value or "").strip()
        if not cleaned:
            return ""
        return _normalize_remote(cleaned).rstrip(":")

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

    def _build_remote_plan(name: str, remote_type: str, settings: dict) -> RemotePlan:
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
                folder_name = (settings.get("folder_name") or name or "").strip()
                if not folder_name:
                    raise RemoteOperationError("folder name is required")
                drive_remote_path = f"{base_remote}{folder_name}"
                ensure_default_drive_remote()
                plan.pre_commands.append(["mkdir", drive_remote_path])
                plan.command = [
                    *base_args,
                    "alias",
                    "remote",
                    drive_remote_path,
                ]
                plan.cleanup_on_error = True
                plan.drive_mode = "shared"
                plan.drive_remote_path = drive_remote_path
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
        elif normalized_type == "local":
            directories = {entry["path"] for entry in get_local_directories()}
            if not directories:
                raise RemoteOperationError("no local directories configured", 500)
            path = (settings.get("path") or "").strip()
            if not path:
                raise RemoteOperationError("path is required")
            if path not in directories:
                raise RemoteOperationError("invalid path")
            plan.command = [*base_args, "alias", "remote", path]
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
            plan.command.extend(["path", target_path, "pass", password])
            plan.post_commands = [["mkdir", f"{name}:"], ["lsd", f"{name}:"]]
            plan.cleanup_on_error = True
            plan.error_translator = _translate_sftp_error
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

        return share_url

    def _restore_remote_backup(remote_name: str, backup_name: str) -> bool:
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
            run_rclone(
                ["config", "copy", backup_name, remote_name],
                capture_output=True,
                text=True,
                check=True,
            )
        except (subprocess.CalledProcessError, RuntimeError):
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
        return render_template("rclone_config.html")

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
            remotes = fetch_configured_remotes()
        except RuntimeError:
            return {"error": "rclone is not installed"}, 500

        with SessionLocal() as db:
            stored = {remote.name: remote for remote in db.query(RcloneRemote).all()}

        entries: list[dict[str, str]] = []
        for remote_name in remotes:
            item: dict[str, str] = {"name": remote_name}
            stored_remote = stored.get(remote_name)
            if stored_remote:
                if stored_remote.type:
                    item["type"] = stored_remote.type
                if stored_remote.share_url:
                    item["share_url"] = stored_remote.share_url
            entries.append(item)
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
            args.extend(["pass", password])
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
            plan = _build_remote_plan(name, remote_type, settings)
        except RemoteOperationError as exc:
            return {"error": str(exc)}, exc.status_code
        except DefaultDriveRemoteError as exc:
            return {"error": str(exc)}, 500

        try:
            share_url = _execute_remote_plan(name, plan)
        except RemoteOperationError as exc:
            return {"error": str(exc)}, exc.status_code
        except RuntimeError:
            return {"error": "rclone is not installed"}, 500

        with SessionLocal() as db:
            existing = db.query(RcloneRemote).filter_by(name=name).one_or_none()
            if existing:
                existing.type = remote_type
                existing.share_url = share_url
            else:
                db.add(
                    RcloneRemote(
                        name=name,
                        type=remote_type,
                        share_url=share_url,
                    )
                )
            db.commit()

        response = {"status": "ok"}
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
        if not normalized_name:
            return {"error": "remote not found"}, 404
        if not remote_type:
            return {"error": "invalid payload"}, 400

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

        try:
            plan = _build_remote_plan(normalized_name, remote_type, settings)
        except RemoteOperationError as exc:
            return {"error": str(exc)}, exc.status_code
        except DefaultDriveRemoteError as exc:
            return {"error": str(exc)}, 500

        backup_name = f"__backup__{uuid.uuid4().hex[:8]}"
        try:
            run_rclone(
                ["config", "copy", normalized_name, backup_name],
                capture_output=True,
                text=True,
                check=True,
            )
        except RuntimeError:
            return {"error": "rclone is not installed"}, 500
        except subprocess.CalledProcessError as exc:
            message = (exc.stderr or exc.stdout or "").strip() or "No se pudo preparar la edición del remote."
            _delete_remote_safely(backup_name)
            return {"error": message}, 400

        try:
            run_rclone(
                ["config", "delete", normalized_name],
                capture_output=True,
                text=True,
                check=True,
            )
        except RuntimeError:
            _delete_remote_safely(backup_name)
            return {"error": "rclone is not installed"}, 500
        except subprocess.CalledProcessError as exc:
            message = (exc.stderr or exc.stdout or "").strip() or "No se pudo reemplazar el remote."
            _delete_remote_safely(backup_name)
            return {"error": message}, 400

        share_url: str | None = None
        try:
            share_url = _execute_remote_plan(normalized_name, plan)
        except RemoteOperationError as exc:
            restored = _restore_remote_backup(normalized_name, backup_name)
            _delete_remote_safely(backup_name)
            if not restored:
                return {
                    "error": f"{exc}. No se pudo restaurar la configuración original.",
                }, 500
            return {"error": str(exc)}, exc.status_code
        except RuntimeError:
            restored = _restore_remote_backup(normalized_name, backup_name)
            _delete_remote_safely(backup_name)
            if not restored:
                return {
                    "error": "rclone is not installed. No se pudo restaurar la configuración original.",
                }, 500
            return {"error": "rclone is not installed"}, 500

        _delete_remote_safely(backup_name)

        with SessionLocal() as db:
            existing = db.query(RcloneRemote).filter_by(name=normalized_name).one_or_none()
            if existing:
                existing.type = remote_type
                existing.share_url = share_url
            else:
                db.add(
                    RcloneRemote(
                        name=normalized_name,
                        type=remote_type,
                        share_url=share_url,
                    )
                )
            db.commit()

        response = {"status": "ok"}
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

        try:
            configured = fetch_configured_remotes()
        except RuntimeError:
            return {"error": "rclone is not installed"}, 500
        if normalized_name not in configured:
            return {"error": "remote not found"}, 404

        try:
            run_rclone(
                ["config", "delete", normalized_name],
                capture_output=True,
                text=True,
                check=True,
            )
        except RuntimeError:
            return {"error": "rclone is not installed"}, 500
        except subprocess.CalledProcessError as exc:
            message = (exc.stderr or exc.stdout or "").strip() or "failed to delete remote"
            return {"error": message}, 400

        with SessionLocal() as db:
            existing = db.query(RcloneRemote).filter_by(name=normalized_name).one_or_none()
            if existing:
                db.delete(existing)
            db.commit()

        return {"status": "ok"}, 200

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

    return app


if __name__ == "__main__":
    app = create_app()
    port = int(os.getenv("PORT", "5550"))
    app.run(host="0.0.0.0", port=port, debug=True)
