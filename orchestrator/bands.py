"""Dynamic price bands: a query object's MinimumValue / MaximumValue worked
out from its recent median price at each vendor, instead of fixed numbers.

A fixed band goes stale as prices move, and one band can't suit every vendor
(eBay's used prices sit far below Newegg's new ones). With ``dynamic_band``
on, each job gets a band built from that vendor's own recent runs:

- minimum = 70% of the reference median -- anything cheaper is almost always
  not the product (a box, a bracket, a "for parts" card);
- maximum = 3x the reference median -- deliberately loose, so collector and
  premium editions still get through.

The reference median is the median of the vendor's last few clean runs'
medians, so a single odd run can't swing it; with too little history the
product's fixed band is used. Category guard rails still apply on top.
"""

from __future__ import annotations

import statistics

from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestrator.models import PriceObservation, QueryObject

MIN_RATIO = 0.70
MAX_RATIO = 3.0
REFERENCE_RUNS = 10
MIN_REFERENCE_RUNS = 2


def reference_median(db: Session, query_object_id: str, vendor_id: str) -> float | None:
    """Median of this vendor's last ``REFERENCE_RUNS`` clean run medians for the
    product, or None with fewer than ``MIN_REFERENCE_RUNS`` of them."""
    medians = db.scalars(
        select(PriceObservation.price_median)
        .where(
            PriceObservation.query_object_id == query_object_id,
            PriceObservation.vendor_id == vendor_id,
            PriceObservation.status != "error",
            PriceObservation.excluded.is_(False),
            PriceObservation.price_median.is_not(None),
        )
        .order_by(PriceObservation.observed_at.desc())
        .limit(REFERENCE_RUNS)
    ).all()
    if len(medians) < MIN_REFERENCE_RUNS:
        return None
    return statistics.median(medians)


def band_for(db: Session, q: QueryObject, vendor_id: str) -> tuple[float, float | None]:
    """The (minimum, maximum) a job for ``q`` at ``vendor_id`` should carry."""
    fixed = q.effective_band()
    if not q.dynamic_band:
        return fixed
    reference = reference_median(db, q.id, vendor_id)
    if reference is None:
        return fixed
    lo, hi = round(reference * MIN_RATIO, 2), round(reference * MAX_RATIO, 2)
    if q.category:
        if q.category.minimum_value_floor is not None:
            lo = max(lo, q.category.minimum_value_floor)
        if q.category.maximum_value_ceiling is not None:
            hi = min(hi, q.category.maximum_value_ceiling)
    if lo >= hi:
        return fixed  # guard rails leave no room; don't send a band nothing can pass
    return lo, hi


def bands_by_vendor(db: Session, q: QueryObject, vendor_ids: list[str]) -> list[dict]:
    """What each vendor's next job would carry, and where it came from, for the
    product's Settings tab."""
    out = []
    for vendor_id in vendor_ids:
        reference = reference_median(db, q.id, vendor_id) if q.dynamic_band else None
        lo, hi = band_for(db, q, vendor_id)
        out.append({"vendor_id": vendor_id, "lo": lo, "hi": hi, "reference": reference,
                    "dynamic": q.dynamic_band and reference is not None})
    return out
