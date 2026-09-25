from __future__ import annotations

from datetime import datetime, timedelta, timezone

from orchestrator.benchmarks import (
    SOURCE,
    get_gpu_benchmarks,
    gpu_brand,
    match_benchmark,
    normalize_gpu_name,
    parse_hierarchy,
)
from orchestrator.db import session_scope
from orchestrator.models import (
    BenchmarkSnapshot,
    Job,
    Listing,
    PriceObservation,
    QueryObject,
    TrackedListing,
    Vendor,
)
from orchestrator.ui.cost_per_frame import cost_per_frame_rows, recompute_observations

# trimmed-down copy of the markup Tom's Hardware serves for the hierarchy tables
HIERARCHY_HTML = """
<html><body>
<h2>Rasterization GPU Benchmarks Rankings 2026</h2>
<table class="table__wrapper">
<caption>GPU Benchmarks Hierarchy &mdash; Rasterization Performance</caption>
<thead><tr><th><p>Graphics Card</p></th><th><p>MSRP</p></th><th><p>1080p Ultra</p></th>
<th><p>1440p Ultra</p></th><th><p>4K Ultra</p></th></tr></thead>
<tbody>
<tr><td><p><a href="#">GeForce RTX 5090</a></p></td><td>1999.99</td><td>100.0% (203.8)</td><td>100.0% (167.3)</td><td>100.0% (110.8)</td></tr>
<tr><td><p><a href="#">GeForce RTX 4060 Ti 16GB</a></p></td><td>$449.99</td><td>40.0% (81.5)</td><td>35.0% (58.6)</td><td>30.0% (33.2)</td></tr>
<tr><td><p><a href="#">GeForce RTX 4060 Ti 8GB</a></p></td><td>$399.99</td><td>39.0% (79.5)</td><td>34.0% (56.9)</td><td>28.0% (31.0)</td></tr>
<tr><td><p><a href="#">Radeon RX 6600</a></p></td><td>329.99</td><td>25.5% (51.9)</td><td>14.9% (24.8)</td><td>13.1% (14.5)</td></tr>
</tbody></table>
<h2>Ray Tracing GPU Benchmarks Rankings 2026</h2>
<table class="table__wrapper">
<caption>GPU Benchmarks Hierarchy &mdash; Ray Tracing Performance</caption>
<thead><tr><th>Graphics Card</th><th>MSRP</th><th>1080p Ultra</th><th>1440p Ultra</th><th>4K Ultra</th></tr></thead>
<tbody>
<tr><td>GeForce RTX 5090</td><td>$1,999.99</td><td>100.0% (125.7)</td><td>100.0% (100.8)</td><td>100.0% (64.1)</td></tr>
<tr><td>Radeon RX 6600</td><td>$329.99</td><td>10.2% (12.8)</td><td>7.6% (7.7)</td><td>4.8% (3.1)</td></tr>
</tbody></table>
</body></html>
"""

VENDOR = "usa_ebay"


def test_parse_hierarchy_reads_both_tables():
    """Both the rasterization and ray-tracing tables are parsed, with fps pulled out of the 'pct% (fps)' cells and MSRPs stripped of '$' and ','."""
    data = parse_hierarchy(HIERARCHY_HTML)
    assert [r["name"] for r in data["raster"]] == [
        "GeForce RTX 5090", "GeForce RTX 4060 Ti 16GB", "GeForce RTX 4060 Ti 8GB", "Radeon RX 6600",
    ]
    assert data["raster"][0] == {
        "name": "GeForce RTX 5090", "msrp": 1999.99,
        "fps": {"1080p": 203.8, "1440p": 167.3, "4k": 110.8},
    }
    assert data["rt"][0]["msrp"] == 1999.99
    assert data["rt"][1]["fps"]["4k"] == 3.1


def test_match_benchmark_normalises_brand_and_memory_variants():
    """Query phrases match hierarchy names without the GeForce/Radeon prefix, and a query with no memory size falls back to the smallest sized variant."""
    rows = parse_hierarchy(HIERARCHY_HTML)["raster"]
    assert normalize_gpu_name("GeForce RTX 4060 Ti 16 GB") == "rtx 4060 ti 16gb"
    assert match_benchmark("RTX 5090", rows)["name"] == "GeForce RTX 5090"
    assert match_benchmark("rx 6600", rows)["name"] == "Radeon RX 6600"
    assert match_benchmark("RTX 4060 Ti 16GB", rows)["name"] == "GeForce RTX 4060 Ti 16GB"
    assert match_benchmark("RTX 4060 Ti", rows)["name"] == "GeForce RTX 4060 Ti 8GB"
    # a sized query never falls back to a different size, and unknown cards don't match
    assert match_benchmark("RTX 4060 Ti 12GB", rows) is None
    assert match_benchmark("GTX 1080", rows) is None
    assert match_benchmark("RTX 509", rows) is None


