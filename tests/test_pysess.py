"""Tests for the pysess CLI and session daemon."""

from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from pysess.cli import _format_idle_duration, _IncrementalBuffer, build_parser


@pytest.fixture
def runtime_env() -> Iterator[dict[str, str]]:
    with tempfile.TemporaryDirectory(prefix="ps-") as short_dir:
        env = os.environ.copy()
        env["PYSESS_RUNTIME_DIR"] = short_dir
        yield env


def _run_cli(
    *args: str,
    env: dict[str, str],
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "pysess", *args],
        input=input_text,
        text=True,
        capture_output=True,
        env=env,
        timeout=15,
    )


def _extract_session_id(output: str) -> str:
    match = re.search(r"\[session:\s*([a-z0-9]+)\]", output)
    assert match is not None, f"Could not find session ID in: {output!r}"
    return match.group(1)


def test_cli_default_timeouts() -> None:
    parser = build_parser()
    assert parser.parse_args(["new"]).timeout == 10.0
    assert parser.parse_args(["exec", "k9x2"]).timeout == 10.0
    assert parser.parse_args(["wait", "k9x2"]).timeout == 30.0
    assert parser.parse_args(["wait", "k9x2", "-t", "0"]).timeout == 0.0


def test_persistent_state_and_jupyter_eval(runtime_env: dict[str, str]) -> None:
    res = _run_cli("new", "-c", "x = 40\ny = 2", env=runtime_env)
    assert res.returncode == 0, res.stderr
    sid = _extract_session_id(res.stdout)

    try:
        res = _run_cli("exec", sid, "-c", "x + y", env=runtime_env)
        assert res.returncode == 0
        assert res.stdout.strip() == "42"

        res = _run_cli("exec", sid, "-c", "_ * 2", env=runtime_env)
        assert res.returncode == 0
        assert res.stdout.strip() == "84"
    finally:
        _run_cli("kill", sid, env=runtime_env)


def test_repr_truncation_vs_explicit_print(runtime_env: dict[str, str]) -> None:
    res = _run_cli("new", env=runtime_env)
    assert res.returncode == 0
    sid = _extract_session_id(res.stdout)

    try:
        res = _run_cli("exec", sid, "-c", "'a' * 40000", env=runtime_env)
        assert res.returncode == 0
        assert "truncated" in res.stdout
        assert "full object stored in _" in res.stdout

        res = _run_cli("exec", sid, "-c", "len(_)", env=runtime_env)
        assert res.returncode == 0
        assert res.stdout.strip() == "40000"

        res = _run_cli("exec", sid, "-c", "print('b' * 40000)", env=runtime_env)
        assert res.returncode == 0
        assert res.stdout.strip() == "b" * 40000
    finally:
        _run_cli("kill", sid, env=runtime_env)


def test_traceback_strips_internal_frames(runtime_env: dict[str, str]) -> None:
    res = _run_cli("new", env=runtime_env)
    assert res.returncode == 0
    sid = _extract_session_id(res.stdout)

    try:
        res = _run_cli("exec", sid, "-c", "def boom():\n    return 1 / 0\nboom()", env=runtime_env)
        assert res.returncode == 1
        assert 'File "<repl>"' in res.stderr
        assert "ZeroDivisionError" in res.stderr
        assert "_execute_cell" not in res.stderr
    finally:
        _run_cli("kill", sid, env=runtime_env)


def test_slow_call_detach_location_incremental_wait_and_busy_guard(
    runtime_env: dict[str, str],
) -> None:
    res = _run_cli("new", "-c", "import time", env=runtime_env)
    assert res.returncode == 0
    sid = _extract_session_id(res.stdout)

    try:
        code = "print('step1', flush=True)\ntime.sleep(0.5)\nprint('step2', flush=True)\n'done'"
        res = _run_cli("exec", sid, "-t", "0.15", "-c", code, env=runtime_env)
        assert res.returncode == 0
        assert "step1" in res.stdout
        assert f"[session: {sid} | running" in res.stdout
        assert "<repl>:2" in res.stdout

        busy = _run_cli("exec", sid, "-c", "1 + 1", env=runtime_env)
        assert busy.returncode == 1
        assert f"[session: {sid} | busy" in busy.stderr

        poll = _run_cli("wait", sid, "-t", "0", env=runtime_env)
        assert poll.returncode == 0
        assert "step1" not in poll.stdout

        done = _run_cli("wait", sid, "-t", "5", env=runtime_env)
        assert done.returncode == 0
        assert "step2" in done.stdout
        assert "'done'" in done.stdout
    finally:
        _run_cli("kill", sid, env=runtime_env)


