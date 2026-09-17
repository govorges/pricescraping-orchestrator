"""The vendored Job/ScrapeResult schema must accept the worker's real payloads."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestrator.schemas import Job, ScrapeResultIn

FIXTURES = Path(__file__).parent / "fixtures"
SIBLING_WORKER = Path(__file__).parents[2] / "pricescraping-worker"


def test_job_example_validates():
    """The vendored Job schema parses a real job payload shape without error."""
    payload = json.loads((FIXTURES / "job.example.json").read_text(encoding="utf-8"))
    job = Job.model_validate(payload)
    assert job.vendor.identifier == "usa_ebay"
    assert job.query_object.id == "gpu-rtx-5090"


def test_scrape_result_example_validates():
    """The inbound ScrapeResult schema parses a real worker result payload shape without error."""
    payload = json.loads((FIXTURES / "scrape_result.example.json").read_text(encoding="utf-8"))
    result = ScrapeResultIn.model_validate(payload)
    assert result.metrics.listings_kept == 30
    assert result.status == "ok"


@pytest.mark.skipif(
    not (SIBLING_WORKER / "config" / "job.example.json").exists(),
    reason="sibling pricescraping-worker checkout not present",
)
def test_job_example_matches_sibling_worker():
    """Our copy of the job example fixture hasn't drifted from the worker repo's own copy."""
    ours = json.loads((FIXTURES / "job.example.json").read_text(encoding="utf-8"))
    theirs = json.loads(
        (SIBLING_WORKER / "config" / "job.example.json").read_text(encoding="utf-8")
    )
    assert ours == theirs, "tests/fixtures/job.example.json drifted from the worker repo"
