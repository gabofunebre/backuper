import os
import subprocess
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
from orchestrator.services.rclone import authorize_drive


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
        config_file = os.getenv("RCLONE_CONFIG", "/config/rclone/rclone.conf")
        supplied_config = any(
            arg == "--config" or arg.startswith("--config=") for arg in args
        )
        cmd = ["rclone"]
        if not supplied_config:
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

    @app.post("/rclone/remotes")
    @login_required
    def create_rclone_remote() -> tuple[dict, int]:
        """Create a new rclone remote."""
        data = request.get_json(force=True)
        if not data or not data.get("name") or not data.get("type"):
            return {"error": "invalid payload"}, 400
        allowed_types = {"drive", "onedrive", "sftp", "local"}
        if data["type"] not in allowed_types:
            return {"error": "unsupported remote type"}, 400
        defaults: dict[str, list[str]] = {
            "drive": ["scope", "drive", "--no-auto-auth"],
            "onedrive": ["--no-auto-auth"],
            "sftp": [],
            "local": [],
        }
        args = [
            "--non-interactive",
            "config",
            "create",
            data["name"],
            data["type"],
            *defaults.get(data["type"], []),
        ]
        try:
            run_rclone(args, capture_output=True, text=True, check=True)
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
            normalized = remote if remote.endswith(":") else f"{remote}:"
            if normalized not in available:
                return {"error": "unknown rclone remote"}, 400
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
            normalized = remote if remote.endswith(":") else f"{remote}:"
            if normalized not in available:
                return {"error": "unknown rclone remote"}, 400
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

    @app.get("/rclone/remotes/<name>/authorize")
    @login_required
    def authorize_remote_url(name: str):
        url = authorize_drive()
        return {"url": url}, 200

    @app.post("/rclone/remotes/<name>/authorize")
    @login_required
    def authorize_remote(name: str):
        """Complete authorization for an rclone remote."""
        data = request.get_json(silent=True) or {}
        token = data.get("token")
        if not token:
            return {"error": "invalid payload"}, 400
        try:
            run_rclone(["config", "update", name, "token", token], check=True)
        except RuntimeError:
            return {"error": "rclone is not installed"}, 500
        return {"status": "ok"}, 200

    return app


if __name__ == "__main__":
    app = create_app()
    port = int(os.getenv("PORT", "5550"))
    app.run(host="0.0.0.0", port=port, debug=True)
