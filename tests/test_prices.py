"""Stored run prices follow the listings people can still see.

A run's price summary is written at ingest; hiding, unhiding or deleting a
listing afterwards has to recompute every run it was kept in, or a listing
nobody can find keeps setting the low price (the RTX 3070 "$130" case).
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

from orchestrator.db import session_scope
from orchestrator.models import Listing, PriceObservation, QueryObject, TrackedListing
from orchestrator.prices import recompute_observations

RESULT = json.loads(
    (Path(__file__).parent / "fixtures" / "scrape_result.example.json").read_text(encoding="utf-8")
)
PATH = "/api/v1/ingest/scrape-results"
AUTH = {"Authorization": "Bearer test-token"}
QID = "gpu-rtx-5090"


def _run(client, job_id: str, at: str, listings: list[tuple[int, float, str]], *, status: str = "ok"):
    """Ingest a run; each listing is (n, price, title) and keeps the URL /itm/<n> across runs."""
    payload = copy.deepcopy(RESULT)
    payload.update(job_id=job_id, observed_at=at, observed_date=at[:10], status=status,
                   listings=[{"title": title, "link": f"https://www.ebay.com/itm/{n}", "price": price, "extra": {}}
                             for n, price, title in listings])
    assert client.post(PATH, json=payload, headers=AUTH).status_code == 202


def _obs(job_id: str) -> PriceObservation:
    with session_scope() as db:
        o = db.query(PriceObservation).filter_by(job_id=job_id).one()
        db.expunge(o)
        return o


def _tracked_id(n: int) -> int:
    with session_scope() as db:
        return db.query(TrackedListing).filter_by(canonical_url=f"https://www.ebay.com/itm/{n}").one().id


CHEAP = (1, 130.0, "GIGABYTE RTX 3070 Triple Fan")
REST = [(2, 189.0, "RTX 3070 card"), (3, 260.0, "RTX 3070 card"), (4, 290.0, "RTX 3070 card")]


def test_deleting_a_listing_recomputes_every_run_it_was_in(client):
    """Deleting the $130 listing takes it out of both runs it appeared in, so neither keeps it as the low."""
    _run(client, "r1", "2026-09-25T21:19:00Z", [CHEAP, *REST])
    _run(client, "r2", "2026-09-26T03:19:00Z", [CHEAP, *REST])
    assert _obs("r1").price_low == 130.0

    r = client.post(f"/tracked/{_tracked_id(1)}/delete")
    assert r.status_code == 302
    assert _obs("r1").price_low == 189.0 and _obs("r2").price_low == 189.0
    with session_scope() as db:
        rows = db.query(Listing).filter_by(price=130.0).all()
        assert rows and all(l.hidden and l.tracked_listing_id is None for l in rows)  # kept on the run, hidden


def test_deleting_a_runs_only_listing_clears_its_prices(client):
    """If the deleted listing was all a run kept, the run ends up with no price rather than the old one."""
    _run(client, "solo", "2026-09-25T21:19:00Z", [CHEAP])
    client.post(f"/tracked/{_tracked_id(1)}/delete")
    o = _obs("solo")
    assert (o.price_low, o.price_median, o.listings_kept) == (None, None, 0)


def test_hiding_and_unhiding_recompute_every_run(client):
    """Hide drops the listing from both runs' prices; unhide puts it back."""
    _run(client, "r1", "2026-09-25T21:19:00Z", [CHEAP, *REST])
    _run(client, "r2", "2026-09-26T03:19:00Z", [CHEAP, *REST])
    tid = _tracked_id(1)
    client.post(f"/tracked/{tid}/hide")
    assert _obs("r1").price_low == 189.0 and _obs("r2").price_low == 189.0
    client.post(f"/tracked/{tid}/unhide")
    assert _obs("r1").price_low == 130.0 and _obs("r2").price_low == 130.0


def test_applying_excludes_recomputes_runs(client):
    """An exclude phrase applied to stored listings hides matches and recomputes their runs."""
    with session_scope() as db:
        db.add(QueryObject(id=QID, content="RTX 5090", exclude=["triple fan"]))
    _run(client, "r1", "2026-09-25T21:19:00Z", [CHEAP, *REST])
    client.post(f"/queries/{QID}/apply-excludes")
    assert _obs("r1").price_low == 189.0


def test_api_listing_hide_recomputes_its_run(client):
    """Hiding a single run row through the JSON API recomputes that run."""
    _run(client, "r1", "2026-09-25T21:19:00Z", [CHEAP, *REST])
    with session_scope() as db:
        lid = db.query(Listing).filter_by(job_id="r1", price=130.0).one().id
    assert client.post(f"/api/v1/listings/{lid}/hide").status_code == 200
    assert _obs("r1").price_low == 189.0


def test_recompute_ignores_rows_whose_tracked_listing_is_gone(client):
    """Existing data from before this fix: a tracked listing deleted under SQLite left the run's row pointing at
    nothing. Refresh prices must treat that row as gone, not as a visible listing."""
    _run(client, "r1", "2026-09-25T21:19:00Z", [CHEAP, *REST])
    tid = _tracked_id(1)
    with session_scope() as db:
        db.query(TrackedListing).filter_by(id=tid).delete()  # the old delete: row left dangling
    with session_scope() as db:
        assert db.query(Listing).filter_by(tracked_listing_id=tid).count() == 1
        assert recompute_observations(db, "usa_ebay") == 1
    assert _obs("r1").price_low == 189.0


def test_cost_per_frame_marks_prices_from_an_older_run():
    """When a GPU's newest run at the vendor has no price (it failed), the price shown is flagged as older."""
    from datetime import datetime, timedelta, timezone

    from orchestrator.ui.cost_per_frame import cost_per_frame_rows

    now = datetime.now(timezone.utc)
    bench = [{"name": "GeForce RTX 5090", "msrp": 1999.0, "fps": {"1440p": 100.0}}]
    with session_scope() as db:
        db.add(QueryObject(id=QID, content="RTX 5090"))
        for job, at, status, median in [("old", now - timedelta(hours=12), "ok", 2500.0),
                                        ("failed", now, "error", None)]:
            db.add(PriceObservation(vendor_id="usa_ebay", query_object_id=QID, observed_date=at.date(),
                                    observed_at=at, job_id=job, status=status, price_median=median))
    with session_scope() as db:
        rows, _, _ = cost_per_frame_rows(db, bench, "usa_ebay", "1440p")
    assert rows[0]["price"] == 2500.0
    assert rows[0]["from_older_run"] is True