def test_gpu_brand_from_hierarchy_and_query_names():
    """Brand is inferred from either the hierarchy's full name or a bare query phrase, and unknown names give None."""
    assert gpu_brand("GeForce RTX 5090") == "nvidia"
    assert gpu_brand("GTX 1080 Ti") == "nvidia"
    assert gpu_brand("Titan RTX") == "nvidia"
    assert gpu_brand("Radeon RX 6600") == "amd"
    assert gpu_brand("R9 Fury X") == "amd"
    assert gpu_brand("Intel Arc B580") == "intel"
    assert gpu_brand("Arc A750") == "intel"
    assert gpu_brand("9800X3D") is None


def test_benchmarks_cached_for_24_hours(settings):
    """A fresh snapshot is served from the DB without fetching; one older than 24 hours is re-fetched and overwritten."""
    calls = []

    def fake_fetch():
        calls.append(1)
        return parse_hierarchy(HIERARCHY_HTML)

    now = datetime.now(timezone.utc)
    with session_scope() as db:
        first = get_gpu_benchmarks(db, fetch=fake_fetch, now=now)
    assert first.refreshed and len(calls) == 1

    with session_scope() as db:
        cached = get_gpu_benchmarks(db, fetch=fake_fetch, now=now + timedelta(hours=23))
    assert not cached.refreshed and len(calls) == 1
    assert cached.data["raster"][0]["name"] == "GeForce RTX 5090"

    later = now + timedelta(hours=25)
    with session_scope() as db:
        refetched = get_gpu_benchmarks(db, fetch=fake_fetch, now=later)
    assert refetched.refreshed and len(calls) == 2
    with session_scope() as db:
        stored = db.get(BenchmarkSnapshot, SOURCE).fetched_at
    assert stored.replace(tzinfo=timezone.utc) == later  # SQLite drops the offset


def test_failed_refetch_falls_back_to_stale_snapshot(settings):
    """When a re-fetch fails, the stale snapshot is still returned (with the error), rather than blanking the page."""
    old = datetime.now(timezone.utc) - timedelta(days=3)
    with session_scope() as db:
        db.add(BenchmarkSnapshot(source=SOURCE, url="x", fetched_at=old, data=parse_hierarchy(HIERARCHY_HTML)))

    def broken():
        raise RuntimeError("503")

    with session_scope() as db:
        result = get_gpu_benchmarks(db, fetch=broken)
    assert result.error and "503" in result.error
    assert result.data["raster"]
    assert not result.refreshed

    with session_scope() as db:
        empty = get_gpu_benchmarks(db, fetch=broken, force=True)
    assert empty.error and empty.data["raster"]  # still stale data, not nothing


def _seed_prices():
    now = datetime.now(timezone.utc)
    with session_scope() as db:
        db.add(Vendor(identifier=VENDOR, name="eBay US", config={}))
        db.add(QueryObject(id="gpu-rtx-5090", content="RTX 5090"))
        db.add(QueryObject(id="gpu-rx-6600", content="RX 6600"))
        db.add(QueryObject(id="gpu-rtx-4060-ti", content="RTX 4060 Ti"))  # benchmarked, never priced
        db.add(QueryObject(id="gpu-gtx-1080", content="GTX 1080"))  # priced, not benchmarked
        for qid, median, at in [
            ("gpu-rtx-5090", 3000.0, now - timedelta(days=2)),
            ("gpu-rtx-5090", 2509.0, now),  # latest wins
            ("gpu-rx-6600", 186.0, now),
            ("gpu-gtx-1080", 120.0, now),
        ]:
            db.add(PriceObservation(
                vendor_id=VENDOR, query_object_id=qid, observed_date=at.date(), observed_at=at,
                job_id=f"j-{qid}-{at.timestamp()}", status="ok",
                price_low=median - 50, price_mean=median, price_median=median,
            ))


def test_cost_per_frame_rows_uses_latest_price_and_sorts_cheapest_first(settings):
    """Each matched GPU's latest price at the vendor is divided by its fps at the chosen resolution; rows are cheapest-per-frame first, and unmatched / unpriced GPUs are reported separately."""
    _seed_prices()
    bench = parse_hierarchy(HIERARCHY_HTML)["raster"]
    with session_scope() as db:
        rows, unmatched, unpriced = cost_per_frame_rows(db, bench, VENDOR, "1440p")
        unmatched_ids = [q.id for q in unmatched]

    assert [r["query_id"] for r in rows] == ["gpu-rx-6600", "gpu-rtx-5090"]
    assert [r["brand"] for r in rows] == ["amd", "nvidia"]
    assert rows[0]["cost_per_frame"] == round(186.0 / 24.8, 2)
    assert rows[1]["price"] == 2509.0
    assert rows[1]["cost_per_frame"] == round(2509.0 / 167.3, 2)
    assert rows[1]["msrp_cost_per_frame"] == round(1999.99 / 167.3, 2)
    assert unmatched_ids == ["gpu-gtx-1080"]
    assert [u["gpu"] for u in unpriced] == ["GeForce RTX 4060 Ti 8GB"]

    with session_scope() as db:
        low_rows, _, _ = cost_per_frame_rows(db, bench, VENDOR, "1440p", "price_low")
    assert {r["query_id"]: r["price"] for r in low_rows}["gpu-rtx-5090"] == 2459.0


