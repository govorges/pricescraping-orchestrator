from __future__ import annotations

from orchestrator.db import session_scope
from orchestrator.models import Job

VENDOR = {
    "identifier": "usa_ebay",
    "config": {
        "url": {"start": "https://x/?q="},
        "selectors": {"listings": "li", "price": {"css_selector": ".p", "type": "price"}},
    },
}
QUERY = {"id": "q1", "content": "RTX 5090", "minimum_value": 500}


def _seed_schedule(client):
    client.post("/api/v1/vendors", json=VENDOR)
    client.post("/api/v1/query-objects", json=QUERY)
    return client.post(
        "/api/v1/schedules",
        json={"vendor_id": "usa_ebay", "query_object_id": "q1", "cron": "0 * * * *"},
    ).get_json()["id"]


def test_cancel_removes_queued_job_from_stream(client, fake_redis, settings):
    """Cancelling a still-queued job both marks it canceled and removes its message from the Redis stream."""
    sid = _seed_schedule(client)
    job_id = client.post(f"/api/v1/schedules/{sid}/run").get_json()["job_id"]
    assert fake_redis.xlen(settings.job_stream) == 1

    resp = client.post(f"/api/v1/jobs/{job_id}/cancel")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "canceled"
    assert body["removed_from_stream"] == 1
    assert fake_redis.xlen(settings.job_stream) == 0


def test_cancel_rejects_finished_job(client, fake_redis):
    """A job that's already finished (status 'ok') can't be cancelled -- that's a 409, not a no-op success."""
    sid = _seed_schedule(client)
    job_id = client.post(f"/api/v1/schedules/{sid}/run").get_json()["job_id"]
    with session_scope() as db:
        db.get(Job, job_id).status = "ok"
    assert client.post(f"/api/v1/jobs/{job_id}/cancel").status_code == 409


def test_purge_clears_queue_and_cancels_rows(client, fake_redis, settings):
    """Purging the queue empties the Redis stream and marks every still-queued Job row canceled."""
    sid = _seed_schedule(client)
    for _ in range(3):
        client.post(f"/api/v1/schedules/{sid}/run")
    assert fake_redis.xlen(settings.job_stream) == 3

    resp = client.post("/api/v1/queue/purge")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["removed_from_stream"] == 3
    assert body["jobs_canceled"] == 3
    assert fake_redis.xlen(settings.job_stream) == 0
    with session_scope() as db:
        assert all(j.status == "canceled" for j in db.query(Job).all())


def test_cron_preview(client):
    """A valid cron expression returns the next N run times; an invalid one reports invalid instead of erroring."""
    ok = client.get("/api/v1/cron/preview?expr=0 9 * * 1&n=3").get_json()
    assert ok["valid"] is True
    assert len(ok["next"]) == 3
    bad = client.get("/api/v1/cron/preview?expr=not-a-cron").get_json()
    assert bad == {"valid": False, "next": []}
