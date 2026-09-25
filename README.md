# pricescraping-orchestrator

The **Orchestrator + Panel** tier of the retail price-scraping platform. It is the
counterpart to [`pricescraping-worker`](../pricescraping-worker): it schedules
scrape jobs onto the queue, provisions and scales worker nodes, ingests the
results workers post back, stores the daily price time-series, and serves a
dashboard to watch the whole thing.

```
┌───────────────────────────── orchestrator (this repo) ─────────────────────────────┐
│  Flask app          Scheduler (APScheduler)         Provisioner                     │
│  ├─ dashboard UI    ├─ cron → build_job → XADD      ├─ LocalProvider  (Docker SDK)  │
│  ├─ CRUD API        └─ reconcile: nodes / DLQ /     └─ SaladProvider  (REST API)    │
│  └─ ingest API         autoscale                                                    │
└───────┬───────────────────────┬───────────────────────────────┬────────────────────┘
        │ XADD scrape.jobs      │ provision / scale             │ POST results
        ▼                       ▼                               ▲
┌───────────────┐      ┌──────────────────┐          ┌──────────┴───────────┐
│ Redis Stream  │────▶ │ Worker (N nodes) │ ───────▶ │ /api/v1/ingest/...   │
│  scrape.jobs  │      │  Playwright      │          │ → PriceObservation   │
└───────────────┘      └──────────────────┘          │   + Listing + Job    │
                                                     └──────────────────────┘
```

**In scope here:** result ingest + persistence, vendor / query-object / schedule
management, the cron scheduler, node provisioning (SaladCloud + local Docker),
queue-driven autoscaling, and the dashboard.

**Out of scope (other repos):** the browser automation and selector engine
(`pricescraping-worker`), the auth server issuing worker tokens, proxy support.

---

## Quickstart (local, full loop, no browser)

```bash
python -m venv .venv && . .venv/Scripts/activate      # or .venv/bin/activate
pip install -e ".[dev]"

# 1. a Redis for the job queue
docker run -d --rm -p 6379:6379 --name pso-redis redis:7-alpine

# 2. schema + example vendors / query objects / a schedule
alembic upgrade head
python scripts/seed_db.py

# 3. the orchestrator (Flask + in-process scheduler)
python -m orchestrator --scheduler          # http://localhost:8000

# 4. a fake worker: consumes scrape.jobs, POSTs a synthetic result back
python scripts/mock_worker.py
```

Open <http://localhost:8000>, go to **Schedules**, hit **Run now** — the job shows
up on the dashboard as `queued`, then flips to `ok` once `mock_worker` posts its
result, and the price chart on the dashboard fills in.

Fastest possible start (no Alembic):

```bash
python -m orchestrator --create-all --scheduler
```

### Full stack with a real worker

`docker compose up -d --build` brings up `redis`, the `orchestrator` (migrations +
seed + scheduler), and one `worker` built from `../pricescraping-worker`. The
orchestrator can start/scale more workers itself via the mounted Docker socket
(`PROVIDER=local`).

### Tests & lint

```bash
pytest          # offline: fakeredis + SQLite, no Docker / no network
ruff check .
```

---

## Contracts (shared with `pricescraping-worker`)

### Outbound — job message  (orchestrator → queue → worker)

`XADD` to the Redis stream `JOB_STREAM` (default `scrape.jobs`), one field
`payload` holding the job JSON. The scheduler resolves a `Vendor` + `QueryObject`
row into **embedded snapshots** so the worker stays stateless. Shape is exactly
[`tests/fixtures/job.example.json`](tests/fixtures/job.example.json) (a copy of the
worker repo's `config/job.example.json`; `tests/test_contract.py` guards against
drift). Built by [`orchestrator/jobs.py`](orchestrator/jobs.py).

### Inbound — result  (worker → orchestrator)

```
POST /api/v1/ingest/scrape-results
Authorization: Bearer {INGEST_TOKEN}       # must equal the worker's ORCHESTRATOR_TOKEN
Idempotency-Key: {job_id}
```

Body is the worker's `ScrapeResult.to_payload()`. Response `202 {"accepted": true}`.
The handler ([`orchestrator/api/ingest.py`](orchestrator/api/ingest.py)) upserts
one `PriceObservation` keyed by `job_id`, replaces the job's `Listing` rows, and
updates the `Job` row — idempotent on replay. `401` without a valid token, `422`
on a missing/invalid field.

---

## Data model  ([`orchestrator/models.py`](orchestrator/models.py))

| table | purpose |
|---|---|
| `vendor` | panel-managed frogscraper vendor config (`config` JSON) |
| `category` | scoped extra-exclude phrases + price guard rails shared by a group of query objects |
| `query_object` | panel-managed Include/Exclude filter spec, optionally tagged with a `category` |
| `schedule` | `vendor` × `query_object` × cron × options |
| `job` | one enqueued job: `queued` → `ok`/`partial`/`error`/`dead`/`canceled` |
| `price_observation` | daily price bucket — mirrors the worker README's `price_observation`; promoted to a TimescaleDB hypertable on PostgreSQL |
| `listing` | per-job listing detail, tagged `listing_type` (`auction`/`buy_now`/`unknown`) and linked to its `tracked_listing` |
| `tracked_listing` | one real-world item, identified across scrapes by its canonicalised URL |
| `listing_price_point` | one price-over-time sample for a `tracked_listing` (one per job that reported it) |
| `node` | a provisioned unit of worker capacity (local containers or a Salad container group) |

