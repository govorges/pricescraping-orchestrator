"""Server-rendered dashboard (Jinja + HTMX)."""

from __future__ import annotations

from flask import Flask


def register_ui(app: Flask) -> None:
    from orchestrator import bands

    # the dynamic band's ratios, for help text that can't drift from the code
    app.jinja_env.globals["dynamic_band_rule"] = {
        "min_pct": round(bands.MIN_RATIO * 100), "max_x": f"{bands.MAX_RATIO:g}",
        "runs": bands.REFERENCE_RUNS, "min_runs": bands.MIN_REFERENCE_RUNS,
    }
    from orchestrator.ui.anomalies_ui import bp as anomalies_bp
    from orchestrator.ui.cost_per_frame import bp as cpf_bp
    from orchestrator.ui.insights import bp as insights_bp
    from orchestrator.ui.products import bp as products_bp
    from orchestrator.ui.views import bp

    app.register_blueprint(bp)
    app.register_blueprint(insights_bp)
    app.register_blueprint(products_bp)
    app.register_blueprint(cpf_bp)
    app.register_blueprint(anomalies_bp)
