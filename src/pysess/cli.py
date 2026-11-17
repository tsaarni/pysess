"""Command-line interface and persistent session daemon for `pysess`."""

from __future__ import annotations

import argparse
import ast
import atexit
import builtins
import contextlib
import io
import json
import os
import queue
import re
import secrets
import signal
import socket
import stat
import string
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import types
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

DEFAULT_EXEC_TIMEOUT = 10.0
DEFAULT_WAIT_TIMEOUT = 30.0
DEFAULT_IDLE_TIMEOUT = 3600.0
MAX_EXPR_REPR_BYTES = 32 * 1024
SESSION_ID_ALPHABET = string.ascii_lowercase + string.digits
SESSION_ID_LENGTH = 4
_SESSION_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,31}$")


def runtime_dir() -> Path:
    """Return the per-user runtime directory for session Unix sockets."""
    if custom := os.environ.get("PYSESS_RUNTIME_DIR"):
        base = Path(custom)
    elif xdg := os.environ.get("XDG_RUNTIME_DIR"):
        base = Path(xdg) / f"pysess-{os.getuid()}"
    else:
        base = Path(tempfile.gettempdir()) / f"pysess-{os.getuid()}"
    base.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        base.chmod(0o700)
    st = base.lstat()
    if not stat.S_ISDIR(st.st_mode):
        raise RuntimeError(f"Runtime path '{base}' is not a directory.")
    if st.st_uid != os.getuid():
        raise RuntimeError(f"Runtime directory '{base}' is not owned by the current user.")
    if stat.S_IMODE(st.st_mode) & 0o077:
        raise RuntimeError(f"Runtime directory '{base}' has insecure permissions.")
    return base


def socket_path(session_id: str) -> Path:
    """Return the validated Unix domain socket path for `session_id`."""
    if not _SESSION_ID_RE.match(session_id):
        raise RuntimeError(f"Invalid session ID: {session_id!r}.")
    return runtime_dir() / f"{session_id}.sock"


def idle_timeout_seconds() -> float | None:
    """Return the configured idle timeout in seconds, or `None` if disabled."""
    raw = os.environ.get("PYSESS_IDLE_TIMEOUT")
    if raw is None:
        return DEFAULT_IDLE_TIMEOUT
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_IDLE_TIMEOUT
    return value if value > 0 else None


def generate_session_id() -> str:
    """Allocate a short, unused random session ID."""
    runtime = runtime_dir()
    for _ in range(64):
        candidate = "".join(secrets.choice(SESSION_ID_ALPHABET) for _ in range(SESSION_ID_LENGTH))
        if not (runtime / f"{candidate}.sock").exists():
            return candidate
    raise RuntimeError("Failed to allocate a unique session ID.")


def _format_repl_cell(code: str) -> str:
    """Format a code cell with Python REPL `>>> ` and `... ` prompts."""
    lines = code.rstrip("\r\n").splitlines() or [""]
    return (
        "\n".join(
            f"{'>>>' if i == 0 else '...'} {line}" if line or i == 0 else "..."
            for i, line in enumerate(lines)
        )
        + "\n"
    )


class _SessionLog:
    """Thread-safe append-only REPL transcript writer for a session."""

    def __init__(self, log_path: Path) -> None:
        fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        self._file = os.fdopen(fd, "w", encoding="utf-8")
        self._lock = threading.Lock()
        self._at_bol = True

    def write(self, text: str, *, ensure_bol: bool = False) -> None:
        if not text:
            return
        with self._lock:
            if self._file.closed:
                return
            if ensure_bol and not self._at_bol:
                self._file.write("\n")
            self._file.write(text)
            self._file.flush()
            self._at_bol = text.endswith("\n")

    def close(self) -> None:
        with self._lock, contextlib.suppress(OSError):
            self._file.close()