`DATABASE_URL` drives everything: `sqlite:///./_local/orchestrator.db` locally,
`postgresql+psycopg://…` (TimescaleDB) in production. Migrations live in
[`migrations/`](migrations/) (`alembic upgrade head`).

---

## Categories  ([`orchestrator/models.py`](orchestrator/models.py):`Category`)

The scoped analogue of frogscraper's single global filtered-phrases list. A
`Category` (panel: **Categories**) holds extra `Exclude` phrases plus optional
`minimum_value_floor` / `maximum_value_ceiling`. Every `QueryObject` tagged with
that category gets them merged in at job-build time
(`QueryObject.effective_exclude()` / `.effective_band()`, used by
`QueryObject.snapshot()`): the category's phrases are OR'd onto the query's own
`Exclude`, and its floor/ceiling can only **narrow** the query's own band, never
widen it. The merge happens before the job payload is built, so the worker sees
one plain, already-merged `Exclude` list — no worker-side changes needed.

## Auction vs. Buy-It-Now  ([`orchestrator/classify.py`](orchestrator/classify.py))

`classify_listing_type(title, extra)` is a vendor-agnostic heuristic: it scans a
listing's title plus every `extra` field (e.g. the `subtitle` selector in the
example eBay config, which carries text like `"3 bids"` or `"Buy It Now"`) for
generic marketplace phrasing, and returns `"auction"` / `"buy_now"` /
`"unknown"`. No worker or vendor-config change is required to benefit from it —
any vendor whose selectors already surface descriptive listing text gets
classified for free. A vendor can instead be explicit by mapping a selector to
`listing_format` / `sale_type` / `auction_type` with a literal value, which
always wins over the heuristic. Applied at ingest time; the result is stored per
listing (`listing.listing_type`) and shown/filterable on the Listings and
Tracked-listings pages.

## Listing tracking  ([`orchestrator/urlnorm.py`](orchestrator/urlnorm.py), `orchestrator/api/ingest.py`)

Every incoming listing's link is canonicalised — query string and fragment
stripped, scheme/host lower-cased, trailing slash dropped
(`canonical_url()`) — because marketplace URLs carry session/tracking params
(`?epid=…&hash=…&_trksid=…`) that change on every visit even though the item is
the same. Ingest looks up a `TrackedListing` by `(vendor_id, canonical_url)`,
creating it on first sight and updating `last_price` / `last_seen_at` /
`listing_type` on every later sighting, plus one `ListingPricePoint` per job
that reported it (deduped so an ingest replay never double-counts). A listing
with no link is stored on its job as usual but isn't tracked over time.
**Tracked listings** page (`/tracked`) lists every tracked item with its
first→last price and % change; each links to a detail page
(`/tracked/<id>`) with a uPlot price-over-time chart and full observation
history.

---

## Scheduler  ([`orchestrator/scheduler.py`](orchestrator/scheduler.py))

Started in-process with `--scheduler` (**run exactly one** such process).

* **cron tick** — one APScheduler `CronTrigger` per enabled `Schedule`; on fire it
  calls `enqueue_for_schedule` → `build_job` → `XADD` → insert a `queued` `Job`.
  "Run now" in the panel calls the same function.
* **reconcile loop** — every `RECONCILE_INTERVAL_S`: refresh `node` rows from the
  provisioner, sweep the dead-letter stream (mark those `Job`s `dead`), and — when
  `AUTOSCALE_ENABLED` — resize the first node to
  `ceil((queue_depth + pending) / AUTOSCALE_PER_REPLICA_TARGET)` clamped to
  `[AUTOSCALE_MIN_REPLICAS, AUTOSCALE_MAX_REPLICAS]`.

Schedule CRUD re-syncs the live scheduler automatically.

---

## Node provisioning  ([`orchestrator/provisioning/`](orchestrator/provisioning/))

`Provisioner` interface (`create_node` / `list_nodes` / `get_node` / `scale` /
`delete_node`), selected by `PROVIDER`:

* **`local`** — `LocalProvider`, Docker SDK. Runs `WORKER_IMAGE` containers
  labelled `orchestrator.node_id=<id>`; scaling a node = making that many
  containers exist. Needs a reachable Docker daemon; falls back to the default
  bridge network if `WORKER_NETWORK` doesn't exist.
* **`salad`** — `SaladProvider`, SaladCloud REST API
  (`https://api.salad.com/api/public`, `Salad-Api-Key` header). A node is a
  container group under `SALAD_ORG` / `SALAD_PROJECT`; `scale` PATCHes `replicas`.
  Request/response mapping is isolated in `_spec_to_body` / `_to_nodeinfo`.

