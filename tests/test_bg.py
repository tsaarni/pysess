"""Tests for the `pysess.bg` Python API."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterator

import pytest

from pysess import bg


@pytest.fixture
def runtime_env() -> Iterator[dict[str, str]]:
    with tempfile.TemporaryDirectory(prefix="ps-") as short_dir:
        env = os.environ.copy()
        env["PYSESS_RUNTIME_DIR"] = short_dir
        try:
            yield env
        finally:
            _run_cli("kill", "--all", env=env)


def _run_cli(*args: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "pysess", *args],
        text=True,
        capture_output=True,
        env=env,
        timeout=15,
        check=False,
    )


def _ok(*args: str, env: dict[str, str]) -> str:
    res = _run_cli(*args, env=env)
    assert res.returncode == 0, f"stdout={res.stdout!r} stderr={res.stderr!r}"
    return res.stdout


def test_bg_returns_result_and_passes_arguments() -> None:
    f = bg(lambda a, b=0: a + b, 1, b=2)
    assert f.result(timeout=5) == 3


def test_bg_keeps_exception_in_future() -> None:
    def fail() -> None:
        raise ValueError("boom")

    f = bg(fail)
    assert isinstance(f.exception(timeout=5), ValueError)
    with pytest.raises(ValueError, match="boom"):
        f.result()


def test_bg_runs_in_daemon_thread_and_is_running_until_done() -> None:
    release = threading.Event()
    seen: list[threading.Thread] = []

    def blocked() -> str:
        seen.append(threading.current_thread())
        release.wait(5)
        return "done"

    f = bg(blocked)
    deadline = time.monotonic() + 5
    while not seen:
        assert time.monotonic() < deadline
        time.sleep(0.01)
    assert seen[0].daemon
    assert seen[0].name == "pysess.bg-blocked"
    assert f.running()
    assert not f.done()
    assert not f.cancel()
    release.set()
    assert f.result(timeout=5) == "done"


def test_bg_keeps_session_free_and_kill_ends_blocked_session(
    runtime_env: dict[str, str],
) -> None:
    # A call blocked in accept() must not make the session busy.
    _ok("new", "-n", "bgt", env=runtime_env)
    _ok(
        "exec",
        "bgt",
        "-c",
        "import os, socket, pysess\n"
        "srv = socket.create_server(('127.0.0.1', 0))\n"
        "f = pysess.bg(lambda: srv.accept()[1][0])\n"
        "pid = os.getpid()",
        env=runtime_env,
    )
    out = _ok("exec", "bgt", "-c", "f.done()", env=runtime_env)
    assert "False" in out

    out = _ok(
        "exec",
        "bgt",
        "-c",
        "socket.create_connection(srv.getsockname()).close()\nf.result(timeout=5)",
        env=runtime_env,
    )
    assert "'127.0.0.1'" in out

    # A second call stays blocked forever; kill must still end the session process.
    out = _ok("exec", "bgt", "-c", "g = pysess.bg(srv.accept)\nprint('pid', pid)", env=runtime_env)
    pid = int(out.split("pid ")[1].split()[0])
    _ok("kill", "bgt", env=runtime_env)
    deadline = time.monotonic() + 5
    while True:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        assert time.monotonic() < deadline, f"session process {pid} still running"
        time.sleep(0.05)
