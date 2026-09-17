"""SQLAlchemy engine / session plumbing.

One ``DATABASE_URL`` drives everything: ``sqlite:///...`` for local dev,
``postgresql+psycopg://...`` (TimescaleDB) in production. Schema is owned by
Alembic (``migrations/``); :func:`create_all` is a convenience for tests and the
fastest local loop.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from orchestrator.settings import Settings, get_settings


class Base(DeclarativeBase):
    pass


_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def _make_engine(settings: Settings) -> Engine:
    url = settings.database_url
    kwargs: dict = {"future": True, "pool_pre_ping": True}
    if url.startswith("sqlite"):
        # allow use across the Flask request thread and the APScheduler threads
        kwargs["connect_args"] = {"check_same_thread": False}
    return create_engine(url, **kwargs)


def init_engine(settings: Settings | None = None) -> Engine:
    """(Re)build the process-wide engine + session factory."""
    global _engine, _SessionLocal
    settings = settings or get_settings()
    _engine = _make_engine(settings)
    _SessionLocal = sessionmaker(bind=_engine, autoflush=False, expire_on_commit=False, future=True)
    return _engine


def get_engine() -> Engine:
    if _engine is None:
        init_engine()
    assert _engine is not None
    return _engine


def _session_factory() -> sessionmaker[Session]:
    if _SessionLocal is None:
        init_engine()
    assert _SessionLocal is not None
    return _SessionLocal


def new_session() -> Session:
    """A fresh Session the caller is responsible for closing."""
    return _session_factory()()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope: commit on success, rollback on error, always close.

    Used by scripts, the scheduler, and anywhere outside a Flask request.
    """
    session = new_session()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def create_all() -> None:
    """Create every table from the ORM metadata (tests / quick local start)."""
    import orchestrator.models  # noqa: F401  (register mappers)

    Base.metadata.create_all(get_engine())


def drop_all() -> None:
    import orchestrator.models  # noqa: F401

    Base.metadata.drop_all(get_engine())
