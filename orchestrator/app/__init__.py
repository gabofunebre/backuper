import os
from flask import Flask, render_template, request, jsonify
from dotenv import load_dotenv
from sqlalchemy import inspect, text
from apscheduler.triggers.cron import CronTrigger

from .database import Base, SessionLocal, engine
from .models import App
from orchestrator.scheduler import start as start_scheduler, schedule_app_backups
from orchestrator.services import rclone


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

    @app.route("/")
    def index() -> str:
        """Render main panel."""
        return render_template("index.html")

    @app.get("/apps")
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

    @app.post("/apps")
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
        new_app = App(
            name=data.get("name"),
            url=data.get("url"),
            token=data.get("token"),
            schedule=schedule,
            drive_folder_id=data.get("drive_folder_id"),
            rclone_remote=data.get("rclone_remote"),
            retention=data.get("retention"),
        )
        with SessionLocal() as db:
            db.add(new_app)
            db.commit()
        schedule_app_backups()
        return {"status": "ok"}, 201

    @app.get("/rclone/remotes")
    def get_remotes() -> tuple[dict, int]:
        """List configured rclone remotes."""
        try:
            remotes = rclone.list_remotes()
        except Exception as exc:  # pragma: no cover - defensive
            return {"error": str(exc)}, 500
        return {"remotes": remotes}, 200

    @app.post("/rclone/remotes")
    def create_remote_endpoint() -> tuple[dict, int]:
        """Create a new rclone remote from JSON payload."""
        data = request.get_json(force=True) or {}
        name = data.get("name")
        params = data.get("params")
        if not name or not isinstance(params, dict):
            return {"error": "invalid payload"}, 400
        try:
            rclone.create_remote(name, params)
        except Exception as exc:  # pragma: no cover - defensive
            return {"error": str(exc)}, 500
        return {"status": "ok"}, 201

    @app.delete("/rclone/remotes/<name>")
    def delete_remote_endpoint(name: str) -> tuple[dict, int]:
        """Delete an existing rclone remote."""
        try:
            rclone.delete_remote(name)
        except Exception as exc:  # pragma: no cover - defensive
            return {"error": str(exc)}, 500
        return {"status": "ok"}, 200

    return app


if __name__ == "__main__":
    app = create_app()
    port = int(os.getenv("PORT", "5550"))
    app.run(host="0.0.0.0", port=port, debug=True)