def test_cost_per_frame_page_renders_and_exports(client, monkeypatch):
    """The page renders a bar per priced GPU for the selected vendor without touching the network (fetch is stubbed), and the CSV export carries the same rows."""
    _seed_prices()
    # get_gpu_benchmarks binds fetch_hierarchy as a default at def time, so stub
    # the fetch by wrapping the function where the view looks it up
    from orchestrator import benchmarks
    from orchestrator.ui import cost_per_frame as view

    monkeypatch.setattr(
        view, "get_gpu_benchmarks",
        lambda db, **kw: benchmarks.get_gpu_benchmarks(db, fetch=lambda: parse_hierarchy(HIERARCHY_HTML), **kw),
    )

    resp = client.get(f"/cost-per-frame?vendor={VENDOR}&mode=raster&res=1440p")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert html.count('class="cpf-row brand-amd"') == 1
    assert html.count('class="cpf-row brand-nvidia"') == 1
    assert html.count("cpf-bar--fps") == 2  # a framerate bar under every cost bar
    assert "24.8 fps" in html and "167.3 fps" in html
    assert "cpf-swatch brand-intel" not in html  # legend only lists brands on the chart
    assert "Radeon RX 6600" in html and "GeForce RTX 5090" in html
    assert "$7.50" in html  # 186 / 24.8

    rt = client.get(f"/cost-per-frame?vendor={VENDOR}&mode=rt&res=4k").get_data(as_text=True)
    assert "$60.00" in rt  # 186 / 3.1

    csv = client.get(f"/cost-per-frame/export.csv?vendor={VENDOR}&mode=raster&res=1440p").get_data(as_text=True)
    assert csv.splitlines()[0].startswith("gpu,brand,query_id,query,price")
    assert len(csv.strip().splitlines()) == 3


def test_cost_per_frame_page_survives_fetch_failure(client, monkeypatch):
    """With no cached data and an unreachable source, the page still renders with an error notice instead of a 500."""
    from orchestrator import benchmarks
    from orchestrator.ui import cost_per_frame as view

    def broken():
        raise RuntimeError("connection refused")

    monkeypatch.setattr(
        view, "get_gpu_benchmarks", lambda db, **kw: benchmarks.get_gpu_benchmarks(db, fetch=broken, **kw)
    )
    resp = client.get("/cost-per-frame")
    assert resp.status_code == 200
    assert "Couldn't fetch benchmark data" in resp.get_data(as_text=True)


def test_refresh_prices_recomputes_from_remaining_listings(client):
    """The page's refresh button rebuilds each run's prices from its stored listings, leaving out deleted and hidden ones (and auctions for the low price); runs with no stored listings keep the worker's figures."""
    _seed_prices()
    now = datetime.now(timezone.utc)
    with session_scope() as db:
        obs = db.query(PriceObservation).filter_by(query_object_id="gpu-rx-6600").one()
        db.add(Job(job_id=obs.job_id, vendor_id=VENDOR, query_object_id="gpu-rx-6600", status="ok"))
        tracked = TrackedListing(vendor_id=VENDOR, query_object_id="gpu-rx-6600",
                                 canonical_url="https://x/4", hidden=True, first_seen_at=now, last_seen_at=now)
        db.add(tracked)
        db.flush()
        for price, listing_type, hidden, tracked_id in [
            (150.0, "auction", False, None),
            (200.0, "buy_now", False, None),
            (220.0, "buy_now", False, None),
            (20.0, "buy_now", True, None),  # hidden listing
            (25.0, "buy_now", False, tracked.id),  # hidden via its tracked listing
        ]:
            db.add(Listing(job_id=obs.job_id, price=price, listing_type=listing_type,
                           hidden=hidden, tracked_listing_id=tracked_id))

    resp = client.post("/cost-per-frame/recompute", data={"vendor": VENDOR, "next": "/cost-per-frame"})
    assert resp.status_code == 302

    with session_scope() as db:
        rx = db.query(PriceObservation).filter_by(query_object_id="gpu-rx-6600").one()
        assert (rx.price_median, rx.price_mean, rx.price_low, rx.listings_kept) == (200.0, 190.0, 200.0, 3)
        untouched = db.query(PriceObservation).filter_by(query_object_id="gpu-gtx-1080").one()
        assert untouched.price_median == 120.0
        assert recompute_observations(db, VENDOR) == 0  # idempotent
