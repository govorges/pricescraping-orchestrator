"""Fake worker: consume scrape.jobs and POST a synthetic result to the orchestrator.

Exercises the whole orchestrator loop (schedule -> enqueue -> ingest -> store ->
dashboard) without Playwright or a browser. Mirrors the real worker's queue
semantics from ``pricescraping-worker/worker/consumer.py`` closely enough for
local testing.

Usage:
    python scripts/mock_worker.py            # runs until Ctrl-C
    python scripts/mock_worker.py --once     # handle a single job then exit
"""

from __future__ import annotations

import argparse
import json
import random
import socket
import sys
import time
from datetime import datetime, timezone

import httpx
import redis

from orchestrator.settings import get_settings


def _synth_result(job: dict) -> dict:
    n = random.randint(8, 40)
    prices = sorted(round(random.uniform(800, 3200), 2) for _ in range(n))
    kept = max(1, int(n * 0.7))
    kept_prices = prices[:kept]
    now = datetime.now(timezone.utc)
    return {
        "job_id": job["job_id"],
        "vendor_id": job["vendor"]["identifier"],
        "query_object_id": job["query_object"]["id"],
        "observed_at": now.isoformat().replace("+00:00", "Z"),
        "observed_date": now.date().isoformat(),
        "status": "ok",
        "error": None,
        "duration_ms": random.randint(1500, 6000),
        "worker": {"node": socket.gethostname(), "container": "mock-worker", "version": "mock"},
        "metrics": {
            "listings_found": n,
            "listings_kept": kept,
            "price_low": kept_prices[0],
            "price_mean": round(sum(kept_prices) / len(kept_prices), 2),
            "price_median": kept_prices[len(kept_prices) // 2],
        },
        "listings": [
            {
                "title": f"{job['query_object']['Content']} listing {i}",
                "link": f"https://example.test/itm/{job['job_id']}-{i}",
                "image": None,
                "price": p,
                "extra": {"shipping": 0.0},
            }
            for i, p in enumerate(kept_prices)
        ],
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--delay", type=float, default=1.0, help="seconds to fake-scrape")
    args = ap.parse_args()

    s = get_settings()
    r = redis.Redis.from_url(s.redis_url, decode_responses=True)
    group, consumer = s.consumer_group, f"mock-{socket.gethostname()}"
    try:
        r.xgroup_create(s.job_stream, group, id="0", mkstream=True)
    except redis.ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise

    ingest_url = f"{s.worker_env_orchestrator_base_url.rstrip('/')}{s.ingest_path}"
    # local run: talk to localhost, not the compose hostname
    ingest_url = ingest_url.replace("http://orchestrator:", "http://localhost:")
    client = httpx.Client(timeout=30.0, headers={"Authorization": f"Bearer {s.ingest_token}"})
    print(f"mock worker: {s.job_stream} -> {ingest_url}", file=sys.stderr)

    while True:
        resp = r.xreadgroup(group, consumer, {s.job_stream: ">"}, count=1, block=5000)
        if not resp:
            if args.once:
                print("no job available", file=sys.stderr)
                return
            continue
        _stream, entries = resp[0]
        for msg_id, fields in entries:
            job = json.loads(fields["payload"])
            print(f"handling job {job['job_id']} (msg {msg_id})", file=sys.stderr)
            time.sleep(args.delay)
            result = _synth_result(job)
            res = client.post(ingest_url, json=result, headers={"Idempotency-Key": job["job_id"]})
            print(f"  ingest -> HTTP {res.status_code}", file=sys.stderr)
            r.xack(s.job_stream, group, msg_id)
            r.xdel(s.job_stream, msg_id)
        if args.once:
            return


if __name__ == "__main__":
    main()
