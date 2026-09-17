"""Desired-replica math for queue-driven autoscaling.

Pure function so it is trivially unit-testable. Only invoked by the reconcile
loop when ``AUTOSCALE_ENABLED`` is set.
"""

from __future__ import annotations

import math


def desired_replicas(
    queue_len: int,
    pending: int,
    *,
    per_replica_target: int,
    min_replicas: int,
    max_replicas: int,
) -> int:
    """One replica per ``per_replica_target`` outstanding jobs, clamped.

    ``queue_len`` is undelivered stream depth; ``pending`` is claimed-but-unacked.
    """
    per_replica_target = max(per_replica_target, 1)
    outstanding = max(queue_len, 0) + max(pending, 0)
    want = math.ceil(outstanding / per_replica_target) if outstanding else min_replicas
    return max(min_replicas, min(want, max_replicas))
