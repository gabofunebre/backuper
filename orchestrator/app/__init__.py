import os
from flask import Flask, render_template, request, jsonify
from dotenv import load_dotenv
from sqlalchemy import inspect, text
from apscheduler.triggers.cron import CronTrigger

from .database import Base, SessionLocal, engine
from .models import App
from orchestrator.scheduler import start as start_scheduler, schedule_app_backups


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
            retention=data.get("retention"),
        )
        with SessionLocal() as db:
            db.add(new_app)
            db.commit()
        schedule_app_backups()
        return {"status": "ok"}, 201

    return app


if __name__ == "__main__":
    app = create_app()
    port = int(os.getenv("PORT", "5550"))
    app.run(host="0.0.0.0", port=port, debug=True)
