"""New filters: Jobs page 'filter by query name', and category-scoped
observations for the Overview chart's category selector."""

from __future__ import annotations

from datetime import datetime, timezone

from orchestrator.db import session_scope
from orchestrator.models import Category, Job, PriceObservation, QueryObject
from orchestrator.ui.views import _jobs_query

VENDOR = "usa_ebay"


def _job(db, *, job_id, qid, created_at=None):
    db.add(
        Job(
            job_id=job_id, vendor_id=VENDOR, query_object_id=qid, status="ok",
            created_at=created_at or datetime.now(timezone.utc),
        )
    )


def _seed_jobs():
    with session_scope() as db:
        db.add(QueryObject(id="gpu-rtx-5090", content="RTX 5090"))
        db.add(QueryObject(id="gpu-rtx-4090", content="RTX 4090"))
        db.add(QueryObject(id="cpu-9800x3d", content="9800X3D"))
        _job(db, job_id="j1", qid="gpu-rtx-5090")
        _job(db, job_id="j2", qid="gpu-rtx-4090")
        _job(db, job_id="j3", qid="cpu-9800x3d")


def test_jobs_query_filters_by_query_name_substring(settings):
    """A substring of the query's display content (not its id) narrows the jobs list."""
    _seed_jobs()
    with session_scope() as db:
        rows = _jobs_query(db, {"q": "5090"})
    assert {j.job_id for j in rows} == {"j1"}


def test_jobs_query_filter_is_case_insensitive_and_matches_id(settings):
    """The filter is case-insensitive and also matches against the query object's id, not just its content."""
    _seed_jobs()
    with session_scope() as db:
        rows = _jobs_query(db, {"q": "GPU-RTX"})
    assert {j.job_id for j in rows} == {"j1", "j2"}


def test_jobs_query_filter_no_match_returns_empty(settings):
    """A filter term matching no query object returns zero jobs, not an unfiltered list."""
    _seed_jobs()
    with session_scope() as db:
        rows = _jobs_query(db, {"q": "nothing-like-this"})
    assert rows == []


def test_jobs_query_blank_filter_returns_everything(settings):
    """A blank/whitespace-only filter term is treated as no filter at all."""
    _seed_jobs()
    with session_scope() as db:
        rows = _jobs_query(db, {"q": "  "})
    assert len(rows) == 3


def _obs(db, *, qid, observed_at=None):
    at = observed_at or datetime.now(timezone.utc)
    db.add(
        PriceObservation(
            vendor_id=VENDOR, query_object_id=qid, observed_date=at.date(), observed_at=at,
            job_id=f"j-{qid}", status="ok", price_median=100.0,
        )
    )


def test_observations_api_filters_by_category(client):
    """category_id resolves to every query object in that category, not just an exact query_object_id match."""
    with session_scope() as db:
        db.add(Category(id="gpu", name="GPUs"))
        db.add(QueryObject(id="gpu-rtx-5090", content="RTX 5090", category_id="gpu"))
        db.add(QueryObject(id="cpu-9800x3d", content="9800X3D"))
        _obs(db, qid="gpu-rtx-5090")
        _obs(db, qid="cpu-9800x3d")

    r = client.get("/api/v1/observations?category_id=gpu")
    assert r.status_code == 200
    rows = r.get_json()
    assert {row["query_object_id"] for row in rows} == {"gpu-rtx-5090"}


def test_observations_api_unknown_category_returns_empty(client):
    """An unknown category_id returns an empty list rather than erroring or ignoring the filter."""
    r = client.get("/api/v1/observations?category_id=does-not-exist")
    assert r.status_code == 200
    assert r.get_json() == []
