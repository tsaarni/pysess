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

from pysess.cli import _format_idle_duration, _format_repl_cell, build_parser


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
        check=False,
    )


def _extract_session_id(output: str) -> str:
    match = re.search(r"\[pysess:\s*([a-z0-9]+)\]", output)
    assert match is not None, f"Could not find session ID in: {output!r}"
    return match.group(1)


def test_cli_default_timeouts() -> None:
    parser = build_parser()
    assert parser.parse_args(["new"]).timeout == 10.0
    assert parser.parse_args(["new"]).detach is False
    assert parser.parse_args(["new", "-d"]).detach is True
    assert parser.parse_args(["new", "--detach"]).detach is True
    assert parser.parse_args(["new", "--timeout", "2.5"]).timeout == 2.5
    assert parser.parse_args(["exec", "k9x2"]).timeout == 10.0
    assert parser.parse_args(["exec", "k9x2", "-d"]).detach is True
    assert parser.parse_args(["exec", "k9x2", "--detach"]).detach is True
    assert parser.parse_args(["exec", "k9x2", "--timeout", "1.5"]).timeout == 1.5
    assert parser.parse_args(["wait", "k9x2"]).timeout == 30.0
    assert parser.parse_args(["wait", "k9x2", "-t", "0"]).timeout == 0.0
    assert parser.parse_args(["wait", "k9x2", "--timeout", "0"]).timeout == 0.0

    with pytest.raises(SystemExit):
        parser.parse_args(["new", "-d", "-t", "1"])
    with pytest.raises(SystemExit):
        parser.parse_args(["exec", "k9x2", "--detach", "--timeout", "1"])


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
        assert f"[pysess: {sid} | running" in res.stdout
        assert "<repl>:2" in res.stdout

        busy = _run_cli("exec", sid, "-c", "1 + 1", env=runtime_env)
        assert busy.returncode == 1
        assert f"[pysess: {sid} | busy" in busy.stderr

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
        assert f"[pysess: {sid} | running" in res.stdout
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
        assert f"[pysess: {sid} | running" in res.stdout

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
        assert f"[pysess: {sid} | killed]" in kill_res.stdout

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
    assert f"[pysess: {sid} | running" in res.stdout

    kill_res = _run_cli("kill", sid, env=runtime_env)
    assert kill_res.returncode == 0
    assert f"[pysess: {sid} | killed]" in kill_res.stdout

    ps_res = _run_cli("ps", env=runtime_env)
    assert ps_res.returncode == 0
    assert "No active sessions." in ps_res.stdout


def test_kill_idle_session(runtime_env: dict[str, str]) -> None:
    res = _run_cli("new", "-c", "x = 1", env=runtime_env)
    assert res.returncode == 0
    sid = _extract_session_id(res.stdout)

    kill_res = _run_cli("kill", sid, env=runtime_env)
    assert kill_res.returncode == 0
    assert f"[pysess: {sid} | killed]" in kill_res.stdout
    assert kill_res.stderr == ""

    ps_res = _run_cli("ps", env=runtime_env)
    assert ps_res.returncode == 0
    assert "No active sessions." in ps_res.stdout


def test_session_stdout_encoding_is_utf8(runtime_env: dict[str, str]) -> None:
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
    assert "[pysess:" not in res.stdout

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


def test_detach_flag_on_new_and_exec(runtime_env: dict[str, str]) -> None:
    start = time.monotonic()
    res = _run_cli(
        "new",
        "-d",
        "-c",
        "import time\ntime.sleep(0.25)\nprint('new_done', flush=True)\n",
        env=runtime_env,
    )
    elapsed = time.monotonic() - start
    assert res.returncode == 0, res.stderr
    assert elapsed < 1.5
    sid = _extract_session_id(res.stdout)
    assert f"[pysess: {sid} | running" in res.stdout

    try:
        wait_res = _run_cli("wait", sid, "--timeout", "5", env=runtime_env)
        assert wait_res.returncode == 0, wait_res.stderr
        assert "new_done" in wait_res.stdout

        exec_res = _run_cli(
            "exec",
            sid,
            "--detach",
            "-c",
            "time.sleep(0.2)\nprint('exec_done', flush=True)\n",
            env=runtime_env,
        )
        assert exec_res.returncode == 0, exec_res.stderr
        assert f"[pysess: {sid} | running" in exec_res.stdout

        wait_res2 = _run_cli("wait", sid, "-t", "5", env=runtime_env)
        assert wait_res2.returncode == 0, wait_res2.stderr
        assert "exec_done" in wait_res2.stdout
    finally:
        _run_cli("kill", sid, env=runtime_env)


