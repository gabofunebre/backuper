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
from orchestrator.scheduler import start as start_scheduler, schedule_app_backups
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

    @app.get("/apps")
    @login_required
    def list_apps() -> list[dict]:
        """Return registered apps as JSON."""
        with SessionLocal() as db:
            apps = db.query(App).all()
            return jsonify([
                {
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
        result = subprocess.run(
            ["rclone", "listremotes"], capture_output=True, text=True, check=True
        )
        remotes = [r.strip().rstrip(":") for r in result.stdout.splitlines() if r.strip()]
        return jsonify(remotes)

    @app.post("/rclone/remotes")
    @login_required
    def create_rclone_remote() -> tuple[dict, int]:
        """Create a new rclone remote."""
        data = request.get_json(force=True)
        if not data or not data.get("name") or not data.get("type"):
            return {"error": "invalid payload"}, 400
        subprocess.run(
            ["rclone", "config", "create", data["name"], data["type"]],
            check=True,
        )
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
            result = subprocess.run(
                ["rclone", "listremotes"], capture_output=True, text=True, check=True
            )
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

    @app.post("/rclone/remotes/<name>/authorize")
    @login_required
    def authorize_remote(name: str):
        """Initiate or complete authorization for an rclone remote."""
        data = request.get_json(silent=True) or {}
        token = data.get("token")
        if token:
            subprocess.run(
                ["rclone", "config", "update", name, "token", token],
                check=True,
            )
            return {"status": "ok"}, 200
        url = authorize_drive()
        return {"url": url}, 200

    return app


if __name__ == "__main__":
    app = create_app()
    port = int(os.getenv("PORT", "5550"))
    app.run(host="0.0.0.0", port=port, debug=True)
