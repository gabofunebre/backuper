from flask import Flask, render_template, request, jsonify


def create_app() -> Flask:
    """Application factory for the backup orchestrator UI."""
    app = Flask(__name__)
    registered_apps: list[dict] = []

    @app.route("/")
    def index() -> str:
        """Render main panel."""
        return render_template("index.html")

    @app.get("/apps")
    def list_apps() -> list[dict]:
        """Return registered apps as JSON."""
        return jsonify(registered_apps)

    @app.post("/apps")
    def register_app() -> tuple[dict, int]:
        """Register a new app from JSON payload."""
        data = request.get_json(force=True)
        if not data:
            return {"error": "invalid payload"}, 400
        registered_apps.append(data)
        return {"status": "ok"}, 201

    return app


if __name__ == "__main__":
    app = create_app()
    app.run(host="0.0.0.0", port=5550, debug=True)
