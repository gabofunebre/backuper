import json
import os
import posixpath
import re
import subprocess
import tempfile
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

from .database import Base, SessionLocal, engine
from .models import App
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

    @app.route("/remotes")
    @login_required
    def remotes() -> str:
        """Render rclone remotes management page."""
        return render_template("remotes.html")

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
    def list_rclone_remotes() -> list[str]:
        """Return available rclone remotes."""
        try:
            remotes = fetch_configured_remotes()
        except RuntimeError:
            return {"error": "rclone is not installed"}, 500
        return jsonify(remotes)

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
        data = request.get_json(force=True)
        if not data or not data.get("name") or not data.get("type"):
            return {"error": "invalid payload"}, 400
        allowed_types = {"drive", "onedrive", "sftp", "local"}
        remote_type = data["type"]
        if remote_type not in allowed_types:
            return {"error": "unsupported remote type"}, 400
        name = (data.get("name") or "").strip()
        if not name:
            return {"error": "invalid payload"}, 400
        settings = data.get("settings") or {}
        base_args = ["config", "create", "--non-interactive", name]
        args: list[str]
        pre_config_commands: list[list[str]] = []
        post_config_commands: list[list[str]] = []
        cleanup_remote_on_error = False
        error_translator = None
        if remote_type == "onedrive":
            return {"error": "OneDrive aún está en construcción"}, 400
        if remote_type == "drive":
            mode = (settings.get("mode") or "").strip().lower()
            token = (settings.get("token") or "").strip()
            if not mode:
                mode = "custom" if token else "shared"
            if mode not in {"shared", "custom"}:
                return {"error": "invalid drive mode"}, 400
            if mode == "shared":
                email = (settings.get("email") or "").strip()
                if not email:
                    return {"error": "email is required"}, 400
                if "@" not in email:
                    return {"error": "invalid email"}, 400
                base_remote = _normalize_remote(
                    os.getenv("RCLONE_REMOTE", "gdrive")
                )
                folder_name = (settings.get("folder_name") or name or "").strip()
                if not folder_name:
                    return {"error": "folder name is required"}, 400
                remote_path = f"{base_remote}{folder_name}"
                try:
                    ensure_default_drive_remote()
                except DefaultDriveRemoteError as exc:
                    return {"error": str(exc)}, 500
                pre_config_commands = [
                    ["mkdir", remote_path],
                    [
                        "backend",
                        "command",
                        remote_path,
                        "share",
                        "--share-with",
                        email,
                        "--type",
                        os.getenv("RCLONE_DRIVE_SHARE_TYPE", "user"),
                        "--role",
                        os.getenv("RCLONE_DRIVE_SHARE_ROLE", "writer"),
                    ],
                ]
                args = base_args + ["alias", "remote", remote_path]
            else:
                if not token:
                    return {"error": "token is required"}, 400
                args = [
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
                    args.extend(["client_id", client_id])
                if client_secret:
                    args.extend(["client_secret", client_secret])
        elif remote_type == "local":
            directories = {entry["path"] for entry in get_local_directories()}
            if not directories:
                return {"error": "no local directories configured"}, 500
            path = (settings.get("path") or "").strip()
            if not path:
                return {"error": "path is required"}, 400
            if path not in directories:
                return {"error": "invalid path"}, 400
            args = base_args + ["alias", "remote", path]
        elif remote_type == "sftp":
            host = (settings.get("host") or "").strip()
            username = (settings.get("username") or settings.get("user") or "").strip()
            password = (settings.get("password") or "").strip()
            port = (settings.get("port") or "").strip()
            base_path = (settings.get("base_path") or "").strip()
            if not host:
                return {"error": "host is required"}, 400
            if not username:
                return {"error": "username is required"}, 400
            if not password:
                return {"error": "password is required"}, 400
            if port and not port.isdigit():
                return {"error": "invalid port"}, 400
            if not base_path:
                return {
                    "error": "Seleccioná la carpeta del servidor SFTP donde se crearán los respaldos.",
                }, 400
            normalized_base = _normalize_sftp_base_path(base_path)
            try:
                target_path = _join_sftp_folder(normalized_base, name)
            except ValueError:
                return {"error": "El nombre del remote no es válido para crear una carpeta en SFTP."}, 400
            args = [
                *base_args,
                "sftp",
                "host",
                host,
                "user",
                username,
            ]
            if port:
                args.extend(["port", port])
            args.extend(["path", target_path, "pass", password])
            post_config_commands = [
                ["mkdir", f"{name}:"],
                ["lsd", f"{name}:"],
            ]
            cleanup_remote_on_error = True
            error_translator = _translate_sftp_error
        else:
            return {"error": "unsupported remote type"}, 400
        try:
            for extra_args in pre_config_commands:
                run_rclone(extra_args, capture_output=True, text=True, check=True)
            run_rclone(args, capture_output=True, text=True, check=True)
            for extra_args in post_config_commands:
                try:
                    run_rclone(extra_args, capture_output=True, text=True, check=True)
                except subprocess.CalledProcessError as exc:
                    try:
                        if cleanup_remote_on_error:
                            run_rclone(
                                ["config", "delete", name],
                                capture_output=True,
                                text=True,
                                check=True,
                            )
                    except Exception:
                        pass
                    error = (exc.stderr or exc.stdout or "").strip() or "failed to create remote"
                    if error_translator:
                        error = error_translator(error)
                    return {"error": error}, 400
        except RuntimeError:
            return {"error": "rclone is not installed"}, 500
        except subprocess.CalledProcessError as exc:
            error = (exc.stderr or exc.stdout or "").strip() or "failed to create remote"
            if error_translator:
                error = error_translator(error)
            return {"error": error}, 400
        return {"status": "ok"}, 201

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
