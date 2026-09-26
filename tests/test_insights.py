from __future__ import annotations

from datetime import datetime, timedelta, timezone

from orchestrator.db import session_scope
from orchestrator.models import Category, PriceObservation, QueryObject, TrackedListing, Vendor
from orchestrator.ui.insights import _leaderboard, _sort_leaderboard

VENDOR = "usa_ebay"


def _seed_basic():
    with session_scope() as db:
        db.add(Vendor(identifier=VENDOR, name="eBay US", config={}))
        db.add(Category(id="gpu", name="GPUs"))
        db.add(QueryObject(id="gpu-rtx-5090", content="RTX 5090", category_id="gpu"))
        db.add(QueryObject(id="gpu-rtx-4090", content="RTX 4090", category_id="gpu"))
        db.add(QueryObject(id="cpu-9800x3d", content="9800X3D"))  # no category, no observations


def _obs(db, *, qid, observed_at, median, low=None, job_id=None):
    db.add(
        PriceObservation(
            vendor_id=VENDOR, query_object_id=qid, observed_date=observed_at.date(),
            observed_at=observed_at, job_id=job_id or f"j-{qid}-{observed_at.timestamp()}",
            status="ok", price_low=low or median, price_mean=median, price_median=median,
            listings_found=5, listings_kept=3,
        )
    )


def test_leaderboard_computes_price_change_and_reach(settings):
    """Latest price and 7-day % change are computed per query, and a query with zero observations still appears with null values instead of being dropped or crashing."""
    _seed_basic()
    now = datetime.now(timezone.utc)
    with session_scope() as db:
        _obs(db, qid="gpu-rtx-5090", observed_at=now - timedelta(days=10), median=2000)
        _obs(db, qid="gpu-rtx-5090", observed_at=now, median=1800)  # dropped 10%
        _obs(db, qid="gpu-rtx-4090", observed_at=now, median=1200)

    with session_scope() as db:
        rows = {r["id"]: r for r in _leaderboard(db)}

    assert rows["gpu-rtx-5090"]["latest_price"] == 1800
    assert rows["gpu-rtx-5090"]["change_pct"] == -10.0
    assert rows["gpu-rtx-5090"]["observations"] == 2
    assert rows["gpu-rtx-4090"]["latest_price"] == 1200
    # a query with zero observations must still appear, with sane empty values
    assert rows["cpu-9800x3d"]["latest_price"] is None
    assert rows["cpu-9800x3d"]["change_pct"] is None
    assert rows["cpu-9800x3d"]["observations"] == 0


def test_leaderboard_filters_by_category(settings):
    """Passing category_id restricts the leaderboard to that category's queries, excluding uncategorised ones."""
    _seed_basic()
    with session_scope() as db:
        rows = _leaderboard(db, category_id="gpu")
    assert {r["id"] for r in rows} == {"gpu-rtx-5090", "gpu-rtx-4090"}


def test_sort_leaderboard_price_desc():
    """Sorting by price descending still pushes rows with no price to the very end, not the front."""
    rows = [
        {"id": "a", "content": "A", "latest_price": 100, "change_pct": None,
         "observations": 1, "active_listings": 0},
        {"id": "b", "content": "B", "latest_price": 300, "change_pct": None,
         "observations": 1, "active_listings": 0},
        {"id": "c", "content": "C", "latest_price": None, "change_pct": None,
         "observations": 0, "active_listings": 0},
    ]
    ordered = [r["id"] for r in _sort_leaderboard(rows, "price_desc")]
    assert ordered == ["b", "a", "c"]  # missing price sorts last even descending


def test_products_page_renders(client):
    """The Products list (which replaced the Insights leaderboard) loads and lists a known query object."""
    _seed_basic()
    r = client.get("/products")
    assert r.status_code == 200
    assert b"gpu-rtx-5090" in r.data


def test_old_insights_pages_redirect_to_products(client):
    """/insights and /insights/<qid> are permanent redirects to their Products equivalents, keeping the category filter."""
    _seed_basic()
    r = client.get("/insights?category_id=gpu")
    assert r.status_code == 301
    assert r.headers["Location"].endswith("/products?category_id=gpu")
    r = client.get("/insights/gpu-rtx-5090")
    assert r.status_code == 301
    assert r.headers["Location"].endswith("/products/gpu-rtx-5090")


def test_product_overview_renders_with_zero_observations(client):
    """Regression: a query with no PriceObservation rows must not 500 --
    stats comes back as {} and every template access needs to survive that."""
    _seed_basic()
    r = client.get("/products/cpu-9800x3d")
    assert r.status_code == 200
    assert b"no observations yet" in r.data


