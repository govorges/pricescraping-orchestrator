from __future__ import annotations

CATEGORY = {
    "id": "gpu",
    "name": "GPUs",
    "extra_exclude": ["mining rig", "water damage"],
    "minimum_value_floor": 200,
    "maximum_value_ceiling": 3000,
}

VENDOR = {
    "identifier": "usa_ebay",
    "config": {
        "url": {"start": "https://x/?q="},
        "selectors": {"listings": "li", "price": {"css_selector": ".p", "type": "price"}},
    },
}


def test_category_crud_roundtrip(client):
    """Create, duplicate-reject, patch, and delete a category via the API."""
    r = client.post("/api/v1/categories", json=CATEGORY)
    assert r.status_code == 201
    assert r.get_json()["query_count"] == 0

    assert client.post("/api/v1/categories", json=CATEGORY).status_code == 409

    r = client.patch("/api/v1/categories/gpu", json={"name": "Graphics Cards"})
    assert r.get_json()["name"] == "Graphics Cards"

    assert client.delete("/api/v1/categories/gpu").status_code == 204
    assert client.get("/api/v1/categories/gpu").status_code == 404


def test_query_rejects_unknown_category(client):
    """Assigning a query object to a category_id that doesn't exist is a validation error, not a dangling FK."""
    r = client.post(
        "/api/v1/query-objects",
        json={"id": "q1", "content": "RTX 5090", "category_id": "does-not-exist"},
    )
    assert r.status_code == 422


def test_query_object_exposes_effective_fields(client):
    """The API response and job snapshot both surface the category-merged exclude list and price band, not just the query's own raw fields."""
    client.post("/api/v1/categories", json=CATEGORY)
    r = client.post(
        "/api/v1/query-objects",
        json={
            "id": "q1", "content": "RTX 5090", "exclude": ["bracket"],
            "minimum_value": 50, "maximum_value": 5000, "category_id": "gpu",
        },
    )
    assert r.status_code == 201
    body = r.get_json()
    assert set(body["effective_exclude"]) == {"bracket", "mining rig", "water damage"}
    assert body["effective_minimum_value"] == 200
    assert body["effective_maximum_value"] == 3000
    assert set(body["snapshot"]["Exclude"]) == {"bracket", "mining rig", "water damage"}
    assert body["snapshot"]["MinimumValue"] == 200
    assert body["snapshot"]["MaximumValue"] == 3000


def test_schedule_run_now_embeds_category_merge_in_job_payload(client, fake_redis, settings):
    """The job payload actually enqueued to Redis carries the category-merged Exclude/MinimumValue, end to end."""
    client.post("/api/v1/vendors", json=VENDOR)
    client.post("/api/v1/categories", json=CATEGORY)
    client.post(
        "/api/v1/query-objects",
        json={"id": "q1", "content": "RTX 5090", "exclude": ["bracket"], "category_id": "gpu"},
    )
    sid = client.post(
        "/api/v1/schedules",
        json={"vendor_id": "usa_ebay", "query_object_id": "q1", "cron": "0 * * * *"},
    ).get_json()["id"]
    client.post(f"/api/v1/schedules/{sid}/run")

    import json as _json

    _stream, entries = fake_redis.xread({settings.job_stream: "0"})[0]
    _mid, fields = entries[0]
    payload = _json.loads(fields["payload"])
    assert set(payload["query_object"]["Exclude"]) == {"bracket", "mining rig", "water damage"}
    assert payload["query_object"]["MinimumValue"] == 200
