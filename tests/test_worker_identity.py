"""Horizontal scaling, part 1: worker identity.

The one change horizontal scaling actually required was making
``CHRONOS_WORKER_ID`` optional, deriving a unique-per-container id from the
hostname when it is not set (see mq/config.py::derive_worker_id and
WorkerConfig.from_env). These are pure config tests -- no DB, no broker.

They pin the three properties lease-ownership fencing depends on:

  * unique across replicas (different container hostnames -> different ids)
  * stable for the process lifetime (deterministic given a fixed hostname;
    never random)
  * explicit override preserved (an operator-set CHRONOS_WORKER_ID still
    wins exactly as before)
"""

from __future__ import annotations

import pytest

from mq.config import ConfigError, WorkerConfig, derive_worker_id


# ---------------------------------------------------------------------------
# derive_worker_id: the scheme itself
# ---------------------------------------------------------------------------


def test_derive_worker_id_uses_task_type_and_hostname():
    assert derive_worker_id("echo", hostname="abc123") == "worker-echo-abc123"


def test_derive_worker_id_is_deterministic_for_a_fixed_hostname():
    # Same inputs -> same id, every time. Nothing random: the id a
    # container computes at startup is the id it keeps for its whole life.
    first = derive_worker_id("sleep", hostname="host-1")
    second = derive_worker_id("sleep", hostname="host-1")
    assert first == second == "worker-sleep-host-1"


def test_derive_worker_id_is_unique_across_replica_hostnames():
    # Three replicas of the SAME service (same task type) on three
    # different container hostnames get three distinct ids -- exactly what
    # `docker compose up --scale worker-echo=3` produces.
    ids = {derive_worker_id("echo", hostname=h) for h in ("c1", "c2", "c3")}
    assert len(ids) == 3


def test_derive_worker_id_defaults_to_real_hostname(monkeypatch):
    monkeypatch.setattr("mq.config.socket.gethostname", lambda: "container-xyz")
    assert derive_worker_id("http_request") == "worker-http_request-container-xyz"


# ---------------------------------------------------------------------------
# WorkerConfig.from_env: derivation vs explicit override
# ---------------------------------------------------------------------------


def test_from_env_derives_worker_id_when_unset(monkeypatch):
    monkeypatch.delenv("CHRONOS_WORKER_ID", raising=False)
    monkeypatch.setenv("CHRONOS_WORKER_TASK_TYPE", "echo")
    monkeypatch.setattr("mq.config.socket.gethostname", lambda: "replica-7")

    config = WorkerConfig.from_env()

    assert config.worker_id == "worker-echo-replica-7"
    assert config.task_type == "echo"


def test_from_env_explicit_worker_id_still_wins(monkeypatch):
    # Pre-existing behavior preserved: an operator can still pin an id.
    monkeypatch.setenv("CHRONOS_WORKER_ID", "hand-picked-worker")
    monkeypatch.setenv("CHRONOS_WORKER_TASK_TYPE", "echo")
    monkeypatch.setattr("mq.config.socket.gethostname", lambda: "replica-7")

    config = WorkerConfig.from_env()

    assert config.worker_id == "hand-picked-worker"


def test_from_env_empty_worker_id_is_treated_as_unset(monkeypatch):
    # An empty string is not a usable id -- fall through to derivation
    # rather than accepting "" as the lease owner.
    monkeypatch.setenv("CHRONOS_WORKER_ID", "")
    monkeypatch.setenv("CHRONOS_WORKER_TASK_TYPE", "sleep")
    monkeypatch.setattr("mq.config.socket.gethostname", lambda: "h9")

    config = WorkerConfig.from_env()

    assert config.worker_id == "worker-sleep-h9"


def test_from_env_still_requires_task_type(monkeypatch):
    # task_type has no safe default -- a worker must be pinned to exactly
    # one queue -- so this remains a hard failure.
    monkeypatch.delenv("CHRONOS_WORKER_ID", raising=False)
    monkeypatch.delenv("CHRONOS_WORKER_TASK_TYPE", raising=False)

    with pytest.raises(ConfigError):
        WorkerConfig.from_env()


def test_two_scaled_replicas_derive_distinct_ids(monkeypatch):
    """The end-to-end property that makes `--scale worker-echo=N` safe:
    two processes with IDENTICAL env (same task type, no explicit id) but
    different container hostnames resolve to different worker ids."""
    monkeypatch.delenv("CHRONOS_WORKER_ID", raising=False)
    monkeypatch.setenv("CHRONOS_WORKER_TASK_TYPE", "echo")

    monkeypatch.setattr("mq.config.socket.gethostname", lambda: "echo-replica-1")
    replica_1 = WorkerConfig.from_env()

    monkeypatch.setattr("mq.config.socket.gethostname", lambda: "echo-replica-2")
    replica_2 = WorkerConfig.from_env()

    assert replica_1.task_type == replica_2.task_type == "echo"
    assert replica_1.worker_id != replica_2.worker_id
