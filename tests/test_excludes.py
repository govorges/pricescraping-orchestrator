from __future__ import annotations

from datetime import datetime, timezone

from orchestrator.excludes import (
    apply_excludes_to_category,
    apply_excludes_to_query,
    excluded_phrases_matched,
)
from orchestrator.models import Category, QueryObject, TrackedListing

VENDOR = "usa_ebay"


def test_or_operator_matches_any_phrase():
    """'or' (the default) excludes a listing if ANY one phrase is found."""
    assert excluded_phrases_matched("For Parts Only", ["for parts", "broken"], operator="or")
    assert not excluded_phrases_matched("Works great", ["for parts", "broken"], operator="or")


def test_and_operator_requires_all_phrases():
    """'and' only excludes when every phrase in the list is present."""
    assert not excluded_phrases_matched("for parts", ["for parts", "broken"], operator="and")
    assert excluded_phrases_matched("for parts, broken", ["for parts", "broken"], operator="and")


def test_xor_operator_requires_exactly_one():
    """'xor' excludes only when exactly one phrase matches -- two or more matching is not exclusion."""
    assert excluded_phrases_matched("for parts", ["for parts", "broken"], operator="xor")
    assert not excluded_phrases_matched("for parts, broken", ["for parts", "broken"], operator="xor")


def test_case_sensitivity():
    """case_sensitive=True requires an exact-case match; the default is case-insensitive."""
    assert not excluded_phrases_matched("FOR PARTS", ["for parts"], case_sensitive=True)
    assert excluded_phrases_matched("FOR PARTS", ["for parts"], case_sensitive=False)


def test_empty_exclude_or_text_never_matches():
    """No exclude phrases, or no text to check against, both mean nothing gets excluded."""
    assert excluded_phrases_matched("anything", []) == []
    assert excluded_phrases_matched("", ["for parts"]) == []


def _tracked(db, *, qid, url, title, hidden=False):
    t = TrackedListing(
        vendor_id=VENDOR, query_object_id=qid, canonical_url=url, title=title,
        first_seen_at=datetime.now(timezone.utc), last_seen_at=datetime.now(timezone.utc),
        hidden=hidden,
    )
    db.add(t)
    return t


def test_apply_excludes_to_query_hides_matching_titles(db_session):
    """Retroactively applying a query's excludes hides only the tracked listings whose title matches."""
    q = QueryObject(id="gpu-rtx-5090", content="RTX 5090", exclude=["waterblock"])
    db_session.add(q)
    match = _tracked(db_session, qid="gpu-rtx-5090", url="https://x/1", title="RTX 5090 with waterblock")
    keep = _tracked(db_session, qid="gpu-rtx-5090", url="https://x/2", title="RTX 5090 stock cooler")
    db_session.flush()

    n = apply_excludes_to_query(db_session, q)

    assert n == 1
    assert match.hidden is True
    assert keep.hidden is False


def test_apply_excludes_to_query_skips_already_hidden(db_session):
    """A listing that's already hidden isn't re-counted in the number of newly-hidden listings."""
    q = QueryObject(id="gpu-rtx-5090", content="RTX 5090", exclude=["waterblock"])
    db_session.add(q)
    _tracked(db_session, qid="gpu-rtx-5090", url="https://x/1", title="RTX 5090 with waterblock", hidden=True)
    db_session.flush()

    n = apply_excludes_to_query(db_session, q)

    assert n == 0  # already hidden, not double-counted


def test_apply_excludes_to_query_uses_category_extra_exclude(db_session):
    """Applying excludes for a query also sweeps against phrases inherited from its category, not just its own."""
    cat = Category(id="gpu", name="GPUs", extra_exclude=["mining rig"])
    db_session.add(cat)
    q = QueryObject(id="gpu-rtx-5090", content="RTX 5090", category_id="gpu", exclude=[])
    db_session.add(q)
    _tracked(db_session, qid="gpu-rtx-5090", url="https://x/1", title="RTX 5090 mining rig bundle")
    db_session.flush()

    n = apply_excludes_to_query(db_session, q)

    assert n == 1


def test_apply_excludes_to_category_covers_every_query(db_session):
    """Applying excludes at the category level sweeps every query object it contains, not just one."""
    cat = Category(id="gpu", name="GPUs", extra_exclude=["for parts"])
    db_session.add(cat)
    q1 = QueryObject(id="gpu-rtx-5090", content="RTX 5090", category_id="gpu")
    q2 = QueryObject(id="gpu-rtx-4090", content="RTX 4090", category_id="gpu")
    db_session.add_all([q1, q2])
    _tracked(db_session, qid="gpu-rtx-5090", url="https://x/1", title="RTX 5090 for parts")
    _tracked(db_session, qid="gpu-rtx-4090", url="https://x/2", title="RTX 4090 for parts")
    _tracked(db_session, qid="gpu-rtx-4090", url="https://x/3", title="RTX 4090 works great")
    db_session.flush()

    n = apply_excludes_to_category(db_session, cat)

    assert n == 2
