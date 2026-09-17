"""Flask application factory for the price-scraping orchestrator."""

from __future__ import annotations

from typing import Any

from flask import Flask, g, jsonify

from orchestrator.db import init_engine, new_session
from orchestrator.logging import configure_logging, get_logger
from orchestrator.settings import Settings, get_settings

__all__ = ["create_app", "get_db"]

log = get_logger("orchestrator.app")


def get_db():
    """Request-scoped SQLAlchemy session, closed on teardown."""
    if "db" not in g:
        g.db = new_session()
    return g.db


def create_app(settings: Settings | None = None, *, start_scheduler: bool = False) -> Flask:
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    init_engine(settings)

    app = Flask(__name__)
    app.config["SETTINGS"] = settings
    app.config["SECRET_KEY"] = settings.secret_key
    app.config["JSON_SORT_KEYS"] = False

    @app.teardown_appcontext
    def _close_db(exc: BaseException | None) -> None:  # noqa: ARG001
        db = g.pop("db", None)
        if db is not None:
            if exc is None:
                try:
                    db.commit()
                except Exception:
                    db.rollback()
                    raise
            else:
                db.rollback()
            db.close()

    @app.errorhandler(404)
    def _not_found(_: Any):
        return jsonify({"error": "not found"}), 404

    @app.get("/healthz")
    def _healthz():
        return "ok", 200

    from orchestrator.api import register_api
    from orchestrator.ui import register_ui

    register_api(app)
    register_ui(app)

    if start_scheduler and settings.scheduler_enabled:
        from orchestrator.scheduler import start_scheduler as _start

        app.config["SCHEDULER"] = _start(settings)
        log.info("scheduler started")

    return app
