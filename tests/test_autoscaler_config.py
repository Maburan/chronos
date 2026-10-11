"""Autoscaling: AutoscalerConfig.from_env parsing (project config
conventions -- same from_env pattern as ReaperConfig/WorkerConfig)."""

from __future__ import annotations

import pytest

from autoscaler.config import AutoscalerConfig, ConfigError
from autoscaler.policy import DEFAULT_BACKLOG_TIERS


def _clear(monkeypatch):
    for key in list(__import__("os").environ):
        if key.startswith("CHRONOS_AUTOSCALER") or key == "CHRONOS_TASK_TYPES":
            monkeypatch.delenv(key, raising=False)


def test_defaults(monkeypatch):
    _clear(monkeypatch)
    cfg = AutoscalerConfig.from_env()
    assert cfg.enabled is False
    assert cfg.interval_seconds == 10.0
    assert cfg.min_replicas == 1
    assert cfg.max_replicas == 5
    assert cfg.scale_up_stabilization == 2
    assert cfg.scale_down_cooldown_seconds == 60.0
    assert cfg.service_prefix == "worker-"
    assert cfg.backlog_tiers == DEFAULT_BACKLOG_TIERS
    assert cfg.compose_project is None


@pytest.mark.parametrize("raw,expected", [("1", True), ("true", True), ("YES", True), ("0", False), ("", False), ("off", False)])
def test_enabled_parsing(monkeypatch, raw, expected):
    _clear(monkeypatch)
    monkeypatch.setenv("CHRONOS_AUTOSCALER_ENABLED", raw)
    assert AutoscalerConfig.from_env().enabled is expected


def test_task_types_falls_back_to_chronos_task_types(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("CHRONOS_TASK_TYPES", "echo, sleep ,http_request")
    cfg = AutoscalerConfig.from_env()
    assert cfg.task_types == ("echo", "sleep", "http_request")


def test_task_types_explicit_overrides_fallback(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("CHRONOS_TASK_TYPES", "echo,sleep")
    monkeypatch.setenv("CHRONOS_AUTOSCALER_TASK_TYPES", "echo")
    assert AutoscalerConfig.from_env().task_types == ("echo",)


def test_tiers_parsing_and_sorting(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("CHRONOS_AUTOSCALER_BACKLOG_TIERS", "6:3, 0:1 ,3:2")
    cfg = AutoscalerConfig.from_env()
    assert cfg.backlog_tiers == ((0, 1), (3, 2), (6, 3))
    # and it flows into the policy correctly
    assert cfg.policy().desired_replicas(6) == 3


def test_malformed_tiers_raise(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("CHRONOS_AUTOSCALER_BACKLOG_TIERS", "not-a-tier")
    with pytest.raises(ConfigError):
        AutoscalerConfig.from_env()


def test_max_below_min_rejected(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("CHRONOS_AUTOSCALER_MIN_REPLICAS", "4")
    monkeypatch.setenv("CHRONOS_AUTOSCALER_MAX_REPLICAS", "2")
    with pytest.raises(ConfigError):
        AutoscalerConfig.from_env()
