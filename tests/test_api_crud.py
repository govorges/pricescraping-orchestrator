from __future__ import annotations

VENDOR = {
    "identifier": "usa_ebay",
    "name": "eBay US",
    "config": {
        "metadata": {"name": "eBay US"},
        "url": {"start": "https://www.ebay.com/sch/i.html?_nkw="},
        "selectors": {
            "listings": "li.s-card",
            "price": {"css_selector": ".p", "type": "price", "required": True},
        },
    },
}

QUERY = {
    "id": "gpu-rtx-5090",
    "content": "RTX 5090",
    "exclude": ["bracket"],
    "minimum_value": 500,
    "maximum_value": 5000,
}


def test_vendor_crud_roundtrip(client):
    """Create, duplicate-reject, patch, list, and delete a vendor via the API."""
    assert client.get("/api/v1/vendors").get_json() == []

    r = client.post("/api/v1/vendors", json=VENDOR)
    assert r.status_code == 201

    assert client.post("/api/v1/vendors", json=VENDOR).status_code == 409

    r = client.patch("/api/v1/vendors/usa_ebay", json={"enabled": False})
    assert r.get_json()["enabled"] is False

    assert len(client.get("/api/v1/vendors").get_json()) == 1
    assert client.delete("/api/v1/vendors/usa_ebay").status_code == 204
    assert client.get("/api/v1/vendors/usa_ebay").status_code == 404


def test_vendor_rejects_bad_config(client):
    """A vendor config missing the required url/selectors fields is rejected, not silently accepted."""
    bad = {"identifier": "x", "config": {"selectors": {}}}  # no url, no listings
    assert client.post("/api/v1/vendors", json=bad).status_code == 422


def test_query_crud_roundtrip(client):
    """Creating a query object returns its resolved frogscraper-shaped snapshot; deleting it works."""
    r = client.post("/api/v1/query-objects", json=QUERY)
    assert r.status_code == 201
    body = r.get_json()
    assert body["snapshot"]["Content"] == "RTX 5090"
    assert body["snapshot"]["MaximumValue"] == 5000

    assert client.delete("/api/v1/query-objects/gpu-rtx-5090").status_code == 204


def test_schedule_crud_and_validation(client):
    """An invalid cron expression is rejected with 422; a valid schedule can be created, fetched, and deleted."""
    client.post("/api/v1/vendors", json=VENDOR)
    client.post("/api/v1/query-objects", json=QUERY)

    assert client.post(
        "/api/v1/schedules",
        json={"vendor_id": "usa_ebay", "query_object_id": "gpu-rtx-5090", "cron": "not a cron"},
    ).status_code == 422

    r = client.post(
        "/api/v1/schedules",
        json={
            "name": "hourly",
            "vendor_id": "usa_ebay",
            "query_object_id": "gpu-rtx-5090",
            "cron": "0 * * * *",
        },
    )
    assert r.status_code == 201
    sid = r.get_json()["id"]

    assert client.get(f"/api/v1/schedules/{sid}").status_code == 200
    assert client.delete(f"/api/v1/schedules/{sid}").status_code == 204


def test_schedule_run_now_enqueues(client, fake_redis, settings):
    """POSTing .../run assembles a job immediately, independent of the cron trigger, and records it as queued."""
    client.post("/api/v1/vendors", json=VENDOR)
    client.post("/api/v1/query-objects", json=QUERY)
    sid = client.post(
        "/api/v1/schedules",
        json={"vendor_id": "usa_ebay", "query_object_id": "gpu-rtx-5090", "cron": "0 * * * *"},
    ).get_json()["id"]

    r = client.post(f"/api/v1/schedules/{sid}/run")
    assert r.status_code == 202
    job_id = r.get_json()["job_id"]

    assert fake_redis.xlen(settings.job_stream) == 1
    jobs = client.get("/api/v1/jobs").get_json()
    assert jobs[0]["job_id"] == job_id
    assert jobs[0]["status"] == "queued"
