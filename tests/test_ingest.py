from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestrator.db import session_scope
from orchestrator.models import DroppedListing, Job, Listing, PriceObservation

RESULT = json.loads(
    (Path(__file__).parent / "fixtures" / "scrape_result.example.json").read_text(encoding="utf-8")
)

PATH = "/api/v1/ingest/scrape-results"
AUTH = {"Authorization": "Bearer test-token"}


def test_ingest_stores_observation_job_and_listings(client):
    """A single valid ingest creates the PriceObservation, upserts the Job, and stores kept Listing rows."""
    resp = client.post(PATH, json=RESULT, headers={**AUTH, "Idempotency-Key": RESULT["job_id"]})
    assert resp.status_code == 202
    assert resp.get_json() == {"accepted": True}

    with session_scope() as db:
        obs = db.query(PriceObservation).one()
        assert obs.job_id == RESULT["job_id"]
        assert obs.price_mean == 2350.55
        job = db.get(Job, RESULT["job_id"])
        assert job.status == "ok"
        assert job.duration_ms == 4210
        assert db.query(Listing).count() == 1


def test_ingest_is_idempotent_on_replay(client):
    """Re-posting the exact same result (e.g. a worker retry) upserts in place -- no duplicate rows."""
    for _ in range(3):
        assert client.post(PATH, json=RESULT, headers=AUTH).status_code == 202
    with session_scope() as db:
        assert db.query(PriceObservation).count() == 1
        assert db.query(Listing).count() == 1


def test_ingest_rejects_missing_token(client):
    """No Authorization header, or the wrong bearer token, both get a 401 -- never silently accepted."""
    assert client.post(PATH, json=RESULT).status_code == 401
    assert client.post(PATH, json=RESULT, headers={"Authorization": "Bearer nope"}).status_code == 401


def test_ingest_422_on_missing_fields(client):
    """A body missing required top-level fields is a 422 with a helpful error, not a 500 or a silent partial write."""
    bad = {"job_id": "x", "vendor_id": "v"}
    resp = client.post(PATH, json=bad, headers=AUTH)
    assert resp.status_code == 422
    assert "missing fields" in resp.get_json()["error"]


def test_ingest_stores_dropped_listings_with_reasons(client):
    """Every entry in the result's 'dropped' list becomes its own DroppedListing row, title and reason intact."""
    payload = {
        **RESULT,
        "job_id": "job-with-drops",
        "dropped": [
            {"title": "Shop on eBay", "reason": "Price 20.0 not greater than MinimumValue 500.0"},
            {"title": None, "reason": "missing required field(s): price"},
        ],
    }
    resp = client.post(PATH, json=payload, headers=AUTH)
    assert resp.status_code == 202

    with session_scope() as db:
        rows = db.query(DroppedListing).filter_by(job_id="job-with-drops").all()
        assert len(rows) == 2
        reasons = {r.reason for r in rows}
        assert "Price 20.0 not greater than MinimumValue 500.0" in reasons
        titles = {r.title for r in rows}
        assert None in titles
        assert "Shop on eBay" in titles


def test_run_page_shows_dropped_listing_reasons(client):
    """The rendered run page actually surfaces a dropped listing's title and reason to the operator."""
    job_id = "job-detail-drops"
    payload = {
        **RESULT,
        "job_id": job_id,
        "dropped": [{"title": "Sketchy Bundle", "reason": "excluded_phrases_found: ['bundle']"}],
    }
    assert client.post(PATH, json=payload, headers=AUTH).status_code == 202

    page = client.get(f"/runs/{job_id}")
    assert page.status_code == 200
    assert b"Sketchy Bundle" in page.data
    assert b"excluded_phrases_found" in page.data


def test_ingest_replay_replaces_dropped_listings(client):
    """A replay with a different (or empty) dropped list replaces the old rows rather than appending to them."""
    job_id = "job-replay-drops"
    first = {**RESULT, "job_id": job_id, "dropped": [{"title": "A", "reason": "no price found"}]}
    assert client.post(PATH, json=first, headers=AUTH).status_code == 202
    with session_scope() as db:
        assert db.query(DroppedListing).filter_by(job_id=job_id).count() == 1

    # a replay with no drops must clear the old ones, not accumulate
    second = {**RESULT, "job_id": job_id, "dropped": []}
    assert client.post(PATH, json=second, headers=AUTH).status_code == 202
    with session_scope() as db:
        assert db.query(DroppedListing).filter_by(job_id=job_id).count() == 0


@pytest.mark.parametrize("status", ["ok", "partial", "error"])
def test_ingest_records_status_and_error(client, status):
    """The job's status matches the reported result, and an error result also records its error type."""
    payload = {**RESULT, "job_id": f"job-{status}", "status": status}
    if status == "error":
        payload["error"] = {"type": "ChallengePage", "message": "blocked"}
    assert client.post(PATH, json=payload, headers=AUTH).status_code == 202
    with session_scope() as db:
        job = db.get(Job, f"job-{status}")
        assert job.status == status
        if status == "error":
            assert job.error_type == "ChallengePage"


def _with_listings(listings, job_id="00000000-0000-0000-0000-0000000000a1"):
    return {**RESULT, "job_id": job_id, "listings": listings}


def test_ingest_price_low_skips_auction_listings(client):
    """price_low is the cheapest non-auction listing: a lower current bid on an auction doesn't count, whatever the worker reported."""
    payload = _with_listings([
        {"title": "RTX 5090 - 3 bids", "link": "https://www.ebay.com/itm/1", "price": 900.0,
         "extra": {"subtitle": "Time left 2d 4h"}},
        {"title": "RTX 5090 Buy It Now", "link": "https://www.ebay.com/itm/2", "price": 2100.0, "extra": {}},
        {"title": "RTX 5090 Founders Edition", "link": "https://www.ebay.com/itm/3", "price": 2050.0, "extra": {}},
    ])
    assert client.post(PATH, json=payload, headers=AUTH).status_code == 202
    with session_scope() as db:
        obs = db.query(PriceObservation).one()
        assert obs.price_low == 2050.0
        # the auction is still stored as a listing -- only the low-price figure skips it
        assert db.query(Listing).filter(Listing.listing_type == "auction").count() == 1


def test_ingest_price_low_none_when_only_auctions(client):
    """A run that found only auctions has no buyable low price, rather than falling back to a bid."""
    payload = _with_listings([
        {"title": "RTX 5090 auction", "link": "https://www.ebay.com/itm/1", "price": 900.0, "extra": {"bids": "5 bids"}},
    ])
    assert client.post(PATH, json=payload, headers=AUTH).status_code == 202
    with session_scope() as db:
        assert db.query(PriceObservation).one().price_low is None


def test_ingest_price_low_falls_back_to_worker_metric_without_listings(client):
    """With no listings in the result there's nothing to filter, so the worker's reported price_low is kept."""
    assert client.post(PATH, json=_with_listings([]), headers=AUTH).status_code == 202
    with session_scope() as db:
        assert db.query(PriceObservation).one().price_low == RESULT["metrics"]["price_low"]