def _open_session_log(session_id: str) -> _SessionLog | None:
    """Open a timestamped REPL log file if `PYSESS_LOG_DIR` is set."""
    if not (raw := os.environ.get("PYSESS_LOG_DIR", "").strip()):
        return None
    base = Path(raw)
    base.mkdir(parents=True, exist_ok=True)
    return _SessionLog(base / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{session_id}.log")


class _IncrementalBuffer(io.TextIOBase):
    """Thread-safe incremental text buffer that clears chunks as they are read."""

    encoding: str = "utf-8"
    errors: str | None = "replace"

    def __init__(self, on_write: Callable[[str], None] | None = None) -> None:
        self._lock = threading.Lock()
        self._chunks: list[str] = []
        self._on_write = on_write

    def isatty(self) -> bool:
        return False

    def writable(self) -> bool:
        return True

    def seed(self, s: str) -> None:
        """Append previously captured text without triggering `on_write`."""
        if not s:
            return
        with self._lock:
            self._chunks.append(s)

    def write(self, s: str) -> int:
        if not s:
            return 0
        with self._lock:
            self._chunks.append(s)
        if self._on_write is not None:
            self._on_write(s)
        return len(s)

    def flush(self) -> None:
        return None

    def read_delta(self) -> str:
        """Return and clear all text written since the previous `read_delta()` call."""
        with self._lock:
            if not self._chunks:
                return ""
            delta = "".join(self._chunks)
            self._chunks.clear()
            return delta


@dataclass
class _Job:
    """Represents a single `exec` unit of work on the daemon main thread."""

    code: str
    started_at: float = field(default_factory=time.monotonic)
    finished_at: float | None = None
    status: str = "running"  # running | ok | error
    expr_repr: str | None = None
    result_delivered: bool = False
    cancel_requested: bool = False
    stdout_buf: _IncrementalBuffer = field(default_factory=_IncrementalBuffer)
    stderr_buf: _IncrementalBuffer = field(default_factory=_IncrementalBuffer)
    done_event: threading.Event = field(default_factory=threading.Event)
    delivered_event: threading.Event = field(default_factory=threading.Event)

    @property
    def elapsed(self) -> float:
        end = self.finished_at if self.finished_at is not None else time.monotonic()
        return round(max(0.0, end - self.started_at), 3)


def _format_repl_traceback(exc: BaseException) -> str:
    """Format an exception traceback starting at the `<repl>` frame."""
    tb = exc.__traceback__
    while tb is not None and tb.tb_frame.f_code.co_filename != "<repl>":
        tb = tb.tb_next
    te = traceback.TracebackException(type(exc), exc, tb)
    if te.stack and te.stack[-1].filename == __file__ and te.stack[-1].name == "_sigint_handler":
        te.stack.pop()
    return "".join(te.format())


def _truncate_repr(text: str) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= MAX_EXPR_REPR_BYTES:
        return text
    truncated = encoded[:MAX_EXPR_REPR_BYTES].decode("utf-8", errors="ignore")
    return f"{truncated}\n... [truncated (>32 KB); full object stored in _]"


def _execute_cell(code: str, namespace: dict[str, Any]) -> str | None:
    """Compile and execute code with Jupyter-cell trailing expression semantics."""
    tree = ast.parse(code, filename="<repl>", mode="exec")
    if not tree.body:
        return None

    trailing_expr: ast.Expression | None = None
    last = tree.body[-1]
    if isinstance(last, ast.Expr):
        tree.body.pop()
        trailing_expr = ast.Expression(body=last.value)
        ast.fix_missing_locations(trailing_expr)

    if tree.body:
        exec(compile(tree, "<repl>", "exec"), namespace, namespace)

    if trailing_expr is not None:
        value = eval(compile(trailing_expr, "<repl>", "eval"), namespace, namespace)
        if value is not None:
            namespace["_"] = value
            return _truncate_repr(repr(value))
    return None


def _main_thread_location() -> str | None:
    """Inspect the main thread's current stack frame and return `<repl>:line [in func()]`."""
    main_tid = threading.main_thread().ident
    if main_tid is None:
        return None
    top_frame = sys._current_frames().get(main_tid)
    if top_frame is None:
        return None

    leaf_name: str | None = None
    repl_lineno: int | None = None
    frame: types.FrameType | None = top_frame
    while frame is not None:
        fname = frame.f_code.co_filename
        cname = frame.f_code.co_name
        if fname == "<repl>":
            repl_lineno = frame.f_lineno
            if leaf_name is None and cname != "<module>":
                leaf_name = cname
            break
        if leaf_name is None and not cname.startswith("_"):
            leaf_name = cname
        frame = frame.f_back

    if repl_lineno is None:
        return None
    if leaf_name:
        return f"<repl>:{repl_lineno} in {leaf_name}()"
    return f"<repl>:{repl_lineno}"


def _is_socket_alive(sock_path: Path) -> bool:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.2)
            probe.connect(str(sock_path))
            return True
    except OSError:
        return False


