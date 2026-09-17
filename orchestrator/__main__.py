"""Entrypoint: ``python -m orchestrator [--scheduler] [--create-all]``.

``--scheduler`` runs the APScheduler loop in-process (run exactly one such
process). ``--create-all`` creates tables from the ORM metadata instead of
requiring ``alembic upgrade head`` — handy for the fastest local loop.
"""

from __future__ import annotations

import argparse

from orchestrator import create_app
from orchestrator.db import create_all
from orchestrator.logging import get_logger
from orchestrator.settings import get_settings

log = get_logger("orchestrator.main")


def main() -> None:
    parser = argparse.ArgumentParser(prog="orchestrator")
    parser.add_argument("--scheduler", action="store_true", help="run the in-process scheduler")
    parser.add_argument("--create-all", action="store_true", help="create tables then serve")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args()

    settings = get_settings()
    if args.create_all:
        create_all()
        log.info("created tables for %s", settings.database_url)

    app = create_app(settings, start_scheduler=args.scheduler)
    app.run(
        host=args.host or settings.host,
        port=args.port or settings.port,
        debug=False,
        use_reloader=False,
    )


if __name__ == "__main__":
    main()
