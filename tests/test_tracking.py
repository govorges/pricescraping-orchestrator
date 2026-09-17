from __future__ import annotations

from orchestrator.db import session_scope
from orchestrator.models import Listing, ListingPricePoint, TrackedListing

PATH = "/api/v1/ingest/scrape-results"
AUTH = {"Authorization": "Bearer test-token"}


def _result(job_id, link, title, extra, price):
    return {
        "job_id": job_id,
        "vendor_id": "usa_ebay",
        "query_object_id": "q1",
        "observed_date": "2026-09-01",
        "observed_at": "2026-09-01T10:00:00Z",
        "status": "ok",
        "duration_ms": 100,
        "metrics": {
            "listings_found": 1, "listings_kept": 1,
            "price_low": price, "price_mean": price, "price_median": price,
        },
        "listings": [{"title": title, "link": link, "image": None, "price": price, "extra": extra}],
    }


def test_same_listing_different_query_string_tracks_as_one(client):
    """Two scrapes of the same listing with different tracking/session query args resolve to one TrackedListing."""
    r1 = _result(
        "job-a", "https://www.ebay.com/itm/999?epid=abc&_trksid=1", "RTX 5090 FE",
        {"subtitle": "3 bids"}, 1500.0,
    )
    r2 = _result(
        "job-b", "https://www.ebay.com/itm/999?epid=def&_trksid=2", "RTX 5090 FE",
        {"subtitle": "5 bids"}, 1600.0,
    )
    assert client.post(PATH, json=r1, headers=AUTH).status_code == 202
    assert client.post(PATH, json=r2, headers=AUTH).status_code == 202

    with session_scope() as db:
        tracked = db.query(TrackedListing).one()
        assert tracked.canonical_url == "https://www.ebay.com/itm/999"
        assert tracked.first_price == 1500.0
        assert tracked.last_price == 1600.0
        assert tracked.observations_count == 2
        assert tracked.listing_type == "auction"
        assert db.query(ListingPricePoint).count() == 2
        listings = db.query(Listing).order_by(Listing.job_id).all()
        assert all(x.tracked_listing_id == tracked.id for x in listings)
        assert all(x.listing_type == "auction" for x in listings)


def test_different_listings_are_not_merged(client):
    """Two genuinely different listing URLs stay as two separate TrackedListing rows, each with its own type."""
    r1 = _result("job-a", "https://www.ebay.com/itm/1", "A", {}, 100.0)
    r2 = _result("job-b", "https://www.ebay.com/itm/2", "B", {"subtitle": "Buy It Now"}, 200.0)
    client.post(PATH, json=r1, headers=AUTH)
    client.post(PATH, json=r2, headers=AUTH)
    with session_scope() as db:
        assert db.query(TrackedListing).count() == 2
        b = db.query(TrackedListing).filter(TrackedListing.canonical_url.contains("itm/2")).one()
        assert b.listing_type == "buy_now"


def test_listing_without_link_is_not_tracked(client):
    """A listing with no link can't be canonicalised, so it's stored on the job but never becomes a TrackedListing."""
    r = _result("job-a", None, "No link item", {}, 50.0)
    assert client.post(PATH, json=r, headers=AUTH).status_code == 202
    with session_scope() as db:
        assert db.query(TrackedListing).count() == 0
        listing = db.query(Listing).one()
        assert listing.tracked_listing_id is None


def test_replay_is_idempotent_for_tracking(client):
    """Re-ingesting the identical job doesn't inflate observations_count or duplicate the price-point history."""
    r = _result("job-a", "https://www.ebay.com/itm/999?epid=abc", "RTX 5090", {"subtitle": "3 bids"}, 1500.0)
    for _ in range(3):
        assert client.post(PATH, json=r, headers=AUTH).status_code == 202
    with session_scope() as db:
        assert db.query(TrackedListing).count() == 1
        tracked = db.query(TrackedListing).one()
        assert tracked.observations_count == 1
        assert db.query(ListingPricePoint).count() == 1
