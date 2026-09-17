"""JSON API blueprints, all mounted under ``/api/v1``."""

from __future__ import annotations

from flask import Flask


def register_api(app: Flask) -> None:
    from orchestrator.api.categories import bp as categories_bp
    from orchestrator.api.ingest import bp as ingest_bp
    from orchestrator.api.jobs import bp as jobs_bp
    from orchestrator.api.listings import bp as listings_bp
    from orchestrator.api.nodes import bp as nodes_bp
    from orchestrator.api.queries import bp as queries_bp
    from orchestrator.api.schedules import bp as schedules_bp
    from orchestrator.api.stats import bp as stats_bp
    from orchestrator.api.tracked import bp as tracked_bp
    from orchestrator.api.vendors import bp as vendors_bp

    for bp in (
        ingest_bp,
        vendors_bp,
        categories_bp,
        queries_bp,
        schedules_bp,
        jobs_bp,
        listings_bp,
        tracked_bp,
        nodes_bp,
        stats_bp,
    ):
        app.register_blueprint(bp)
