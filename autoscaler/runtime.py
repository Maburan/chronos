"""The runtime abstraction the autoscaler depends on, plus the first
concrete implementation (Docker Compose).

The autoscaler policy/loop never contains a line of Docker-specific code:
it talks only to ``WorkerRuntime``. That is what makes a future
``KubernetesRuntime`` (``kubectl scale deployment worker-echo
--replicas=N``, or the apps/v1 API) a drop-in -- it implements the same
two methods and nothing in autoscaler.py or policy.py changes.

    WorkerRuntime
        current_replicas(task_type) -> int
        scale(task_type, replicas) -> None

``DockerComposeRuntime`` maps a task type to its Compose service by a
fixed, deterministic convention (``worker-<task_type>`` -- exactly the
service names already in docker-compose.yml: worker-echo, worker-sleep,
worker-http_request, worker-python_expression) and shells out to the
Compose CLI. It does NOT spawn worker processes itself, does NOT template
containers by hand, and does NOT set CHRONOS_WORKER_ID on the new
replicas -- each scaled-up replica therefore derives its own unique
``worker-<task_type>-<hostname>`` identity exactly as the horizontal-
scaling phase established (mq.config.derive_worker_id), so lease
ownership/fencing keeps working. The scale command is deliberately
narrow and idempotent:

    docker compose -p <project> -f <file> --project-directory <dir> \\
        up -d --no-deps --no-recreate --scale worker-<type>=<N> worker-<type>

  * --no-deps      : never touch postgres/rabbitmq/api/etc.
  * --no-recreate  : leave already-running replicas alone; only add/remove
                     to reach the target count -- no churn of healthy
                     workers, so in-flight tasks are never disturbed by a
                     scale operation that didn't need to move THIS replica.
  * naming the one service : the command can only affect that service.

Reading the current count is the same CLI, ``ps -q`` (one line per running
service container). All process execution goes through an injected
``runner`` so the command construction and output parsing are unit-tested
with no real Docker (see tests/test_autoscaler_runtime.py).
"""

from __future__ import annotations

import logging
import socket
import subprocess
from dataclasses import dataclass, field
from typing import Callable, Optional, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@runtime_checkable
class WorkerRuntime(Protocol):
    """The only surface the autoscaler depends on. Implementations must be
    safe to call repeatedly and must treat ``scale`` as declarative (set
    the count to exactly ``replicas``), not incremental."""

    def current_replicas(self, task_type: str) -> int:
        """Number of worker replicas currently running for ``task_type``."""
        ...

    def scale(self, task_type: str, replicas: int) -> None:
        """Set the number of worker replicas for ``task_type`` to exactly
        ``replicas``. Declarative: calling it with the count that is
        already running must be a harmless no-op at the runtime layer."""
        ...


class RuntimeError_(RuntimeError):
    """Raised when the underlying runtime command fails. Named with a
    trailing underscore only to avoid shadowing the builtin at call sites
    that ``from autoscaler.runtime import *`` (none do, but explicit)."""


# A command runner: takes argv, returns the completed process. Injected so
# tests can supply a fake without a real Docker daemon.
CommandRunner = Callable[[list[str]], "subprocess.CompletedProcess[str]"]


def _default_runner(argv: list[str]) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(argv, capture_output=True, text=True, check=False)


@dataclass
class DockerComposeRuntime:
    """Scales Compose worker services via the Compose CLI.

    ``project`` is the Compose project name to target. If left ``None`` it
    is auto-detected once, from this autoscaler container's OWN
    ``com.docker.compose.project`` label (looked up through the mounted
    Docker socket) -- so the autoscaler always scales the very project it
    is part of, with no hand-configured name to drift. An explicit
    ``project`` (e.g. from CHRONOS_AUTOSCALER_COMPOSE_PROJECT) always wins.
    """

    project: Optional[str] = None
    compose_file: str = "docker-compose.yml"
    project_directory: str = "/app"
    service_prefix: str = "worker-"
    docker_bin: str = "docker"
    runner: CommandRunner = field(default=_default_runner)
    _resolved_project: Optional[str] = field(default=None, init=False, repr=False)

    def service_for(self, task_type: str) -> str:
        return f"{self.service_prefix}{task_type}"

    # -- project resolution ------------------------------------------------

    def _project_name(self) -> str:
        if self.project:
            return self.project
        if self._resolved_project is not None:
            return self._resolved_project
        self._resolved_project = self._detect_project()
        return self._resolved_project

    def _detect_project(self) -> str:
        """Read our own container's compose-project label via the socket."""
        hostname = socket.gethostname()
        result = self.runner(
            [
                self.docker_bin,
                "inspect",
                "--format",
                '{{ index .Config.Labels "com.docker.compose.project" }}',
                hostname,
            ]
        )
        project = (result.stdout or "").strip()
        if result.returncode != 0 or not project:
            raise RuntimeError_(
                "could not auto-detect the Compose project name from this "
                f"container ({hostname!r}); set CHRONOS_AUTOSCALER_COMPOSE_PROJECT "
                f"explicitly. docker inspect exit={result.returncode} "
                f"stderr={(result.stderr or '').strip()!r}"
            )
        return project

    # -- the compose command prefix ---------------------------------------

    def _compose_base(self) -> list[str]:
        return [
            self.docker_bin,
            "compose",
            "-p",
            self._project_name(),
            "-f",
            self.compose_file,
            "--project-directory",
            self.project_directory,
        ]

    # -- WorkerRuntime -----------------------------------------------------

    def current_replicas(self, task_type: str) -> int:
        service = self.service_for(task_type)
        result = self.runner(self._compose_base() + ["ps", "-q", service])
        if result.returncode != 0:
            raise RuntimeError_(
                f"`docker compose ps -q {service}` failed "
                f"(exit={result.returncode}): {(result.stderr or '').strip()!r}"
            )
        # One container id per line; blank lines (and a trailing newline)
        # don't count.
        return len([line for line in (result.stdout or "").splitlines() if line.strip()])

    def scale(self, task_type: str, replicas: int) -> None:
        if replicas < 0:
            raise ValueError("replicas must be >= 0")
        service = self.service_for(task_type)
        argv = self._compose_base() + [
            "up",
            "-d",
            "--no-deps",
            "--no-recreate",
            "--scale",
            f"{service}={replicas}",
            service,
        ]
        result = self.runner(argv)
        if result.returncode != 0:
            raise RuntimeError_(
                f"scaling {service} to {replicas} failed "
                f"(exit={result.returncode}): {(result.stderr or '').strip()!r}"
            )