def test_named_session_and_collision_guard(runtime_env: dict[str, str]) -> None:
    res = _run_cli("new", "-n", "http-srv", "-c", "port = 8080", env=runtime_env)
    assert res.returncode == 0, res.stderr
    assert "[pysess: http-srv]" in res.stdout

    try:
        dup = _run_cli("new", "--name", "http-srv", env=runtime_env)
        assert dup.returncode == 1
        assert "already exists" in dup.stderr

        exec_res = _run_cli("exec", "http-srv", "-c", "port", env=runtime_env)
        assert exec_res.returncode == 0, exec_res.stderr
        assert exec_res.stdout.strip() == "8080"
    finally:
        kill_res = _run_cli("kill", "http-srv", env=runtime_env)
        assert kill_res.returncode == 0

    bad = _run_cli("new", "-n", "../bad", env=runtime_env)
    assert bad.returncode == 1
    assert "Invalid session ID" in bad.stderr


def test_format_repl_cell() -> None:
    assert _format_repl_cell("x = 1\n") == ">>> x = 1\n"
    assert _format_repl_cell("for i in range(2):\n    print(i)\n") == (
        ">>> for i in range(2):\n...     print(i)\n"
    )
    assert _format_repl_cell("a = 1\n\nb = 2") == ">>> a = 1\n...\n... b = 2\n"
    assert _format_repl_cell("") == ">>> \n"


def test_session_log_dir_disabled_by_default(runtime_env: dict[str, str]) -> None:
    res = _run_cli("new", "-n", "nolog", "-c", "x = 1", env=runtime_env)
    assert res.returncode == 0, res.stderr
    try:
        log_files = list(Path(runtime_env["PYSESS_RUNTIME_DIR"]).glob("*.log"))
        assert log_files == []
    finally:
        _run_cli("kill", "nolog", env=runtime_env)


def test_session_log_dir_records_repl_transcript(runtime_env: dict[str, str]) -> None:
    with tempfile.TemporaryDirectory(prefix="pslog-") as log_dir:
        env = runtime_env.copy()
        env["PYSESS_LOG_DIR"] = log_dir

        res = _run_cli("new", "-n", "audit", "-c", "x = 40\ny = 2\n", env=env)
        assert res.returncode == 0, res.stderr

        try:
            res = _run_cli("exec", "audit", "-c", "x + y", env=env)
            assert res.returncode == 0

            res = _run_cli(
                "exec",
                "audit",
                "-c",
                "for i in range(2):\n    print(i)\n",
                env=env,
            )
            assert res.returncode == 0

            res = _run_cli("exec", "audit", "-c", "print('no_nl', end='')", env=env)
            assert res.returncode == 0

            res = _run_cli("exec", "audit", "-c", "1 / 0", env=env)
            assert res.returncode == 1
        finally:
            _run_cli("kill", "audit", env=env)

        log_files = list(Path(log_dir).glob("*.log"))
        assert len(log_files) == 1
        log_path = log_files[0]
        assert re.fullmatch(r"\d{8}-\d{6}-audit\.log", log_path.name)

        content = log_path.read_text(encoding="utf-8")
        ts_re = r"# pysess \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:[+-]\d{2}:\d{2}|Z)"
        expected_re = (
            f"^{ts_re}\n"
            r">>> x = 40\n"
            r"\.\.\. y = 2\n"
            f"{ts_re}\n"
            r">>> x \+ y\n"
            r"42\n"
            f"{ts_re}\n"
            r">>> for i in range\(2\):\n"
            r"\.\.\.     print\(i\)\n"
            r"0\n"
            r"1\n"
            f"{ts_re}\n"
            r">>> print\('no_nl', end=''\)\n"
            r"no_nl\n"
            f"{ts_re}\n"
            r">>> 1 / 0\n"
            r"Traceback \(most recent call last\):"
        )
        assert re.search(expected_re, content) is not None, content
        assert "ZeroDivisionError: division by zero\n" in content


