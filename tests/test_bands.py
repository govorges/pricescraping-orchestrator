from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from orchestrator.bands import band_for, reference_median
from orchestrator.db import session_scope
from orchestrator.models import Category, PriceObservation, QueryObject, Schedule, Vendor

QID = "gpu-rtx-3070"
START = datetime(2026, 9, 1, tzinfo=timezone.utc)
VENDOR_CONFIG = {
    "metadata": {"name": "eBay US"},
    "url": {"start": "https://www.ebay.com/sch/i.html?_nkw="},
    "selectors": {"listings": "li.s-card", "price": {"css_selector": ".p", "type": "price", "required": True}},
}


def _seed(*, dynamic=True, floor=None, ceiling=None, minimum=100.0, maximum=2000.0):
    with session_scope() as db:
        db.add(Category(id="gpu", name="GPUs", minimum_value_floor=floor, maximum_value_ceiling=ceiling))
        db.add(QueryObject(id=QID, content="RTX 3070", category_id="gpu", dynamic_band=dynamic,
                           minimum_value=minimum, maximum_value=maximum))


def _obs(vendor: str, medians: list[float | None], *, status="ok", excluded=False, start=0):
    with session_scope() as db:
        for i, m in enumerate(medians, start=start):
            at = START + timedelta(hours=6 * i)
            db.add(PriceObservation(vendor_id=vendor, query_object_id=QID, observed_date=at.date(), observed_at=at,
                                    job_id=f"{vendor}-{status}-{i}", status=status, excluded=excluded,
                                    price_median=m))


def _band(vendor: str):
    with session_scope() as db:
        return band_for(db, db.get(QueryObject, QID), vendor)


def test_band_follows_each_vendors_own_median(settings):
    """Minimum is 70% and maximum 3x of the vendor's recent median -- eBay and Newegg get different bands."""
    _seed()
    _obs("usa_ebay", [240, 260, 250])
    _obs("usa_newegg", [640, 660])
    assert _band("usa_ebay") == (175.0, 750.0)  # median 250
    assert _band("usa_newegg") == (455.0, 1950.0)  # median 650


def test_fixed_band_until_enough_history_or_when_off(settings):
    """With one run (or the option off) the product's own fixed band -- with category rails -- is used."""
    _seed(floor=100.0)
    _obs("usa_ebay", [250])
    assert _band("usa_ebay") == (100.0, 2000.0)
    with session_scope() as db:
        db.get(QueryObject, QID).dynamic_band = False
    _obs("usa_ebay", [260], start=1)
    assert _band("usa_ebay") == (100.0, 2000.0)


def test_reference_ignores_failed_and_left_out_runs(settings):
    """Errored runs, runs left out of trends and runs with no median never move the reference."""
    _seed()
    _obs("usa_ebay", [250, 250])
    _obs("usa_ebay", [10], status="error", start=2)
    _obs("usa_ebay", [5], excluded=True, start=3)
    _obs("usa_ebay", [None], start=4)
    with session_scope() as db:
        assert reference_median(db, QID, "usa_ebay") == 250


def test_category_rails_still_apply(settings):
    """A category floor lifts the dynamic minimum; a ceiling below it falls back to the fixed band."""
    _seed(floor=200.0)
    _obs("usa_ebay", [250, 250])
    assert _band("usa_ebay") == (200.0, 750.0)

    with session_scope() as db:
        db.get(Category, "gpu").minimum_value_floor = None
        db.get(Category, "gpu").maximum_value_ceiling = 150.0
    assert _band("usa_ebay") == (100.0, 150.0)  # fixed band, capped by the ceiling


def test_queued_job_carries_the_dynamic_band(settings, fake_redis):
    """Run now sends the worker the vendor's dynamic band, not the fixed one."""
    from orchestrator.scheduler import enqueue_for_schedule

    _seed()
    _obs("usa_ebay", [240, 260, 250])
    with session_scope() as db:
        db.add(Vendor(identifier="usa_ebay", name="eBay US", config=VENDOR_CONFIG))
        db.add(Schedule(id=1, vendor_id="usa_ebay", query_object_id=QID, cron="0 * * * *"))
    with session_scope() as db:
        enqueue_for_schedule(db, db.get(Schedule, 1), scheduled_by="test")
    _stream, entries = fake_redis.xread({settings.job_stream: "0"})[0]
    payload = json.loads(entries[0][1]["payload"])
    assert payload["query_object"]["MinimumValue"] == 175.0
    assert payload["query_object"]["MaximumValue"] == 750.0


def test_settings_form_and_api_toggle_the_option(client):
    """The Settings checkbox and the API both turn the option on and off; Settings shows each vendor's band."""
    _seed(dynamic=False)
    _obs("usa_ebay", [240, 260, 250])
    client.post(f"/queries/{QID}/update", data={"content": "RTX 3070", "dynamic_band": "on",
                                                 "minimum_value": "100", "maximum_value": "2000",
                                                 "category_id": "gpu"})
    with session_scope() as db:
        assert db.get(QueryObject, QID).dynamic_band is True
    html = client.get(f"/products/{QID}/settings").get_data(as_text=True)
    assert "Price band in use" in html and "$175" in html and "$750" in html
    assert "median $250 of the last" in html

    r = client.patch(f"/api/v1/query-objects/{QID}", json={"dynamic_band": False})
    assert r.status_code == 200 and r.get_json()["dynamic_band"] is False
