"""Autoscaling test 7 (+ 8): the runtime adapter. DockerComposeRuntime's
command construction and output parsing are verified with a fake command
runner -- no real Docker daemon. Test 8's property (scaled replicas keep a
unique derived identity) is enforced here by asserting the scale command
never injects CHRONOS_WORKER_ID; the identity derivation itself is proven
in tests/test_worker_identity.py."""

from __future__ import annotations

import subprocess

import pytest

from autoscaler.runtime import DockerComposeRuntime, RuntimeError_, WorkerRuntime


class FakeRunner:
    """Records argv and returns canned CompletedProcess results, keyed by a
    substring match on the argv so different subcommands (inspect/ps/up)
    can each get their own scripted output."""

    def __init__(self, responses: dict[str, tuple[int, str, str]]):
        self.responses = responses
        self.calls: list[list[str]] = []

    def __call__(self, argv):
        self.calls.append(argv)
        for needle, (code, out, err) in self.responses.items():
            if needle in argv:
                return subprocess.CompletedProcess(argv, code, out, err)
        return subprocess.CompletedProcess(argv, 0, "", "")


def _runtime(runner, **kw):
    return DockerComposeRuntime(
        project="chronos",
        compose_file="docker-compose.yml",
        project_directory="/app",
        service_prefix="worker-",
        runner=runner,
        **kw,
    )


def test_is_a_worker_runtime():
    assert isinstance(_runtime(FakeRunner({})), WorkerRuntime)


def test_service_name_convention():
    rt = _runtime(FakeRunner({}))
    assert rt.service_for("echo") == "worker-echo"
    assert rt.service_for("http_request") == "worker-http_request"


def test_current_replicas_counts_ps_lines():
    # `docker compose ps -q worker-echo` -> three container ids
    runner = FakeRunner({"ps": (0, "id1\nid2\nid3\n", "")})
    assert _runtime(runner).current_replicas("echo") == 3


def test_current_replicas_zero_when_no_output():
    runner = FakeRunner({"ps": (0, "\n  \n", "")})
    assert _runtime(runner).current_replicas("echo") == 0


def test_current_replicas_raises_on_failure():
    runner = FakeRunner({"ps": (1, "", "boom")})
    with pytest.raises(RuntimeError_):
        _runtime(runner).current_replicas("echo")


def test_scale_builds_expected_command():
    runner = FakeRunner({"up": (0, "", "")})
    _runtime(runner).scale("echo", 3)

    argv = runner.calls[-1]
    joined = " ".join(argv)
    assert argv[:2] == ["docker", "compose"]
    assert "-p" in argv and "chronos" in argv
    assert "-f" in argv and "docker-compose.yml" in argv
    assert "--project-directory" in argv and "/app" in argv
    assert "up" in argv and "-d" in argv
    assert "--no-deps" in argv
    assert "--no-recreate" in argv
    assert "--scale" in argv
    assert "worker-echo=3" in argv
    # the service is named explicitly so the command can touch nothing else
    assert argv[-1] == "worker-echo"
    # test 8: identity must stay derived -- never pin a worker id on scale
    assert "CHRONOS_WORKER_ID" not in joined


def test_scale_rejects_negative():
    with pytest.raises(ValueError):
        _runtime(FakeRunner({})).scale("echo", -1)


def test_scale_raises_on_failure():
    runner = FakeRunner({"up": (1, "", "no such service")})
    with pytest.raises(RuntimeError_):
        _runtime(runner).scale("echo", 2)


def test_project_auto_detected_from_own_container_label():
    # project=None -> runtime inspects its own hostname's compose label.
    runner = FakeRunner({"inspect": (0, "chronos\n", ""), "ps": (0, "id1\n", "")})
    rt = DockerComposeRuntime(project=None, runner=runner)
    assert rt.current_replicas("echo") == 1
    # the inspect call happened and its result was used as -p value
    inspect_calls = [c for c in runner.calls if "inspect" in c]
    assert len(inspect_calls) == 1
    ps_call = [c for c in runner.calls if "ps" in c][0]
    assert "chronos" in ps_call


def test_project_auto_detect_failure_raises():
    runner = FakeRunner({"inspect": (1, "", "not found")})
    rt = DockerComposeRuntime(project=None, runner=runner)
    with pytest.raises(RuntimeError_):
        rt.current_replicas("echo")
