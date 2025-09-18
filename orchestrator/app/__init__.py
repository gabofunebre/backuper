import os
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
            result = run_rclone(
                ["listremotes"], capture_output=True, text=True, check=True
            )
        except RuntimeError:
            return {"error": "rclone is not installed"}, 500
        remotes = [r.strip().rstrip(":") for r in result.stdout.splitlines() if r.strip()]
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

    @app.post("/rclone/remotes/drive/validate")
    @login_required
    def validate_drive_token() -> tuple[dict, int]:
        """Validate a Google Drive token without persisting configuration."""

        data = request.get_json(force=True) or {}
        token = (data.get("token") or "").strip()
        if not token:
            return {"error": "token is required"}, 400

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
            client_id = os.getenv("RCLONE_DRIVE_CLIENT_ID")
            client_secret = os.getenv("RCLONE_DRIVE_CLIENT_SECRET")
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
        args: list[str]
        post_config_commands: list[list[str]] = []
        if remote_type == "onedrive":
            return {"error": "OneDrive aún está en construcción"}, 400
        if remote_type == "drive":
            token = (settings.get("token") or "").strip()
            if not token:
                return {"error": "token is required"}, 400
            args = [
                "--non-interactive",
                "config",
                "create",
                name,
                "drive",
                "token",
                token,
                "scope",
                os.getenv("RCLONE_DRIVE_SCOPE", "drive"),
                "--no-auto-auth",
            ]
            client_id = os.getenv("RCLONE_DRIVE_CLIENT_ID")
            client_secret = os.getenv("RCLONE_DRIVE_CLIENT_SECRET")
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
            args = [
                "--non-interactive",
                "config",
                "create",
                name,
                "alias",
                "remote",
                path,
            ]
        elif remote_type == "sftp":
            host = (settings.get("host") or "").strip()
            username = (settings.get("username") or settings.get("user") or "").strip()
            password = (settings.get("password") or "").strip()
            port = (settings.get("port") or "").strip()
            if not host:
                return {"error": "host is required"}, 400
            if not username:
                return {"error": "username is required"}, 400
            if not password:
                return {"error": "password is required"}, 400
            if port and not port.isdigit():
                return {"error": "invalid port"}, 400
            args = [
                "--non-interactive",
                "config",
                "create",
                name,
                "sftp",
                "host",
                host,
                "user",
                username,
            ]
            if port:
                args.extend(["port", port])
            args.extend(["pass", password])
            post_config_commands = [
                ["lsd", f"{name}:"],
                ["mkdir", f"{name}:{name}"],
            ]
        else:
            return {"error": "unsupported remote type"}, 400
        try:
            run_rclone(args, capture_output=True, text=True, check=True)
            for extra_args in post_config_commands:
                try:
                    run_rclone(extra_args, capture_output=True, text=True, check=True)
                except subprocess.CalledProcessError as exc:
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
                    return {"error": error}, 400
        except RuntimeError:
            return {"error": "rclone is not installed"}, 500
        except subprocess.CalledProcessError as exc:
            error = (exc.stderr or exc.stdout or "").strip() or "failed to create remote"
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
