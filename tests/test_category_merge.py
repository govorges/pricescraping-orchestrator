from __future__ import annotations

from orchestrator.models import Category, QueryObject


def test_snapshot_without_category_is_unchanged():
    """A query with no category snapshots exactly its own raw fields -- no merge logic kicks in."""
    q = QueryObject(id="q1", content="Widget", exclude=["broken"], minimum_value=10)
    snap = q.snapshot()
    assert snap["Exclude"] == ["broken"]
    assert snap["MinimumValue"] == 10
    assert "MaximumValue" not in snap


def test_category_adds_exclude_phrases_without_duplicating():
    """effective_exclude() is the union of the query's own and the category's phrases, with no repeats."""
    cat = Category(id="gpu", extra_exclude=["mining rig", "broken"])
    q = QueryObject(id="q1", content="Widget", exclude=["broken", "bracket"])
    q.category = cat
    exclude = q.effective_exclude()
    assert exclude.count("broken") == 1
    assert set(exclude) == {"broken", "bracket", "mining rig"}


def test_category_floor_and_ceiling_only_narrow():
    """A category's floor/ceiling pull a wider query band inward to the guard rails."""
    cat = Category(id="gpu", minimum_value_floor=200, maximum_value_ceiling=3000)
    q = QueryObject(id="q1", content="Widget", minimum_value=50, maximum_value=5000)
    q.category = cat
    lo, hi = q.effective_band()
    assert (lo, hi) == (200, 3000)


def test_category_does_not_widen_a_tighter_band():
    """A category's floor/ceiling never loosen a query's own band that's already narrower."""
    cat = Category(id="gpu", minimum_value_floor=10, maximum_value_ceiling=10000)
    q = QueryObject(id="q1", content="Widget", minimum_value=500, maximum_value=1000)
    q.category = cat
    lo, hi = q.effective_band()
    assert (lo, hi) == (500, 1000)


def test_snapshot_includes_category_merge():
    """The job-payload snapshot (not just effective_exclude()/effective_band() directly) reflects the category merge."""
    cat = Category(id="gpu", extra_exclude=["mining rig"], maximum_value_ceiling=3000)
    q = QueryObject(id="q1", content="RTX 5090", exclude=["bracket"], maximum_value=5000)
    q.category = cat
    snap = q.snapshot()
    assert set(snap["Exclude"]) == {"bracket", "mining rig"}
    assert snap["MaximumValue"] == 3000