Every provisioned worker inherits `Settings.worker_base_env()` — `REDIS_URL`,
`ORCHESTRATOR_BASE_URL`, `ORCHESTRATOR_TOKEN`, stream names — so it joins the same
`workers` consumer group and reports back here.

---

## HTTP surface

**Dashboard (HTML, HTMX-polled):** `/` · `/products` · `/products/<id>` (Overview)
· `/products/<id>/listings` · `/products/<id>/settings` · `/tracked` · `/tracked/<id>` ·
`/cost-per-frame` · `/vendors` · `/categories` · `/schedules` · `/jobs` · `/jobs/<id>` ·
`/nodes` · `/database`. The old `/queries`, `/queries/<id>/listings`, `/insights` and
`/insights/<id>` pages permanently redirect to their `/products` equivalents.

**JSON API (`/api/v1`):**

| method + path | |
|---|---|
| `POST /ingest/scrape-results` | worker result sink |
| `GET/POST /vendors`, `GET/PATCH/DELETE /vendors/<id>` | vendor CRUD |
| `GET/POST /categories`, `GET/PATCH/DELETE /categories/<id>` | category CRUD |
| `GET/POST /query-objects`, `GET/PATCH/DELETE /query-objects/<id>` | query-object CRUD (`category_id`, `effective_exclude`, `effective_*_value`) |
| `GET/POST /schedules`, `GET/PATCH/DELETE /schedules/<id>`, `POST /schedules/<id>/run` | schedule CRUD + trigger |
| `GET /jobs`, `GET /jobs/<id>`, `GET /observations` | job history + price series |
| `POST /jobs/<id>/cancel`, `POST /queue/purge` | pull job(s) off the stream |
| `GET /listings`, `POST /listings/<id>/hide`\|`/unhide`, `DELETE /listings/<id>` | per-job listing curation |
| `GET /tracked-listings`, `GET /tracked-listings/<id>` | tracked-listing list + price history |
| `GET /nodes`, `POST /nodes`, `POST /nodes/<id>/scale`, `DELETE /nodes/<id>` | node lifecycle |
| `GET /stats`, `GET /deadletter`, `GET /cron/preview` | dashboard summary + cron helper |

---

## Configuration (env / `.env`)

See [`.env.example`](.env.example). Key ones:

| var | default | note |
|---|---|---|
| `DATABASE_URL` | `sqlite:///./_local/orchestrator.db` | SQLite local, Postgres/Timescale prod |
| `REDIS_URL` / `JOB_STREAM` / `CONSUMER_GROUP` | `redis://localhost:6379/0` / `scrape.jobs` / `workers` | must match the worker |
| `DEADLETTER_STREAM` | `scrape.jobs.deadletter` | swept by the reconcile loop |
| `INGEST_TOKEN` / `INGEST_PATH` | `dev-token` / `/api/v1/ingest/scrape-results` | must match the worker's `ORCHESTRATOR_TOKEN` / `INGEST_PATH` |
| `SCHEDULER_ENABLED` / `RECONCILE_INTERVAL_S` | `true` / `30` | scheduler + reconcile cadence |
| `AUTOSCALE_ENABLED` … `AUTOSCALE_MAX_REPLICAS` | `false` / `10` / `0` / `10` | queue-driven autoscale |
| `PROVIDER` | `local` | `local` \| `salad` |
| `WORKER_IMAGE` / `WORKER_NETWORK` | `pricescraping-worker:latest` / `pricescraping_default` | local provider |
| `WORKER_ENV_REDIS_URL` / `WORKER_ENV_ORCHESTRATOR_BASE_URL` | — | base env handed to every worker node |
| `SALAD_BASE_URL` / `SALAD_API_KEY` / `SALAD_ORG` / `SALAD_PROJECT` / `SALAD_CONTAINER_GROUP_PREFIX` | — | SaladCloud provider |
| `HOST` / `PORT` / `LOG_LEVEL` | `0.0.0.0` / `8000` / `INFO` | |

---

## Layout

```
orchestrator/
  __init__.py        app factory        settings.py  db.py  logging.py
  schemas.py         worker wire contract (Job / ScrapeResultIn)
  queue.py           XADD + stream/DLQ observability + delete_message / purge_queue
  jobs.py            build_job(): Vendor + QueryObject -> job payload snapshot
  scheduler.py       APScheduler cron ticks + reconcile / autoscale loop
  autoscale.py       desired_replicas() (pure)
  classify.py        classify_listing_type(): auction / buy_now / unknown (pure)
  urlnorm.py         canonical_url(): strip session/tracking query args (pure)
  timeutil.py        iso_utc(): stamp UTC back on a SQLite-naive datetime
  provisioning/      base (interface) · local (Docker SDK) · salad (REST)
  api/               ingest · vendors · categories · queries · schedules · jobs ·
                     listings · tracked · nodes · stats
  ui/                views.py + Jinja templates + vendored htmx / uPlot
migrations/          Alembic (0001_initial · 0002_listing_hidden · 0003_categories_and_tracking)
config/              seed.example.json
scripts/             seed_db.py · mock_worker.py
tests/               offline unit + contract tests
```