def test_fd_level_output_from_subprocesses_is_captured(runtime_env: dict[str, str]) -> None:
    res = _run_cli("new", "-n", "fdcap", "-c", "import os, subprocess, sys", env=runtime_env)
    assert res.returncode == 0, res.stderr
    try:
        code = (
            "print('before')\n"
            "subprocess.run(['sh', '-c', 'echo child-out; echo child-err >&2'])\n"
            "os.system('echo from-system')\n"
            "os.write(1, b'raw-fd\\n')\n"
            "print('after')\n"
        )
        res = _run_cli("exec", "fdcap", "-c", code, env=runtime_env)
        assert res.returncode == 0, res.stderr
        # Python and child-process output share one pipe, so their order is kept.
        assert res.stdout.splitlines() == ["before", "child-out", "from-system", "raw-fd", "after"]
        assert res.stderr.strip() == "child-err"

        res = _run_cli("exec", "fdcap", "-c", "print('ü', end='')\n1 / 0", env=runtime_env)
        assert res.returncode == 1
        assert res.stdout == "ü"
        assert "ZeroDivisionError" in res.stderr
    finally:
        _run_cli("kill", "fdcap", env=runtime_env)


def test_output_after_step_finished_is_delivered_later(runtime_env: dict[str, str]) -> None:
    res = _run_cli("new", "-n", "late", "-c", "import subprocess", env=runtime_env)
    assert res.returncode == 0, res.stderr
    try:
        res = _run_cli(
            "exec",
            "late",
            "-c",
            "p = subprocess.Popen(['sh', '-c', 'sleep 0.3; echo late-out'])",
            env=runtime_env,
        )
        assert res.returncode == 0, res.stderr
        assert "late-out" not in res.stdout
        time.sleep(0.6)

        res = _run_cli("exec", "late", "-c", "p.wait()", env=runtime_env)
        assert res.returncode == 0, res.stderr
        assert res.stdout.splitlines() == ["late-out", "0"]
    finally:
        _run_cli("kill", "late", env=runtime_env)


def test_reassigned_sys_streams_are_restored_after_step(runtime_env: dict[str, str]) -> None:
    res = _run_cli(
        "new", "-n", "streams", "-c", "import io, sys; sys.stderr = io.StringIO()", env=runtime_env
    )
    assert res.returncode == 0, res.stderr
    try:
        res = _run_cli("exec", "streams", "-c", "1 / 0", env=runtime_env)
        assert res.returncode == 1
        assert "ZeroDivisionError" in res.stderr

        code = "sys.__stdout__.write('a\\n')\nsys.stdout.buffer.write(b'b\\n')\nprint('c', end='')"
        res = _run_cli("exec", "streams", "-c", code, env=runtime_env)
        assert res.returncode == 0, res.stderr
        assert res.stdout == "a\nb\nc"
    finally:
        _run_cli("kill", "streams", env=runtime_env)


def test_logs_shows_transcript_and_tail(runtime_env: dict[str, str]) -> None:
    res = _run_cli("new", "-n", "logview", "-c", "x = 40\ny = 2\n", env=runtime_env)
    assert res.returncode == 0, res.stderr
    try:
        assert _run_cli("exec", "logview", "-c", "x + y", env=runtime_env).returncode == 0
        assert _run_cli("exec", "logview", "-c", "print('hello')", env=runtime_env).returncode == 0

        # Ephemeral transcript uses an unlinked file: no .log files in runtime_dir.
        assert list(Path(runtime_env["PYSESS_RUNTIME_DIR"]).rglob("*.log")) == []

        logs = _run_cli("logs", "logview", env=runtime_env)
        assert logs.returncode == 0, logs.stderr
        assert ">>> x = 40\n... y = 2\n" in logs.stdout
        assert ">>> x + y\n42\n" in logs.stdout
        assert ">>> print('hello')\nhello\n" in logs.stdout

        tailed = _run_cli("logs", "logview", "--tail", "2", env=runtime_env)
        assert tailed.returncode == 0, tailed.stderr
        assert tailed.stdout == ">>> print('hello')\nhello\n"

        zero = _run_cli("logs", "logview", "--tail", "0", env=runtime_env)
        assert zero.returncode == 0, zero.stderr
        assert zero.stdout == ""

        neg = _run_cli("logs", "logview", "--tail", "-1", env=runtime_env)
        assert neg.returncode == 2
        assert "must be >= 0" in neg.stderr
    finally:
        _run_cli("kill", "logview", env=runtime_env)

    missing = _run_cli("logs", "logview", env=runtime_env)
    assert missing.returncode == 1
    assert "not found" in missing.stderr


