from __future__ import annotations

import pytest

from orchestrator.autoscale import desired_replicas


@pytest.mark.parametrize(
    "depth,pending,expected",
    [
        (0, 0, 0),
        (1, 0, 1),
        (10, 0, 1),
        (11, 0, 2),
        (5, 6, 2),
        (1000, 0, 5),  # clamped to max
    ],
)
def test_desired_replicas(depth, pending, expected):
    """Replica count scales with (queue depth + pending) / per_replica_target, clamped to max_replicas."""
    got = desired_replicas(
        depth, pending, per_replica_target=10, min_replicas=0, max_replicas=5
    )
    assert got == expected


def test_respects_min_replicas_when_idle():
    """An empty queue still keeps at least min_replicas running, never scales to zero below that floor."""
    assert desired_replicas(0, 0, per_replica_target=10, min_replicas=2, max_replicas=5) == 2


def test_zero_target_does_not_divide_by_zero():
    """A misconfigured per_replica_target of 0 must not crash -- it should fail safe, not raise."""
    assert desired_replicas(5, 0, per_replica_target=0, min_replicas=0, max_replicas=9) == 5
