"""Retroactively apply exclude phrases to listings already sitting in the DB.

Adding a phrase to a category or query object's Exclude list only affects
*future* scrapes -- the worker is what actually filters listings, and it only
sees a query at job-assembly time. This mirrors the exclude half of
``pricescraping-worker/worker/query.py`` (``QueryFilter._is_string_valid``) so
the same phrase can be swept back over listings that were kept before the
phrase existed. Matching is title-only and exclude-only: no Include / price
re-validation, since that's not what "apply this exclude phrase" means to an
operator retroactively -- it means "hide anything with this phrase in it".
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestrator.models import Category, QueryObject, TrackedListing
from orchestrator.prices import recompute_for_tracked


def excluded_phrases_matched(
    text: str, exclude: list[str], *, case_sensitive: bool = False, operator: str = "or"
) -> list[str]:
    """Which (if any) of ``exclude`` appear in ``text``, per ``operator``.

    Returns the matched phrases when the listing should be excluded, or an
    empty list when it passes -- so ``bool(result)`` is "should be hidden".
    """
    if not exclude or not text:
        return []
    haystack = text if case_sensitive else text.lower()
    needles = exclude if case_sensitive else [p.lower() for p in exclude]
    found = [orig for orig, needle in zip(exclude, needles, strict=True) if needle in haystack]

    op = (operator or "or").lower()
    if op == "and":
        hit = len(found) == len(exclude) and len(exclude) > 0
    elif op == "xor":
        hit = len(found) == 1
    else:  # "or" (and any unrecognised operator falls back to "or")
        hit = len(found) > 0
    return found if hit else []


def _exclude_settings(q: QueryObject) -> dict:
    return q.exclude_settings or (q.category.exclude_settings if q.category else None) or {}


def apply_excludes_to_query(db: Session, q: QueryObject) -> int:
    """Hide every non-hidden tracked listing for ``q`` whose title now matches
    its effective exclude list. Returns how many were newly hidden."""
    exclude = q.effective_exclude()
    settings = _exclude_settings(q)
    case_sensitive = bool(settings.get("case_sensitive", False))
    operator = settings.get("operator", "or")

    rows = db.scalars(
        select(TrackedListing).where(TrackedListing.query_object_id == q.id, ~TrackedListing.hidden)
    ).all()
    newly_hidden = []
    for t in rows:
        if excluded_phrases_matched(t.title or "", exclude, case_sensitive=case_sensitive, operator=operator):
            t.hidden = True
            newly_hidden.append(t.id)
    # the runs they were in stop counting them towards their prices
    recompute_for_tracked(db, newly_hidden)
    return len(newly_hidden)


def apply_excludes_to_category(db: Session, category: Category) -> int:
    """Apply :func:`apply_excludes_to_query` across every query in the category."""
    return sum(apply_excludes_to_query(db, q) for q in category.queries)
