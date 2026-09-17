from __future__ import annotations

import fakeredis
import pytest


@pytest.fixture
def settings(tmp_path, monkeypatch):
    db_path = (tmp_path / "test.db").as_posix()
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("INGEST_TOKEN", "test-token")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/15")
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("PROVIDER", "local")

    from orchestrator.db import create_all, init_engine
    from orchestrator.settings import get_settings, reset_settings_cache

    reset_settings_cache()
    s = get_settings()
    init_engine(s)
    create_all()
    yield s
    reset_settings_cache()


@pytest.fixture
def app(settings):
    from orchestrator import create_app

    application = create_app(settings)
    application.config.update(TESTING=True)
    return application


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def fake_redis(monkeypatch):
    fr = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr("orchestrator.queue.build_redis", lambda *a, **k: fr)
    return fr


@pytest.fixture
def db_session(settings):
    from orchestrator.db import session_scope

    with session_scope() as s:
        yield s
