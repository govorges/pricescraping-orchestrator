from __future__ import annotations

from orchestrator.jobs import build_job
from orchestrator.models import QueryObject, Vendor
from orchestrator.schemas import Job

VENDOR_CONFIG = {
    "metadata": {"name": "eBay US"},
    "url": {"start": "https://www.ebay.com/sch/i.html?_nkw=", "end": ""},
    "selectors": {
        "listings": "ul.srp-results li.s-card",
        "title": {"css_selector": ".t", "type": "text", "required": True},
        "price": {"css_selector": ".p", "type": "price", "required": True},
    },
}


def _vendor():
    return Vendor(identifier="usa_ebay", name="eBay US", config=VENDOR_CONFIG)


def _query():
    return QueryObject(
        id="gpu-rtx-5090",
        content="RTX 5090",
        include=[],
        exclude=["bracket", "box only"],
        minimum_value=500.0,
        maximum_value=5000.0,
    )


def test_build_job_shape_and_validation():
    """A built job payload has exactly the expected fields, embeds the vendor/query snapshots, and validates against the worker's own Job schema."""
    payload = build_job(_vendor(), _query(), options={"headless": True}, scheduled_by="test")
    assert set(payload) == {
        "job_id",
        "created_at",
        "scheduled_by",
        "attempt",
        "vendor",
        "query_object",
        "options",
    }
    assert payload["vendor"]["identifier"] == "usa_ebay"
    assert payload["query_object"]["Content"] == "RTX 5090"
    assert payload["query_object"]["MaximumValue"] == 5000.0
    assert payload["options"] == {"headless": True}
    assert payload["created_at"].endswith("Z")

    job = Job.model_validate(payload)
    assert job.vendor.selectors["listings"] == "ul.srp-results li.s-card"


def test_build_job_drops_unknown_options():
    """An options key the worker doesn't recognise is silently dropped rather than breaking job assembly."""
    payload = build_job(_vendor(), _query(), options={"nope": 1, "max_listings": 50})
    assert payload["options"] == {"max_listings": 50}


def test_build_job_unique_ids():
    """Every call to build_job mints a fresh job_id, even for the identical vendor/query pair."""
    a = build_job(_vendor(), _query())
    b = build_job(_vendor(), _query())
    assert a["job_id"] != b["job_id"]