class SessionDaemon:
    """Main-thread executor + control-thread Unix socket server."""

    def __init__(self, session_id: str, sock_path: Path) -> None:
        self.session_id = session_id
        self.sock_path = sock_path
        self.idle_timeout = idle_timeout_seconds()
        self.session_log: _SessionLog | None = None

        main_module = types.ModuleType("__main__")
        main_module.__dict__["__builtins__"] = builtins
        sys.modules["__main__"] = main_module
        self.namespace: dict[str, Any] = main_module.__dict__

        self.work_queue: queue.Queue[_Job | None] = queue.Queue()
        self.shutdown_event = threading.Event()
        self.state_lock = threading.Lock()
        self.current_job: _Job | None = None
        self.last_activity = time.monotonic()
        self._in_user_code = False

        self._server_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        if self.sock_path.exists():
            if _is_socket_alive(self.sock_path):
                self._server_sock.close()
                raise RuntimeError(f"Session '{session_id}' is already active.")
            self.sock_path.unlink()
        try:
            self._server_sock.bind(str(self.sock_path))
        except OSError as exc:
            self._server_sock.close()
            raise RuntimeError(f"Failed to bind Unix socket '{self.sock_path}': {exc}") from exc
        os.chmod(self.sock_path, 0o600)
        self._server_sock.listen(16)
        self._server_sock.settimeout(0.5)
        self.session_log = _open_session_log(session_id)

    def _cleanup(self) -> None:
        if self.session_log is not None:
            self.session_log.close()
        with contextlib.suppress(OSError):
            self._server_sock.close()
        with contextlib.suppress(OSError):
            if self.sock_path.exists():
                self.sock_path.unlink()

    def _interrupt_main_thread(self) -> None:
        main_tid = threading.main_thread().ident
        if main_tid is not None:
            signal.pthread_kill(main_tid, signal.SIGINT)
        else:
            os.kill(os.getpid(), signal.SIGINT)

    def _control_loop(self) -> None:
        while not self.shutdown_event.is_set():
            try:
                conn, _ = self._server_sock.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle_conn, args=(conn,), daemon=True).start()

    def _handle_conn(self, conn: socket.socket) -> None:
        job_to_notify: _Job | None = None
        kill_after_reply = False
        with conn:
            try:
                with conn.makefile("r", encoding="utf-8") as reader:
                    line = reader.readline()
                if not line:
                    return
                req = json.loads(line)
                resp, job_to_notify = self._dispatch(req)
                kill_after_reply = req.get("op") == "kill" and resp.get("status") == "killed"
            except Exception as exc:
                resp = {"status": "error", "error": str(exc)}
            with contextlib.suppress(OSError):
                conn.sendall((json.dumps(resp) + "\n").encode("utf-8"))
        if job_to_notify is not None:
            job_to_notify.delivered_event.set()
        if kill_after_reply:
            self._initiate_kill()

    def _job_response(self, job: _Job) -> tuple[dict[str, Any], _Job | None]:
        stdout_delta = job.stdout_buf.read_delta()
        stderr_delta = job.stderr_buf.read_delta()
        if not job.done_event.is_set():
            return (
                {
                    "status": "running",
                    "elapsed": job.elapsed,
                    "location": _main_thread_location(),
                    "stdout_delta": stdout_delta,
                    "stderr_delta": stderr_delta,
                },
                None,
            )
        job.result_delivered = True
        return (
            {
                "status": job.status,
                "elapsed": job.elapsed,
                "stdout_delta": stdout_delta,
                "stderr_delta": stderr_delta,
                "expr_repr": job.expr_repr,
            },
            job,
        )

    def _dispatch(self, req: dict[str, Any]) -> tuple[dict[str, Any], _Job | None]:
        op = req.get("op")
        if op != "ps":
            with self.state_lock:
                self.last_activity = time.monotonic()

        if op == "exec":
            timeout = float(req.get("timeout", DEFAULT_EXEC_TIMEOUT))
            with self.state_lock:
                if self.current_job is not None and not self.current_job.done_event.is_set():
                    running = self.current_job
                    return (
                        {
                            "status": "busy",
                            "elapsed": running.elapsed,
                            "location": _main_thread_location(),
                            "stdout_delta": running.stdout_buf.read_delta(),
                            "stderr_delta": running.stderr_buf.read_delta(),
                        },
                        None,
                    )
                prev_stdout = ""
                prev_stderr = ""
                if self.current_job is not None and not self.current_job.result_delivered:
                    prev_stdout = self.current_job.stdout_buf.read_delta()
                    prev_stderr = self.current_job.stderr_buf.read_delta()
                    self.current_job.result_delivered = True

                on_output = self.session_log.write if self.session_log is not None else None
                job = _Job(
                    code=str(req.get("code", "")),
                    stdout_buf=_IncrementalBuffer(on_write=on_output),
                    stderr_buf=_IncrementalBuffer(on_write=on_output),
                )
                if prev_stdout:
                    job.stdout_buf.seed(prev_stdout)
                if prev_stderr:
                    job.stderr_buf.seed(prev_stderr)
                self.current_job = job
                self.work_queue.put(job)

            job.done_event.wait(timeout=max(0.0, timeout))
            with self.state_lock:
                return self._job_response(job)

        if op == "wait":
            timeout = float(req.get("timeout", DEFAULT_WAIT_TIMEOUT))
            with self.state_lock:
                job = self.current_job
            if job is None:
                return ({"status": "idle", "stdout_delta": "", "stderr_delta": ""}, None)
            if not job.done_event.is_set() and timeout > 0:
                job.done_event.wait(timeout=timeout)
            with self.state_lock:
                if job.done_event.is_set() and job.result_delivered:
                    return (
                        {
                            "status": "idle",
                            "stdout_delta": job.stdout_buf.read_delta(),
                            "stderr_delta": job.stderr_buf.read_delta(),
                        },
                        None,
                    )
                return self._job_response(job)

        if op == "cancel":
            with self.state_lock:
                job = self.current_job
                is_running = job is not None and not job.done_event.is_set()
                if is_running and job is not None:
                    job.cancel_requested = True
            if not is_running or job is None:
                return ({"status": "idle"}, None)
            self._interrupt_main_thread()
            job.done_event.wait(timeout=2.0)
            with self.state_lock:
                return self._job_response(job)

        if op == "ps":
            with self.state_lock:
                job = self.current_job
                running = job is not None and not job.done_event.is_set()
                user_vars = {
                    k: type(v).__name__
                    for k, v in self.namespace.items()
                    if not (k.startswith("__") and k.endswith("__"))
                }
                return (
                    {
                        "status": "ok",
                        "id": self.session_id,
                        "pid": os.getpid(),
                        "state": "running" if running else "idle",
                        "elapsed": job.elapsed if running and job is not None else 0.0,
                        "location": _main_thread_location() if running else None,
                        "idle_seconds": (
                            0.0 if running else round(max(0.0, time.monotonic() - self.last_activity), 1)
                        ),
                        "vars": user_vars,
                    },
                    None,
                )

        if op == "kill":
            self._cleanup()
            return ({"status": "killed", "id": self.session_id}, None)

        return ({"status": "error", "error": f"Unknown operation: {op!r}"}, None)

    def _initiate_kill(self) -> None:
        """Shut down the daemon after the kill response has been sent."""
        with self.state_lock:
            self.shutdown_event.set()
            job = self.current_job
            is_running = job is not None and not job.done_event.is_set()
            if is_running and job is not None:
                job.cancel_requested = True
            self.work_queue.put(None)
        if is_running:
            self._interrupt_main_thread()
            threading.Thread(target=self._force_exit_watchdog, daemon=True).start()

    def _force_exit_watchdog(self) -> None:
        time.sleep(0.5)
        self._cleanup()
        with contextlib.suppress(Exception):
            atexit._run_exitfuncs()
        os._exit(0)

    def run(self) -> int:
        """Run the control server thread and execute jobs on the main thread."""

        def _sigint_handler(signum: int, frame: Any) -> None:
            if self._in_user_code:
                signal.default_int_handler(signum, frame)

        signal.signal(signal.SIGINT, _sigint_handler)

        control_thread = threading.Thread(target=self._control_loop, daemon=True)
        control_thread.start()
        try:
            while not self.shutdown_event.is_set():
                try:
                    job = self.work_queue.get(timeout=0.5)
                except queue.Empty:
                    if self.idle_timeout is not None:
                        with self.state_lock:
                            idle_for = time.monotonic() - self.last_activity
                        if idle_for >= self.idle_timeout:
                            break
                    continue

                if job is None:
                    break

                exited_via_sys_exit = False
                if self.session_log is not None:
                    ts = datetime.now().astimezone().isoformat(timespec="seconds")
                    self.session_log.write(
                        f"# pysess {ts}\n{_format_repl_cell(job.code)}",
                        ensure_bol=True,
                    )
                with (
                    contextlib.redirect_stdout(job.stdout_buf),
                    contextlib.redirect_stderr(job.stderr_buf),
                ):
                    try:
                        self._in_user_code = True
                        if job.cancel_requested:
                            raise KeyboardInterrupt
                        job.expr_repr = _execute_cell(job.code, self.namespace)
                        self._in_user_code = False
                        if self.session_log is not None and job.expr_repr is not None:
                            self.session_log.write(f"{job.expr_repr}\n", ensure_bol=True)
                        job.status = "ok"
                    except BaseException as exc:
                        self._in_user_code = False
                        if isinstance(exc, SystemExit):
                            if exc.code in (None, 0):
                                job.status = "ok"
                            elif isinstance(exc.code, int):
                                job.stderr_buf.write(_format_repl_traceback(exc))
                                job.status = "error"
                            else:
                                job.stderr_buf.write(f"{exc.code}\n")
                                job.status = "error"
                            exited_via_sys_exit = True
                            self.shutdown_event.set()
                        else:
                            job.stderr_buf.write(_format_repl_traceback(exc))
                            job.status = "error"
                    finally:
                        self._in_user_code = False
                        with self.state_lock:
                            self.last_activity = time.monotonic()
                        job.finished_at = time.monotonic()
                        job.done_event.set()

                if exited_via_sys_exit:
                    job.delivered_event.wait(timeout=1.0)
                    break
        finally:
            self.shutdown_event.set()
            self._cleanup()
        return 0


