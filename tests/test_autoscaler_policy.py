"""Autoscaling test 1 + 2: backlog -> desired replica calculation, and
min/max clamping. Pure function, no I/O."""

from __future__ import annotations

import pytest

from autoscaler.policy import DEFAULT_BACKLOG_TIERS, ScalingPolicy


@pytest.mark.parametrize(
    "backlog,expected",
    [
        (0, 1),
        (1, 1),
        (2, 1),
        (3, 2),
        (5, 2),
        (6, 3),
        (10, 3),
        (11, 4),
        (20, 4),
        (21, 5),
        (100, 5),
    ],
)
def test_default_tiers_map_backlog_to_replicas(backlog, expected):
    policy = ScalingPolicy()  # defaults: min=1, max=5, documented tiers
    assert policy.desired_replicas(backlog) == expected


def test_negative_backlog_floors_to_min():
    policy = ScalingPolicy()
    assert policy.desired_replicas(-5) == 1


def test_clamp_to_max():
    # A tier table that would ask for 9, capped at max_replicas=3.
    policy = ScalingPolicy(min_replicas=1, max_replicas=3, tiers=((0, 1), (5, 9)))
    assert policy.desired_replicas(100) == 3


def test_clamp_to_min():
    # High floor: even backlog 0 never drops below min_replicas=2.
    policy = ScalingPolicy(min_replicas=2, max_replicas=5)
    assert policy.desired_replicas(0) == 2
    assert policy.desired_replicas(3) == 2  # tier says 2 anyway
    assert policy.desired_replicas(6) == 3


def test_custom_linear_ish_tiers():
    policy = ScalingPolicy(min_replicas=1, max_replicas=10, tiers=((0, 1), (10, 2), (20, 3)))
    assert policy.desired_replicas(0) == 1
    assert policy.desired_replicas(9) == 1
    assert policy.desired_replicas(10) == 2
    assert policy.desired_replicas(25) == 3


def test_default_tiers_constant_is_what_policy_uses():
    assert ScalingPolicy().tiers == DEFAULT_BACKLOG_TIERS


def test_invalid_bounds_rejected():
    with pytest.raises(ValueError):
        ScalingPolicy(min_replicas=5, max_replicas=2)
    with pytest.raises(ValueError):
        ScalingPolicy(tiers=())
