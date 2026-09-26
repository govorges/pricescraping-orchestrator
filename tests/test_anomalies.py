from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from orchestrator.anomalies import (
    DEFAULT_RULES,
    _detect_move,
    parse_rules_form,
    preview_counts,
    rules_for,
    save_rules,
)
from orchestrator.db import session_scope
from orchestrator.models import (
    Anomaly,
    AnomalyRuleSet,
    Category,
    PriceObservation,
    QueryObject,
    TrackedListing,
    Vendor,
)

RESULT = json.loads(
    (Path(__file__).parent / "fixtures" / "scrape_result.example.json").read_text(encoding="utf-8")
)
PATH = "/api/v1/ingest/scrape-results"
AUTH = {"Authorization": "Bearer test-token"}
QID = "gpu-rtx-5090"
START = datetime(2026, 9, 1, tzinfo=timezone.utc)


def _run(client, n: int, prices: list[float], *, median: float | None = None, kept: int | None = None,
         found: int | None = None, types: dict[int, str] | None = None, titles: dict[int, str] | None = None):
    """Ingest run number ``n`` (6 hours apart) with one listing per price; listing i keeps the same URL
    across runs so it's tracked as the same listing."""
    at = (START + timedelta(hours=6 * n)).strftime("%Y-%m-%dT%H:%M:%SZ")
    payload = copy.deepcopy(RESULT)
    listings = []
    for i, price in enumerate(prices):
        extra = {"extra": "3 bids"} if (types or {}).get(i) == "auction" else {}
        listings.append({"title": (titles or {}).get(i, f"RTX 5090 card #{i}"),
                         "link": f"https://www.ebay.com/itm/{i}", "price": price, "extra": extra})
    ordered = sorted(prices)
    payload.update(job_id=f"run-{n}", observed_at=at, observed_date=at[:10], listings=listings)
    payload["metrics"] = {
        "listings_found": found if found is not None else len(prices),
        "listings_kept": kept if kept is not None else len(prices),
        "price_low": ordered[0] if ordered else None,
        "price_mean": sum(prices) / len(prices) if prices else None,
        "price_median": median if median is not None else (ordered[len(ordered) // 2] if ordered else None),
    }
    assert client.post(PATH, json=payload, headers=AUTH).status_code == 202


def _anomalies(kind: str | None = None) -> list[Anomaly]:
    with session_scope() as db:
        stmt = db.query(Anomaly)
        if kind:
            stmt = stmt.filter_by(kind=kind)
        rows = stmt.order_by(Anomaly.id).all()
        db.expunge_all()
        return rows


NORMAL = [1950.0, 1980.0, 2000.0, 2010.0, 2030.0, 2050.0]


# ---- detection ----------------------------------------------------------------


def test_inbox_counts_each_anomaly_once(client):
    """Regression: the type and status chips count anomalies, not anomalies squared."""
    _run(client, 1, [*NORMAL, 100.0, 7000.0])
    _run(client, 2, [*NORMAL, 110.0, 7100.0])
    inbox = client.get("/anomalies").get_data(as_text=True)
    assert ">Outlier listings 4<" in inbox and ">Open 4<" in inbox


def test_low_and_high_outliers_are_flagged_once_and_auctions_skipped(client):
    """A $100 'box only' and a $7,000 bundle are flagged against a ~$2,000 median; a low auction bid is skipped by
    default; replaying the same result raises nothing new."""
    prices = [*NORMAL, 100.0, 7000.0, 300.0]
    _run(client, 1, prices, types={8: "auction"})
    found = _anomalies("outlier")
    titles = sorted(a.title for a in found)
    assert titles == ["Listing priced 3.5× the median", "Listing priced 95% below the median"]
    assert {a.severity for a in found} == {"high"}
    low = next(a for a in found if a.details["side"] == "low")
    assert low.details["low_if_removed"] == 1950.0

    _run(client, 1, prices, types={8: "auction"})  # worker retry of the same run
    assert len(_anomalies("outlier")) == 2


def test_sudden_median_drop_is_flagged(client):
    """Four steady runs around $2,000, then a run at $1,500: flagged as a high-severity 25% drop."""
    for n, m in enumerate([2000, 2010, 1990, 2005], start=1):
        _run(client, n, NORMAL, median=m)
    _run(client, 5, NORMAL, median=1500)
    moves = _anomalies("move")
    assert len(moves) == 1
    assert moves[0].job_id == "run-5"
    assert moves[0].title == "Median fell 25% in one run"
    assert moves[0].severity == "high"


def test_sustained_move_is_flagged_once_by_the_run_that_completes_it():
    """A volatile history keeps the one-run check quiet; three rises in a row totalling 12.5% are flagged
    exactly when the streak reaches three, not again on the fourth."""
    def obs(i, m):
        return PriceObservation(job_id=f"j{i}", vendor_id="v", query_object_id="q", status="ok",
                                observed_at=START + timedelta(hours=6 * i), price_median=m, listings_kept=30)

    medians = [2000, 2200, 2000, 2200, 2000, 2050, 2150, 2250, 2300]
    history = [obs(i, m) for i, m in enumerate(medians)]
    cfg = DEFAULT_RULES["move"]
    assert _detect_move(history[7], history[:7], cfg)[0]["title"] == "Median rose 12% over 3 runs"
    assert _detect_move(history[8], history[:8], cfg) == []  # streak of 4: already reported
    assert _detect_move(history[6], history[:6], cfg) == []  # streak of 2: not yet


def test_listing_price_cut_near_the_median_is_flagged(client):
    """A buy-it-now listing dropping from $2,400 to $1,900 (-21%) at a ~$2,000 median is a price cut."""
    _run(client, 1, [*NORMAL, 2400.0])
    _run(client, 2, [*NORMAL, 1900.0])
    cuts = _anomalies("cut")
    assert len(cuts) == 1
    assert cuts[0].title == "Listing price cut 21%"
    assert cuts[0].details["before"] == 2400.0


def test_broken_looking_run_is_flagged_and_left_out_of_trends(client):
    """A run keeping 1 listing where 30 is normal is a health anomaly; by default the run is left out of trends,
    so it doesn't count toward product prices or against listings' 'still for sale' status."""
    from orchestrator.ui.insights import _leaderboard
    from orchestrator.ui.views import _run_times_by_vendor

    for n in range(1, 4):
        _run(client, n, NORMAL, kept=30, found=32)
    _run(client, 4, [5.0], median=5.0, kept=1, found=2)
    health = _anomalies("health")
    assert len(health) == 1 and health[0].title == "Only 1 listing kept, usually about 30"
    with session_scope() as db:
        broken = db.query(PriceObservation).filter_by(job_id="run-4").one()
        assert broken.excluded is True
        assert broken.observed_at not in _run_times_by_vendor(db, QID)["usa_ebay"]
        db.add(QueryObject(id=QID, content="RTX 5090"))
        db.flush()
        row = next(r for r in _leaderboard(db) if r["id"] == QID)
        assert row["latest_price"] == 2010.0  # the $5 run is ignored


def test_detection_failure_never_loses_the_result(client, monkeypatch):
    """If a check raises, the scrape result is still stored and the ingest still succeeds."""
    def boom(db, job_id):
        raise RuntimeError("bug in a check")

    monkeypatch.setattr("orchestrator.anomalies.detect_for_run", boom)
    _run(client, 1, NORMAL)
    with session_scope() as db:
        assert db.query(PriceObservation).filter_by(job_id="run-1").count() == 1


# ---- actions ---------------------------------------------------------------------


def test_hide_listing_action_hides_it_and_recomputes_the_run(client):
    """Hiding the outlier takes it out of the run's stored low price and marks the anomaly reviewed."""
    _run(client, 1, [*NORMAL, 100.0])
    a = _anomalies("outlier")[0]
    r = client.post(f"/anomalies/{a.id}/resolve", data={"action": "hide_listing"})
    assert r.status_code == 302
    with session_scope() as db:
        assert db.get(TrackedListing, a.tracked_listing_id).hidden is True
        assert db.query(PriceObservation).filter_by(job_id="run-1").one().price_low == 1950.0
        a2 = db.get(Anomaly, a.id)
        assert (a2.status, a2.resolution) == ("reviewed", "Listing hidden")


def test_exclude_phrase_action_adds_the_phrase_and_hides_matches(client):
    """'Exclude phrase' appends to the product's exclude list and hides stored listings that match it."""
    with session_scope() as db:
        db.add(QueryObject(id=QID, content="RTX 5090"))
    _run(client, 1, [*NORMAL, 100.0], titles={6: "RTX 5090 box only, no card"})
    a = _anomalies("outlier")[0]
    client.post(f"/anomalies/{a.id}/resolve", data={"action": "exclude_phrase", "phrase": "box only"})
    with session_scope() as db:
        assert "box only" in db.get(QueryObject, QID).exclude
        assert db.get(TrackedListing, a.tracked_listing_id).hidden is True
        assert db.get(Anomaly, a.id).resolution == 'Excluded "box only"'


def test_run_trends_toggle(client):
    """The run page can leave a run out of trends and put it back."""
    _run(client, 1, NORMAL)
    client.post("/runs/run-1/trends", data={})
    with session_scope() as db:
        assert db.query(PriceObservation).filter_by(job_id="run-1").one().excluded is True
    client.post("/runs/run-1/trends", data={"include": "1"})
    with session_scope() as db:
        assert db.query(PriceObservation).filter_by(job_id="run-1").one().excluded is False


# ---- rules ---------------------------------------------------------------------------


def test_rules_merge_defaults_then_category_and_store_only_changes(db_session):
    """Only numbers that differ from what a scope inherits are stored; a category override layers on top."""
    db_session.add(Category(id="gpu", name="GPUs"))
    rules = rules_for(db_session)
    rules["outlier"]["low_pct"] = 30
    save_rules(db_session, "default", rules)
    assert db_session.get(AnomalyRuleSet, "default").config == {"outlier": {"low_pct": 30}}

    cat_rules = rules_for(db_session, "gpu")
    cat_rules["move"]["min_pct"] = 20
    save_rules(db_session, "gpu", cat_rules)
    assert db_session.get(AnomalyRuleSet, "gpu").config == {"move": {"min_pct": 20}}

    merged = rules_for(db_session, "gpu")
    assert merged["outlier"]["low_pct"] == 30 and merged["move"]["min_pct"] == 20
    assert rules_for(db_session)["move"]["min_pct"] == DEFAULT_RULES["move"]["min_pct"]


def test_rules_form_parsing():
    """An unchecked rule is turned off, numbers are read, and junk falls back to the current value."""
    form = {"outlier.present": "1", "move.present": "1", "move.enabled": "on",
            "move.min_pct": "12.5", "move.window_days": "abc", "health.action": "flag", "cut.action": "delete-all"}
    rules = parse_rules_form(form, DEFAULT_RULES)
    assert rules["outlier"]["enabled"] is False
    assert rules["move"]["enabled"] is True and rules["move"]["min_pct"] == 12.5
    assert rules["move"]["window_days"] == DEFAULT_RULES["move"]["window_days"]
    assert rules["health"]["action"] == "flag"
    assert rules["cut"]["action"] == DEFAULT_RULES["cut"]["action"]


def test_preview_counts_without_saving(client):
    """'Test on last 30 days' reports what a looser rule would raise without saving anomalies."""
    now = datetime.now(timezone.utc)
    with session_scope() as db:
        db.add(Vendor(identifier="usa_ebay", name="eBay US", config={}))
    payload = copy.deepcopy(RESULT)
    payload.update(job_id="recent", observed_at=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                   observed_date=now.strftime("%Y-%m-%d"),
                   listings=[{"title": f"card {i}", "link": f"https://x/{i}", "price": p, "extra": {}}
                             for i, p in enumerate([*NORMAL, 1100.0])])
    payload["metrics"]["price_median"] = 2000.0
    client.post(PATH, json=payload, headers=AUTH)
    assert len(_anomalies("outlier")) == 0  # $1,100 is 55% of the median: above the 45% line

    with session_scope() as db:
        loose = rules_for(db)
        loose["outlier"]["low_pct"] = 60
        counts = preview_counts(db, loose)
    assert counts["outlier"] == 1
    assert len(_anomalies("outlier")) == 0


# ---- pages ------------------------------------------------------------------------------


def test_inbox_rules_and_product_pages_show_anomalies(client):
    """The inbox lists and explains the anomaly, the sidebar counts it, the product shows a banner, the run page
    links it, and the rules page renders, tests and saves."""
    with session_scope() as db:
        db.add(Vendor(identifier="usa_ebay", name="eBay US", config={}))
        db.add(QueryObject(id=QID, content="RTX 5090"))
    _run(client, 1, [*NORMAL, 100.0])
    a = _anomalies("outlier")[0]

    inbox = client.get("/anomalies").get_data(as_text=True)
    assert "Listing priced 95% below the median" in inbox
    assert ">Outlier listings 1<" in inbox and ">Open 1<" in inbox
    assert "Every listing kept in this run" in inbox  # the strip chart
    assert "Hide listing" in inbox and "Exclude phrase" in inbox
    assert 'title="1 open"' in inbox  # sidebar badge

    overview = client.get(f"/products/{QID}").get_data(as_text=True)
    assert "anom-banner" in overview and "1 open anomaly" in overview
    run = client.get("/runs/run-1").get_data(as_text=True)
    assert f"id={a.id}" in run

    assert client.get("/anomalies/rules").status_code == 200
    test = client.post("/anomalies/rules", data={"scope": "default", "intent": "test", "outlier.present": "1",
                                                 "outlier.enabled": "on", "outlier.low_pct": "45"})
    assert b"unsaved" in test.data
    saved = client.post("/anomalies/rules", data={"scope": "default", "intent": "save", "move.present": "1"})
    assert saved.status_code == 302
    assert rules_for_scope("default") == {"move": {"enabled": False}}


def rules_for_scope(scope: str) -> dict:
    with session_scope() as db:
        row = db.get(AnomalyRuleSet, scope)
        return dict(row.config) if row else {}
