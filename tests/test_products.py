from __future__ import annotations

from datetime import datetime, timezone

from orchestrator.db import session_scope
from orchestrator.models import (
    Category,
    CategorySchedule,
    QueryObject,
    Schedule,
    TrackedListing,
    Vendor,
)

VENDOR_CONFIG = {
    "metadata": {"name": "eBay US"},
    "url": {"start": "https://www.ebay.com/sch/i.html?_nkw="},
    "selectors": {
        "listings": "li.s-card",
        "price": {"css_selector": ".p", "type": "price", "required": True},
    },
}


def _seed():
    with session_scope() as db:
        db.add(Vendor(identifier="usa_ebay", name="eBay US", config=VENDOR_CONFIG))
        db.add(Category(id="gpu", name="GPUs"))
        db.add(QueryObject(id="gpu-rtx-5090", content="RTX 5090", category_id="gpu"))
        db.add(QueryObject(id="cpu-9800x3d", content="9800X3D"))
        db.add(Schedule(id=1, vendor_id="usa_ebay", query_object_id="gpu-rtx-5090", cron="0 */6 * * *"))
        db.add(CategorySchedule(id=1, category_id="gpu", vendor_id=None, cron="0 4 * * 1"))


def test_products_list_shows_schedule_state(client):
    """Each product row links to its product page, and a product nothing schedules is called out."""
    _seed()
    html = client.get("/products").get_data(as_text=True)
    assert 'href="/products/gpu-rtx-5090"' in html
    # cpu-9800x3d has no schedule and no category schedule
    row = html[html.index('data-name="9800x3d cpu-9800x3d"'):]
    assert "not scheduled" in row[: row.index('class="rowact"')]


def test_products_list_preselects_category_filter(client):
    """?category_id= (used by the Categories page and the old /insights redirect) preselects that category chip."""
    _seed()
    html = client.get("/products?category_id=gpu").get_data(as_text=True)
    assert 'data-cat="gpu" aria-pressed="true"' in html
    assert 'data-cat="" aria-pressed="false"' in html


def test_product_tabs_render_and_mark_the_current_one(client):
    """Overview, Listings and Settings all render inside the product frame, each marking itself as the current tab."""
    _seed()
    for path, label in [("", "Overview"), ("/listings", "Listings"), ("/settings", "Settings")]:
        r = client.get(f"/products/gpu-rtx-5090{path}")
        assert r.status_code == 200, path
        html = r.get_data(as_text=True)
        assert f'href="/products/gpu-rtx-5090{path}" aria-current=page' in html
        assert 'aria-label="Breadcrumb"' in html


def test_listings_tab_shows_the_products_tracked_listings(client):
    """The Listings tab lists this product's tracked listings and none from other products."""
    _seed()
    now = datetime.now(timezone.utc)
    with session_scope() as db:
        for qid, title in [("gpu-rtx-5090", "ASUS RTX 5090 Astral"), ("cpu-9800x3d", "Ryzen 7 9800X3D tray")]:
            db.add(TrackedListing(vendor_id="usa_ebay", query_object_id=qid, canonical_url=f"https://x/{qid}",
                                  title=title, first_seen_at=now, last_seen_at=now, last_price=100.0))
    html = client.get("/products/gpu-rtx-5090/listings").get_data(as_text=True)
    assert "ASUS RTX 5090 Astral" in html
    assert "Ryzen 7 9800X3D tray" not in html


def test_settings_tab_lists_direct_and_category_schedules(client):
    """Settings shows the product's own schedules with a Run now that returns here, plus schedules it gets from its category."""
    _seed()
    html = client.get("/products/gpu-rtx-5090/settings").get_data(as_text=True)
    assert "0 */6 * * *" in html
    assert 'action="/schedules/1/run"' in html
    assert 'name="next" value="/products/gpu-rtx-5090/settings"' in html
    assert "0 4 * * 1" in html and "via GPUs" in html


def test_run_now_returns_to_the_page_it_was_pressed_on(client, fake_redis):
    """A local `next` path is honoured; anything pointing off-site falls back to the Schedules page."""
    _seed()
    r = client.post("/schedules/1/run", data={"next": "/products/gpu-rtx-5090/settings"})
    assert r.headers["Location"].endswith("/products/gpu-rtx-5090/settings")
    r = client.post("/schedules/1/run", data={"next": "//evil.example/x"})
    assert r.headers["Location"].endswith("/schedules")


def test_create_and_update_land_on_the_products_settings(client):
    """Creating a product goes straight to its Settings tab (to add a schedule); saving the form stays there."""
    _seed()
    r = client.post("/queries", data={"id": "gpu-rx-9070", "content": "RX 9070"})
    assert r.headers["Location"].endswith("/products/gpu-rx-9070/settings")
    r = client.post("/queries/gpu-rx-9070/update", data={"content": "RX 9070 XT"})
    assert r.headers["Location"].endswith("/products/gpu-rx-9070/settings")
    with session_scope() as db:
        assert db.get(QueryObject, "gpu-rx-9070").content == "RX 9070 XT"
    r = client.post("/queries/gpu-rx-9070/delete")
    assert r.headers["Location"].endswith("/products")


def test_old_query_pages_redirect(client):
    """/queries and /queries/<id>/listings permanently redirect to Products, keeping listing filters."""
    _seed()
    r = client.get("/queries")
    assert r.status_code == 301 and r.headers["Location"].endswith("/products")
    r = client.get("/queries/gpu-rtx-5090/listings?status=active&show=hidden")
    assert r.status_code == 301
    assert "/products/gpu-rtx-5090/listings?" in r.headers["Location"]
    assert "status=active" in r.headers["Location"] and "show=hidden" in r.headers["Location"]


def test_deleting_a_product_deletes_its_schedules(client):
    """The product's own schedules go with it (SQLite ignores the FK cascade), while category schedules stay."""
    _seed()
    r = client.post("/queries/gpu-rtx-5090/delete")
    assert r.headers["Location"].endswith("/products")
    with session_scope() as db:
        assert db.get(QueryObject, "gpu-rtx-5090") is None
        assert db.query(Schedule).count() == 0
        assert db.query(CategorySchedule).count() == 1


def test_api_delete_also_deletes_the_products_schedules(client):
    """DELETE /api/v1/query-objects/<id> removes its schedules the same way the dashboard does."""
    _seed()
    assert client.delete("/api/v1/query-objects/gpu-rtx-5090").status_code == 204
    with session_scope() as db:
        assert db.query(Schedule).filter_by(query_object_id="gpu-rtx-5090").count() == 0
