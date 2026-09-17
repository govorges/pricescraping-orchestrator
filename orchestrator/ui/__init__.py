"""Server-rendered dashboard (Jinja + HTMX)."""

from __future__ import annotations

from flask import Flask


def register_ui(app: Flask) -> None:
    from orchestrator.ui.insights import bp as insights_bp
    from orchestrator.ui.views import bp

    app.register_blueprint(bp)
    app.register_blueprint(insights_bp)
