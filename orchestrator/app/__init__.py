import os
from flask import Flask, render_template, request, jsonify
from dotenv import load_dotenv

from .database import Base, SessionLocal, engine
from .models import App


def create_app() -> Flask:
    """Application factory for the backup orchestrator UI."""
    load_dotenv()
    app = Flask(__name__)
    Base.metadata.create_all(bind=engine)

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
                {"name": a.name, "url": a.url, "token": a.token} for a in apps
            ])

    @app.post("/apps")
    def register_app() -> tuple[dict, int]:
        """Register a new app from JSON payload."""
        data = request.get_json(force=True)
        if not data:
            return {"error": "invalid payload"}, 400
        new_app = App(name=data.get("name"), url=data.get("url"), token=data.get("token"))
        with SessionLocal() as db:
            db.add(new_app)
            db.commit()
        return {"status": "ok"}, 201

    return app


if __name__ == "__main__":
    app = create_app()
    port = int(os.getenv("PORT", "5550"))
    app.run(host="0.0.0.0", port=port, debug=True)