def test_logs_follow_streams_live_output_and_exits_on_kill(runtime_env: dict[str, str]) -> None:
    res = _run_cli("new", "-n", "live", "-c", "import time", env=runtime_env)
    assert res.returncode == 0, res.stderr
    follower = subprocess.Popen(
        [sys.executable, "-m", "pysess", "logs", "live", "-f", "--tail", "0"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=runtime_env,
    )
    try:
        # Give the follower a moment to attach before starting the slow detached step.
        time.sleep(0.15)
        step = _run_cli(
            "exec",
            "live",
            "-d",
            "-c",
            "print('tick-1', flush=True)\ntime.sleep(0.6)\nprint('tick-2', flush=True)",
            env=runtime_env,
        )
        assert step.returncode == 0, step.stderr

        # Read lines as they stream: tick-1 must arrive while the step is still sleeping.
        assert follower.stdout is not None
        seen: list[str] = []
        t0 = time.monotonic()
        while True:
            line = follower.stdout.readline()
            assert line != "", "follower closed before tick-1"
            seen.append(line.rstrip("\n"))
            if "tick-1" in line:
                break
        assert time.monotonic() - t0 < 0.5, f"tick-1 arrived too late: {seen}"

        wait_res = _run_cli("wait", "live", "-t", "5", env=runtime_env)
        assert wait_res.returncode == 0, wait_res.stderr
        _run_cli("kill", "live", env=runtime_env)

        rest, stderr = follower.communicate(timeout=5)
        assert follower.returncode == 0, stderr
        full = "\n".join(seen) + "\n" + rest
        assert "tick-1" in full
        assert "tick-2" in full
    finally:
        if follower.poll() is None:
            follower.kill()
            follower.wait(timeout=2)
        _run_cli("kill", "live", env=runtime_env)


def test_logs_color_formatting(runtime_env: dict[str, str]) -> None:
    res = _run_cli("new", "-n", "colorlog", "-c", "print('plain')", env=runtime_env)
    assert res.returncode == 0, res.stderr
    try:
        assert _run_cli("exec", "colorlog", "-c", "1 / 0", env=runtime_env).returncode == 1

        # Default on non-TTY pipe emits plain text with no ANSI codes.
        plain = _run_cli("logs", "colorlog", env=runtime_env)
        assert plain.returncode == 0, plain.stderr
        assert "\033[" not in plain.stdout

        # FORCE_COLOR enables ANSI styling; NO_COLOR overrides FORCE_COLOR.
        colored = _run_cli("logs", "colorlog", env={**runtime_env, "FORCE_COLOR": "1"})
        assert colored.returncode == 0, colored.stderr
        assert "\033[2m# pysess " in colored.stdout
        assert "\033[1;36m>>>\033[0m\033[1m print('plain')\033[0m\nplain\n" in colored.stdout
        assert "\033[31mTraceback (most recent call last):\033[0m\n" in colored.stdout
        assert "\033[1;31mZeroDivisionError: division by zero\033[0m\n" in colored.stdout

        disabled = _run_cli(
            "logs", "colorlog", env={**runtime_env, "FORCE_COLOR": "1", "NO_COLOR": "1"}
        )
        assert disabled.returncode == 0, disabled.stderr
        assert "\033[" not in disabled.stdout
    finally:
        _run_cli("kill", "colorlog", env=runtime_env)