def test_blocking_server_accept_and_auto_flush_on_next_exec(
    runtime_env: dict[str, str],
) -> None:
    setup = (
        "import socket\n"
        "srv = socket.create_server(('127.0.0.1', 0))\n"
        "print(f'PORT={srv.getsockname()[1]}', flush=True)\n"
        "conn, addr = srv.accept()\n"
        "msg = conn.recv(64)\n"
        "print('received:', msg.decode(), flush=True)\n"
        "conn.close()\n"
        "srv.close()\n"
    )
    res = _run_cli("new", "-t", "0.2", "-c", setup, env=runtime_env)
    assert res.returncode == 0
    sid = _extract_session_id(res.stdout)

    try:
        assert f"[session: {sid} | running" in res.stdout
        assert "in accept()" in res.stdout
        port_match = re.search(r"PORT=(\d+)", res.stdout)
        assert port_match is not None
        port = int(port_match.group(1))

        with socket.create_connection(("127.0.0.1", port), timeout=2.0) as client:
            client.sendall(b"hello_from_client")

        time.sleep(0.1)

        # Jump straight to the next `exec` without calling `wait`:
        follow_up = _run_cli("exec", sid, "-c", "msg", env=runtime_env)
        assert follow_up.returncode == 0
        assert "received: hello_from_client" in follow_up.stdout
        assert "b'hello_from_client'" in follow_up.stdout
    finally:
        _run_cli("kill", sid, env=runtime_env)


def test_cancel_interrupts_blocking_call(runtime_env: dict[str, str]) -> None:
    res = _run_cli("new", "-c", "import time", env=runtime_env)
    assert res.returncode == 0
    sid = _extract_session_id(res.stdout)

    try:
        res = _run_cli("exec", sid, "-t", "0.1", "-c", "time.sleep(30)", env=runtime_env)
        assert res.returncode == 0
        assert f"[session: {sid} | running" in res.stdout

        cancel_res = _run_cli("cancel", sid, env=runtime_env)
        assert cancel_res.returncode == 1
        assert "KeyboardInterrupt" in cancel_res.stderr
        assert "_sigint_handler" not in cancel_res.stderr

        after = _run_cli("exec", sid, "-c", "123", env=runtime_env)
        assert after.returncode == 0
        assert after.stdout.strip() == "123"
    finally:
        _run_cli("kill", sid, env=runtime_env)


def test_ps_and_kill_all(runtime_env: dict[str, str]) -> None:
    res = _run_cli("new", "-c", "alpha = [1, 2, 3]", env=runtime_env)
    assert res.returncode == 0
    sid = _extract_session_id(res.stdout)

    try:
        ps_all = _run_cli("ps", env=runtime_env)
        assert ps_all.returncode == 0
        assert sid in ps_all.stdout

        ps_one = _run_cli("ps", sid, env=runtime_env)
        assert ps_one.returncode == 0
        assert "alpha  list" in ps_one.stdout
    finally:
        kill_res = _run_cli("kill", "--all", env=runtime_env)
        assert kill_res.returncode == 0
        assert f"[session: {sid} | killed]" in kill_res.stdout

    kill_empty = _run_cli("kill", "--all", env=runtime_env)
    assert kill_empty.returncode == 0
    assert "No active sessions." in kill_empty.stdout

    ps_empty = _run_cli("ps", env=runtime_env)
    assert ps_empty.returncode == 0
    assert "No active sessions." in ps_empty.stdout


def test_idle_timeout_triggers_clean_exit_and_atexit(
    runtime_env: dict[str, str],
    tmp_path: Path,
) -> None:
    marker = tmp_path / "atexit.txt"
    env = dict(runtime_env)
    env["PYSESS_IDLE_TIMEOUT"] = "0.4"

    code = f"import atexit\natexit.register(lambda: open({str(marker)!r}, 'w').write('closed'))"
    res = _run_cli("new", "-c", code, env=env)
    assert res.returncode == 0
    sid = _extract_session_id(res.stdout)

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and not marker.exists():
        time.sleep(0.05)

    assert marker.exists()
    assert marker.read_text() == "closed"

    missing = _run_cli("exec", sid, "-c", "1", env=env)
    assert missing.returncode == 1


def test_kill_while_job_is_running(runtime_env: dict[str, str]) -> None:
    res = _run_cli("new", "-c", "import time", env=runtime_env)
    assert res.returncode == 0
    sid = _extract_session_id(res.stdout)

    res = _run_cli("exec", sid, "-t", "0.1", "-c", "time.sleep(30)", env=runtime_env)
    assert res.returncode == 0
    assert f"[session: {sid} | running" in res.stdout

    kill_res = _run_cli("kill", sid, env=runtime_env)
    assert kill_res.returncode == 0
    assert f"[session: {sid} | killed]" in kill_res.stdout

    ps_res = _run_cli("ps", env=runtime_env)
    assert ps_res.returncode == 0
    assert "No active sessions." in ps_res.stdout


