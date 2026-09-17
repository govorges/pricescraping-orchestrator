"""Wire contract shared with ``pricescraping-worker``.

These models are a deliberate copy of ``pricescraping-worker/worker/models.py``
(inbound ``Job``) plus an inbound view of that repo's ``ScrapeResult.to_payload()``
output. The orchestrator uses them to:

* validate a job payload before it is ``XADD``-ed to the stream (fail fast, exactly
  like ``pricescraping-worker/scripts/seed_job.py``);
* parse and validate a result POSTed to the ingest endpoint.

``tests/test_contract.py`` round-trips ``pricescraping-worker/config/job.example.json``
through :class:`Job` so drift is caught early.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field

SelectorType = Literal["text", "link", "image", "price", "additive_price", "misc_price"]


class SelectorDef(BaseModel):
    css_selector: str
    type: SelectorType
    required: bool = False
    ignore_filter: bool = False


class VendorUrl(BaseModel):
    start: str
    end: str = ""


class VendorConfig(BaseModel):
    identifier: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    url: VendorUrl
    preload: str | None = None
    strip_phrases: list[str] = Field(default_factory=list)
    challenge_markers: list[str] = Field(default_factory=list)
    selectors: dict[str, Any]


class FilterSettings(BaseModel):
    operator: Literal["and", "or", "xor"] = "or"
    case_sensitive: bool = False
    require_content: bool = True


class QueryObject(BaseModel):
    id: str
    Content: str
    Include: list[str] = Field(default_factory=list)
    IncludeSettings: FilterSettings = Field(default_factory=lambda: FilterSettings(operator="and"))
    Exclude: list[str] = Field(default_factory=list)
    ExcludeSettings: FilterSettings = Field(default_factory=lambda: FilterSettings(operator="or"))
    MinimumValue: float = 0.0
    MaximumValue: float | None = None


class ScrapeOptions(BaseModel):
    engine: Literal["firefox", "chromium", "webkit"] | None = None
    headless: bool | None = None
    nav_timeout_ms: int | None = None
    request_delay_ms: int | None = None
    max_listings: int | None = None
    challenge_retries: int | None = None


class Job(BaseModel):
    job_id: str
    created_at: datetime | None = None
    scheduled_by: str | None = None
    attempt: int = 1
    vendor: VendorConfig
    query_object: QueryObject
    options: ScrapeOptions = Field(default_factory=ScrapeOptions)


# ---- inbound result (worker -> orchestrator) --------------------------------


class ListingResultIn(BaseModel):
    title: str | None = None
    link: str | None = None
    image: str | None = None
    price: float
    extra: dict[str, Any] = Field(default_factory=dict)


class DroppedListingIn(BaseModel):
    title: str | None = None
    reason: str


class ResultMetricsIn(BaseModel):
    listings_found: int = 0
    listings_kept: int = 0
    price_low: float | None = None
    price_mean: float | None = None
    price_median: float | None = None


class ResultErrorIn(BaseModel):
    type: str
    message: str


class ScrapeResultIn(BaseModel):
    job_id: str
    vendor_id: str
    query_object_id: str
    observed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    observed_date: date = Field(default_factory=lambda: datetime.now(timezone.utc).date())
    status: Literal["ok", "partial", "error"] = "ok"
    error: ResultErrorIn | None = None
    duration_ms: int = 0
    worker: dict[str, Any] = Field(default_factory=dict)
    metrics: ResultMetricsIn = Field(default_factory=ResultMetricsIn)
    listings: list[ListingResultIn] = Field(default_factory=list)
    dropped: list[DroppedListingIn] = Field(default_factory=list)
