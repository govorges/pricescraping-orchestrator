from __future__ import annotations

import copy
import html
import json
from pathlib import Path

from orchestrator.db import session_scope
from orchestrator.models import QueryObject, Vendor
from orchestrator.ui.views import _drop_reason_group

RESULT = json.loads(
    (Path(__file__).parent / "fixtures" / "scrape_result.example.json").read_text(encoding="utf-8")
)
PATH = "/api/v1/ingest/scrape-results"
AUTH = {"Authorization": "Bearer test-token"}


def _listing(n: int, price: float) -> dict:
    return {"title": f"RTX 5090 card #{n}", "link": f"https://www.ebay.com/itm/{n}", "price": price, "extra": {}}


def _ingest(client, job_id: str, at: str, listings: list[dict], status: str = "ok") -> None:
    payload = copy.deepcopy(RESULT)
    payload.update(job_id=job_id, observed_at=at, observed_date=at[:10], status=status, listings=listings)
    assert client.post(PATH, json=payload, headers=AUTH).status_code == 202


def _seed_names():
    with session_scope() as db:
        db.add(Vendor(identifier="usa_ebay", name="eBay US", config={}))
        db.add(QueryObject(id="gpu-rtx-5090", content="RTX 5090"))


def _changed(page: str) -> str:
    return html.unescape(page[page.index("What changed in this run"):page.index("kept listings</b>")])


def test_run_page_shows_new_and_repriced_listings(client):
    """Compared with the previous clean run: #1 was repriced and #3 is new; an errored run in between is skipped."""
    _seed_names()
    _ingest(client, "run-1", "2026-09-01T08:00:00Z", [_listing(1, 2000.0), _listing(2, 2100.0)])
    _ingest(client, "run-err", "2026-09-01T14:00:00Z", [], status="error")
    _ingest(client, "run-2", "2026-09-01T20:00:00Z", [_listing(1, 1900.0), _listing(2, 2100.0), _listing(3, 2200.0)])

    html = client.get("/runs/run-2").get_data(as_text=True)
    assert "RTX 5090 <span" in html and "eBay US" in html  # "<product> at <vendor>" heading
    changed = _changed(html)
    assert 'href="/runs/run-1"' in changed  # compared against run-1, not the errored run
    assert "RTX 5090 card #1" in changed and "−$100" in changed and "from $2,000" in changed
    assert "RTX 5090 card #3" in changed and ">new<" in changed
    assert "RTX 5090 card #2" not in changed  # unchanged price, nothing to report
    assert "1 new · 2 seen before" in html


def test_listing_is_gone_only_after_missing_two_runs_in_a_row(client):
    """One miss is noted but not called gone (results shuffle); the second miss reports it gone, once."""
    _ingest(client, "run-1", "2026-09-01T08:00:00Z", [_listing(1, 2000.0), _listing(2, 2100.0)])
    _ingest(client, "run-2", "2026-09-01T14:00:00Z", [_listing(1, 2000.0)])
    _ingest(client, "run-3", "2026-09-01T20:00:00Z", [_listing(1, 2000.0)])
    _ingest(client, "run-4", "2026-09-02T02:00:00Z", [_listing(1, 2000.0)])

    first_miss = _changed(client.get("/runs/run-2").get_data(as_text=True))
    assert ">gone<" not in first_miss
    assert "1 listing from the previous run isn't in this one" in first_miss

    second_miss = _changed(client.get("/runs/run-3").get_data(as_text=True))
    assert "RTX 5090 card #2" in second_miss and ">gone<" in second_miss

    assert ">gone<" not in _changed(client.get("/runs/run-4").get_data(as_text=True))


def test_listing_stays_active_until_it_misses_two_runs(client):
    """The Listings tab keeps a listing active after one missed run and marks it ended after two."""
    _seed_names()
    _ingest(client, "run-1", "2026-09-01T08:00:00Z", [_listing(1, 2000.0), _listing(2, 2100.0)])
    _ingest(client, "run-2", "2026-09-01T14:00:00Z", [_listing(1, 2000.0)])
    active = client.get("/products/gpu-rtx-5090/listings?status=active").get_data(as_text=True)
    assert "RTX 5090 card #2" in active

    _ingest(client, "run-3", "2026-09-01T20:00:00Z", [_listing(1, 2000.0)])
    active = client.get("/products/gpu-rtx-5090/listings?status=active").get_data(as_text=True)
    ended = client.get("/products/gpu-rtx-5090/listings?status=inactive").get_data(as_text=True)
    assert "RTX 5090 card #2" not in active
    assert "RTX 5090 card #2" in ended


def test_first_run_has_nothing_to_compare_against(client):
    """A product's first run at a vendor lists every listing as new rather than inventing a comparison."""
    _ingest(client, "run-first", "2026-09-01T08:00:00Z", [_listing(1, 2000.0)])
    html = client.get("/runs/run-first").get_data(as_text=True)
    assert "first run for this product at this vendor" in html
    assert ">new<" in html


def test_drop_reasons_are_grouped():
    """The worker's raw drop reasons collapse into a few readable groups for the run summary."""
    assert _drop_reason_group("excluded_phrases_found: ['read']") == "excluded phrase"
    assert _drop_reason_group("phrases_not_found: ['rtx 5080']") == "search phrase missing"
    assert _drop_reason_group("Price 80.0 not greater than MinimumValue 100.0") == "below minimum price"
    assert _drop_reason_group("missing required field(s): price") == "missing fields"
    assert _drop_reason_group("condition_for_parts: 'For parts'") == "for parts"


def test_old_job_urls_redirect_to_runs(client):
    """/jobs and /jobs/<id> are permanent redirects, keeping list filters."""
    r = client.get("/jobs?status=error&vendor_id=usa_ebay")
    assert r.status_code == 301
    assert "/runs?" in r.headers["Location"]
    assert "status=error" in r.headers["Location"] and "vendor_id=usa_ebay" in r.headers["Location"]
    r = client.get("/jobs/abc123")
    assert r.status_code == 301 and r.headers["Location"].endswith("/runs/abc123")


def test_dead_filter_shows_the_dead_letter_stream(client, fake_redis, settings):
    """Dead-letter entries (and why the worker gave up) appear on Runs under the dead filter, not on the Nodes page."""
    fake_redis.xadd(settings.deadletter_stream,
                    {"payload": json.dumps({"job_id": "job-dead-1"}), "reason": "max_attempts_exceeded"})
    html = client.get("/runs?status=dead").get_data(as_text=True)
    assert "Dead-letter stream" in html
    assert "max_attempts_exceeded" in html
    assert 'href="/runs/job-dead-1"' in html

    assert "Dead-letter stream" not in client.get("/runs").get_data(as_text=True)
    assert "max_attempts_exceeded" not in client.get("/nodes").get_data(as_text=True)