def test_product_overview_renders_with_history(client):
    """With real observations and a tracked listing present, the Overview tab renders the vendor's data."""
    _seed_basic()
    now = datetime.now(timezone.utc)
    with session_scope() as db:
        _obs(db, qid="gpu-rtx-5090", observed_at=now - timedelta(days=1), median=1900, low=1800)
        _obs(db, qid="gpu-rtx-5090", observed_at=now, median=1800, low=1700)
        t = TrackedListing(
            vendor_id=VENDOR, query_object_id="gpu-rtx-5090", canonical_url="https://x/1",
            first_seen_at=now, last_seen_at=now, listing_type="buy_now",
        )
        db.add(t)

    r = client.get("/products/gpu-rtx-5090")
    assert r.status_code == 200
    assert b"usa_ebay" in r.data
    assert b"Recent runs" in r.data


def test_product_overview_404_for_unknown_query(client):
    """A query id that doesn't exist is a 404, not a rendered page with empty/garbage data."""
    r = client.get("/products/does-not-exist")
    assert r.status_code == 404


def test_export_leaderboard_csv(client):
    """The leaderboard CSV export downloads as an attachment with a proper header row."""
    _seed_basic()
    r = client.get("/insights/export.csv")
    assert r.status_code == 200
    assert r.mimetype == "text/csv"
    assert "attachment" in r.headers["Content-Disposition"]
    assert b"gpu-rtx-5090" in r.data
    assert r.data.decode().splitlines()[0].split(",")[0] == "id"


def test_export_leaderboard_json(client):
    """The leaderboard JSON export returns the same rows as valid, parseable JSON."""
    _seed_basic()
    r = client.get("/insights/export.json")
    assert r.status_code == 200
    assert r.mimetype == "application/json"
    ids = {row["id"] for row in r.get_json()}
    assert "gpu-rtx-5090" in ids


def test_export_bad_format_404s(client):
    """An export format other than csv/json is rejected rather than silently defaulting to one."""
    _seed_basic()
    assert client.get("/insights/export.xml").status_code == 404


def test_export_query_observations_csv(client):
    """A single query's raw observation history exports as CSV with the actual price data in it."""
    _seed_basic()
    now = datetime.now(timezone.utc)
    with session_scope() as db:
        _obs(db, qid="gpu-rtx-5090", observed_at=now, median=1800)

    r = client.get("/insights/gpu-rtx-5090/export.csv")
    assert r.status_code == 200
    assert r.mimetype == "text/csv"
    body = r.data.decode()
    assert "price_median" in body.splitlines()[0]
    assert "1800" in body


def test_export_category_observations_json(client):
    """A category-level export includes observations from every query object in that category."""
    _seed_basic()
    now = datetime.now(timezone.utc)
    with session_scope() as db:
        _obs(db, qid="gpu-rtx-5090", observed_at=now, median=1800)
        _obs(db, qid="gpu-rtx-4090", observed_at=now, median=1200)

    r = client.get("/insights/category/gpu/export.json")
    assert r.status_code == 200
    rows = r.get_json()
    assert {row["query_object_id"] for row in rows} == {"gpu-rtx-5090", "gpu-rtx-4090"}


def test_export_category_unknown_404s(client):
    """Exporting an unknown category id 404s instead of silently returning an empty file."""
    assert client.get("/insights/category/nope/export.csv").status_code == 404


def test_price_summary_does_not_mix_vendors():
    """Two vendors at very different price levels: the current price is the median of each vendor's latest,
    and the change averages each vendor's own move (-10% and +10%) instead of comparing one vendor with the other."""
    from orchestrator.ui.insights import price_summary

    now = datetime.now(timezone.utc)
    points = [
        (now - timedelta(days=10), 200.0, "usa_ebay"),
        (now - timedelta(days=10), 600.0, "usa_newegg"),
        (now - timedelta(hours=2), 660.0, "usa_newegg"),
        (now - timedelta(hours=1), 180.0, "usa_ebay"),
    ]
    s = price_summary(points, now)
    assert s["current"] == 420.0  # median of 180 and 660
    assert s["change_7d"] == 0.0  # (-10% + 10%) / 2 -- mixed, it read as -70% (600 -> 180)
    assert s["volatility"] == 20.0  # std dev 10 at eBay, 30 at Newegg, averaged


def test_price_summary_skips_stale_vendors_and_single_runs():
    """A vendor not scraped for over 30 days is left out of the current price, and a product whose
    vendors each have one run has a price but no change yet."""
    from orchestrator.ui.insights import price_summary

    now = datetime.now(timezone.utc)
    s = price_summary([
        (now - timedelta(days=90), 900.0, "old_vendor"),
        (now - timedelta(hours=1), 300.0, "usa_ebay"),
    ], now)
    assert s["current"] == 300.0
    assert s["vendors_in_current"] == 1
    assert s["change_7d"] is None