def _send_request(session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    sock_path = socket_path(session_id)
    if not sock_path.exists():
        raise RuntimeError(f"Session '{session_id}' not found.")
    req_timeout = max(0.0, float(payload.get("timeout", 0.0)))
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(req_timeout + 5.0)
            sock.connect(str(sock_path))
            sock.sendall((json.dumps(payload) + "\n").encode("utf-8"))
            with sock.makefile("r", encoding="utf-8") as reader:
                line = reader.readline()
    except OSError as exc:
        raise RuntimeError(f"Session '{session_id}' is unreachable ({exc}).") from exc

    if not line:
        raise RuntimeError(f"Session '{session_id}' closed connection without a response.")
    return json.loads(line)


def _spawn_daemon(session_id: str) -> None:
    sock_path = socket_path(session_id)
    cmd = [sys.executable, "-m", "pysess", "--daemon", session_id, str(sock_path)]
    with open(os.devnull, "rb") as devnull_in, open(os.devnull, "wb") as devnull_out:
        proc = subprocess.Popen(
            cmd,
            stdin=devnull_in,
            stdout=devnull_out,
            stderr=devnull_out,
            start_new_session=True,
            close_fds=True,
        )

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if sock_path.exists():
            try:
                if _send_request(session_id, {"op": "ps"}).get("status") == "ok":
                    return
            except RuntimeError:
                pass
        if proc.poll() is not None:
            raise RuntimeError(f"Session daemon exited prematurely with code {proc.returncode}.")
        time.sleep(0.02)
    raise RuntimeError("Timed out waiting for session daemon to start.")


def _read_code(code_arg: str | None, file_arg: str | None, *, read_stdin_tty_safe: bool) -> str:
    if code_arg is not None:
        return code_arg
    if file_arg is not None:
        return Path(file_arg).read_text(encoding="utf-8")
    if read_stdin_tty_safe and sys.stdin.isatty():
        return ""
    return sys.stdin.read()


def _format_idle_duration(seconds: float) -> str:
    """Format an idle duration in whole seconds using compact human units."""
    total = max(0, int(seconds))
    if total < 60:
        return f"{total}s"
    if total < 3600:
        mins, secs = divmod(total, 60)
        return f"{mins}m{secs}s" if secs else f"{mins}m"
    if total < 86400:
        hours, rem = divmod(total, 3600)
        mins = rem // 60
        return f"{hours}h{mins}m" if mins else f"{hours}h"
    days, rem = divmod(total, 86400)
    hours = rem // 3600
    return f"{days}d{hours}h" if hours else f"{days}d"


def _format_session_status(
    session_id: str,
    state: str,
    *,
    elapsed: float | None = None,
    location: str | None = None,
    hint: bool = False,
) -> str:
    parts = [f"[pysess: {session_id} | {state}"]
    if elapsed is not None:
        parts.append(f" ({elapsed:.1f}s)")
    if location:
        parts.append(f" at {location}")
    if hint:
        parts.append(f" - use 'pysess wait {session_id}' or 'pysess cancel {session_id}'")
    parts.append("]")
    return "".join(parts)


def _format_ps_rows(rows: list[dict[str, Any]]) -> list[str]:
    table = [
        [
            resp["id"],
            f"pid={resp['pid']}",
            f"state={resp['state']}",
            f"idle={_format_idle_duration(resp['idle_seconds'])}",
            f"vars={len(resp.get('vars', {}))}",
        ]
        for resp in rows
    ]
    widths = [max(len(row[i]) for row in table) for i in range(len(table[0]))]
    lines: list[str] = []
    for resp, cols in zip(rows, table, strict=True):
        line = "  ".join(
            col if i == len(cols) - 1 else f"{col:<{widths[i]}}" for i, col in enumerate(cols)
        )
        if resp["state"] == "running":
            elapsed = resp.get("elapsed", 0.0)
            loc = f" at {resp['location']}" if resp.get("location") else ""
            line = f"{line}  ({elapsed:.1f}s){loc}"
        lines.append(line)
    return lines


def _emit_step_output(session_id: str, resp: dict[str, Any]) -> int:
    if stdout_delta := resp.get("stdout_delta"):
        sys.stdout.write(stdout_delta)
        sys.stdout.flush()
    if stderr_delta := resp.get("stderr_delta"):
        sys.stderr.write(stderr_delta)
        sys.stderr.flush()

    status = resp.get("status")
    if status == "running":
        print(
            _format_session_status(
                session_id,
                "running",
                elapsed=resp.get("elapsed", 0.0),
                location=resp.get("location"),
                hint=True,
            )
        )
        return 0
    if status == "busy":
        print(
            _format_session_status(
                session_id,
                "busy",
                elapsed=resp.get("elapsed", 0.0),
                location=resp.get("location"),
                hint=True,
            ),
            file=sys.stderr,
        )
        return 1
    if status == "error":
        if err := resp.get("error"):
            print(f"pysess: {err}", file=sys.stderr)
        return 1

    if (expr_repr := resp.get("expr_repr")) is not None:
        print(expr_repr)
    return 0


def _add_code_source_args(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group()
    group.add_argument("-c", "--code", dest="code", help="Python code string to execute.")
    group.add_argument("file", nargs="?", help="Path to a Python file to execute.")


def _add_exec_wait_args(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "-t",
        "--timeout",
        type=float,
        default=DEFAULT_EXEC_TIMEOUT,
        metavar="SEC",
        help=(
            f"Soft timeout in seconds (supports decimals, e.g. 0.2) before detaching "
            f"(default: {DEFAULT_EXEC_TIMEOUT:g})."
        ),
    )
    group.add_argument(
        "-d",
        "--detach",
        action="store_true",
        help="Run in the background and detach immediately without waiting.",
    )


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser for `pysess`."""
    parser = argparse.ArgumentParser(
        prog="pysess",
        description="Persistent Python REPL sessions for coding agents.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            '  pysess new -c "x = 40"         # start a session and run code\n'
            '  pysess exec <id> -c "x + 2"    # run in the same session (prints 42)\n'
            "  pysess exec <id> <<'EOF'       # run multiline code from stdin\n"
            "  pysess ps                      # list active sessions\n"
            "  pysess kill <id>               # stop session (or --all)"
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    new_parser = subparsers.add_parser(
        "new",
        help="Start a new session, optionally run code, and print session ID.",
    )
    new_parser.add_argument(
        "-n",
        "--name",
        metavar="NAME",
        help="Optional custom session name (default: random ID).",
    )
    _add_code_source_args(new_parser)
    _add_exec_wait_args(new_parser)

    exec_parser = subparsers.add_parser(
        "exec",
        help="Execute code in a session (reads stdin if no -c or FILE).",
    )
    exec_parser.add_argument("session_id", metavar="ID", help="Target session ID.")
    _add_code_source_args(exec_parser)
    _add_exec_wait_args(exec_parser)

    wait_parser = subparsers.add_parser(
        "wait",
        help="Wait for a running background step (use -t 0 to poll).",
    )
    wait_parser.add_argument("session_id", metavar="ID", help="Target session ID.")
    wait_parser.add_argument(
        "-t",
        "--timeout",
        type=float,
        default=DEFAULT_WAIT_TIMEOUT,
        metavar="SEC",
        help=(
            f"Wait timeout in seconds (supports decimals, or 0 to poll; "
            f"default: {DEFAULT_WAIT_TIMEOUT:g})."
        ),
    )

    cancel_parser = subparsers.add_parser(
        "cancel",
        help="Interrupt the running step with KeyboardInterrupt.",
    )
    cancel_parser.add_argument("session_id", metavar="ID", help="Target session ID.")

    ps_parser = subparsers.add_parser(
        "ps",
        help="List active sessions or inspect a session's state.",
    )
    ps_parser.add_argument("session_id", nargs="?", metavar="ID", help="Optional session ID.")

    kill_parser = subparsers.add_parser(
        "kill",
        help="Stop session(s) cleanly and remove socket files.",
    )
    kill_group = kill_parser.add_mutually_exclusive_group(required=True)
    kill_group.add_argument("session_id", nargs="?", metavar="ID", help="Target session ID.")
    kill_group.add_argument("--all", action="store_true", help="Stop all active sessions.")

    return parser


def main(argv: list[str] | None = None) -> None:
    """CLI entrypoint for `pysess`."""
    effective_argv = list(sys.argv[1:] if argv is None else argv)
    if len(effective_argv) == 3 and effective_argv[0] == "--daemon":
        daemon = SessionDaemon(effective_argv[1], Path(effective_argv[2]))
        raise SystemExit(daemon.run())

    parser = build_parser()
    if not effective_argv:
        parser.print_help()
        return
    args = parser.parse_args(effective_argv)
    try:
        if args.command == "new":
            code = _read_code(args.code, args.file, read_stdin_tty_safe=True)
            timeout = 0.0 if args.detach else args.timeout
            if args.name is not None:
                sid = args.name
                sock_path = socket_path(sid)
                if sock_path.exists():
                    try:
                        _send_request(sid, {"op": "ps"})
                    except RuntimeError:
                        with contextlib.suppress(OSError):
                            sock_path.unlink()
                    else:
                        raise RuntimeError(f"Session '{sid}' already exists.")
            else:
                sid = generate_session_id()
            _spawn_daemon(sid)
            print(f"[pysess: {sid}]", flush=True)
            rc = (
                _emit_step_output(
                    sid,
                    _send_request(sid, {"op": "exec", "code": code, "timeout": timeout}),
                )
                if code.strip()
                else 0
            )
        elif args.command == "exec":
            code = _read_code(args.code, args.file, read_stdin_tty_safe=False)
            timeout = 0.0 if args.detach else args.timeout
            resp = _send_request(
                args.session_id,
                {"op": "exec", "code": code, "timeout": timeout},
            )
            rc = _emit_step_output(args.session_id, resp)
        elif args.command == "wait":
            resp = _send_request(args.session_id, {"op": "wait", "timeout": args.timeout})
            rc = _emit_step_output(args.session_id, resp)
        elif args.command == "cancel":
            resp = _send_request(args.session_id, {"op": "cancel"})
            if resp.get("status") == "idle":
                print(_format_session_status(args.session_id, "idle"))
                rc = 0
            else:
                rc = _emit_step_output(args.session_id, resp)
                if resp.get("status") == "running":
                    rc = 1
        elif args.command == "ps":
            if args.session_id:
                resp = _send_request(args.session_id, {"op": "ps"})
                print(_format_ps_rows([resp])[0])
                if user_vars := resp.get("vars", {}):
                    print("vars:")
                    width = max(len(name) for name in user_vars)
                    for name, type_name in sorted(user_vars.items()):
                        print(f"  {name:<{width}}  {type_name}")
                else:
                    print("vars: (none)")
            else:
                rows: list[dict[str, Any]] = []
                for sock_file in sorted(runtime_dir().glob("*.sock")):
                    try:
                        rows.append(_send_request(sock_file.stem, {"op": "ps"}))
                    except RuntimeError:
                        with contextlib.suppress(OSError):
                            sock_file.unlink()
                        continue
                if rows:
                    for line in _format_ps_rows(rows):
                        print(line)
                else:
                    print("No active sessions.")
            rc = 0
        else:  # kill
            if args.all:
                killed_any = False
                for sock_file in sorted(runtime_dir().glob("*.sock")):
                    try:
                        _send_request(sock_file.stem, {"op": "kill"})
                    except RuntimeError:
                        with contextlib.suppress(OSError):
                            sock_file.unlink()
                        continue
                    killed_any = True
                    print(_format_session_status(sock_file.stem, "killed"))
                if not killed_any:
                    print("No active sessions.")
            else:
                _send_request(args.session_id, {"op": "kill"})
                print(_format_session_status(args.session_id, "killed"))
            rc = 0
    except (OSError, RuntimeError) as exc:
        print(f"pysess: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    if rc:
        raise SystemExit(rc)
