"""Server-rendered dashboard (Jinja + HTMX)."""

from __future__ import annotations

from flask import Flask


def register_ui(app: Flask) -> None:
    from orchestrator.ui.cost_per_frame import bp as cpf_bp
    from orchestrator.ui.insights import bp as insights_bp
    from orchestrator.ui.products import bp as products_bp
    from orchestrator.ui.views import bp

    app.register_blueprint(bp)
    app.register_blueprint(insights_bp)
    app.register_blueprint(products_bp)
    app.register_blueprint(cpf_bp)