def test_kill_idle_session(runtime_env: dict[str, str]) -> None:
    res = _run_cli("new", "-c", "x = 1", env=runtime_env)
    assert res.returncode == 0
    sid = _extract_session_id(res.stdout)

    kill_res = _run_cli("kill", sid, env=runtime_env)
    assert kill_res.returncode == 0
    assert f"[session: {sid} | killed]" in kill_res.stdout
    assert kill_res.stderr == ""

    ps_res = _run_cli("ps", env=runtime_env)
    assert ps_res.returncode == 0
    assert "No active sessions." in ps_res.stdout


def test_incremental_buffer_clears_chunks_and_exposes_encoding(
    runtime_env: dict[str, str],
) -> None:
    buf = _IncrementalBuffer()
    assert buf.encoding == "utf-8"
    assert buf.errors == "replace"
    assert buf.isatty() is False
    buf.write("hello ")
    buf.write("world")
    assert buf.read_delta() == "hello world"
    assert buf._chunks == []
    assert buf.read_delta() == ""

    res = _run_cli("new", "-c", "import sys; print(sys.stdout.encoding)", env=runtime_env)
    assert res.returncode == 0
    sid = _extract_session_id(res.stdout)
    try:
        assert "utf-8" in res.stdout
    finally:
        _run_cli("kill", sid, env=runtime_env)


def test_sys_exit_zero_and_nonzero_deliver_responses(runtime_env: dict[str, str]) -> None:
    res = _run_cli("new", env=runtime_env)
    assert res.returncode == 0
    sid1 = _extract_session_id(res.stdout)

    exit_ok = _run_cli("exec", sid1, "-c", "import sys; print('bye'); sys.exit(0)", env=runtime_env)
    assert exit_ok.returncode == 0
    assert exit_ok.stdout.strip() == "bye"

    res2 = _run_cli("new", env=runtime_env)
    assert res2.returncode == 0
    sid2 = _extract_session_id(res2.stdout)

    exit_err = _run_cli("exec", sid2, "-c", "import sys; sys.exit('fatal error')", env=runtime_env)
    assert exit_err.returncode == 1
    assert "fatal error" in exit_err.stderr


def test_main_module_semantics_and_builtins_module(runtime_env: dict[str, str]) -> None:
    code = (
        "from __future__ import annotations\n"
        "import types, typing\n"
        "from dataclasses import dataclass\n"
        "class Inner:\n"
        "    pass\n"
        "@dataclass\n"
        "class Outer:\n"
        "    item: Inner\n"
        "hints = typing.get_type_hints(Outer)\n"
        "(hints['item'] is Inner, isinstance(__builtins__, types.ModuleType))\n"
    )
    res = _run_cli("new", "-c", code, env=runtime_env)
    assert res.returncode == 0, res.stderr
    sid = _extract_session_id(res.stdout)
    try:
        assert "(True, True)" in res.stdout
    finally:
        _run_cli("kill", sid, env=runtime_env)


def test_new_with_missing_file_does_not_leak_session(
    runtime_env: dict[str, str],
    tmp_path: Path,
) -> None:
    missing_file = tmp_path / "does_not_exist.py"
    res = _run_cli("new", str(missing_file), env=runtime_env)
    assert res.returncode == 1
    assert "[session:" not in res.stdout

    ps_res = _run_cli("ps", env=runtime_env)
    assert ps_res.returncode == 0
    assert "No active sessions." in ps_res.stdout


def test_invalid_session_id_rejected(runtime_env: dict[str, str]) -> None:
    res = _run_cli("exec", "../traversal", "-c", "1", env=runtime_env)
    assert res.returncode == 1
    assert "Invalid session ID" in res.stderr


def test_format_idle_duration() -> None:
    assert _format_idle_duration(0.0) == "0s"
    assert _format_idle_duration(0.9) == "0s"
    assert _format_idle_duration(5.2) == "5s"
    assert _format_idle_duration(59.9) == "59s"
    assert _format_idle_duration(60.0) == "1m"
    assert _format_idle_duration(65.0) == "1m5s"
    assert _format_idle_duration(3600.0) == "1h"
    assert _format_idle_duration(3660.0) == "1h1m"
    assert _format_idle_duration(86400.0) == "1d"
    assert _format_idle_duration(90000.0) == "1d1h"
