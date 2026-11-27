"""Command-line interface and persistent session daemon for `pysess`."""

from __future__ import annotations

import argparse
import ast
import atexit
import builtins
import codecs
import contextlib
import fcntl
import io
import json
import os
import queue
import secrets
import signal
import socket
import string
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import types
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from pysess import venv
from pysess._runtime import (
    is_socket_alive,
    live_sessions,
    runtime_dir,
    send_request,
    socket_path,
    stream_request,
)

DEFAULT_EXEC_TIMEOUT = 10.0
DEFAULT_WAIT_TIMEOUT = 30.0
DEFAULT_IDLE_TIMEOUT = 3600.0
MAX_EXPR_REPR_BYTES = 32 * 1024
SESSION_ID_ALPHABET = string.ascii_lowercase + string.digits
SESSION_ID_LENGTH = 4


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
    """Thread-safe append-only REPL transcript backed by an unlinked temp file.

    The unlinked file in `runtime_dir` is freed automatically by the kernel when the daemon
    exits (even on `SIGKILL`). If `audit_path` is provided (via `PYSESS_LOG_DIR`), writes are
    also teed to that persistent file.
    """

    def __init__(self, directory: Path, audit_path: Path | None = None) -> None:
        self._tmp = tempfile.TemporaryFile(dir=directory)  # noqa: SIM115 - lives with daemon
        self._tmp_fd = self._tmp.fileno()
        self._audit_file: io.TextIOWrapper | None = None
        if audit_path is not None:
            fd = os.open(audit_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            self._audit_file = os.fdopen(fd, "w", encoding="utf-8")
        self._cond = threading.Condition()
        self._size = 0
        self._at_bol = True
        self._finished = False

    def write(self, text: str, *, ensure_bol: bool = False) -> None:
        if not text:
            return
        with self._cond:
            if self._finished:
                return
            if ensure_bol and not self._at_bol:
                text = "\n" + text
            data = text.encode("utf-8")
            self._tmp.write(data)
            self._tmp.flush()
            self._size += len(data)
            if self._audit_file is not None and not self._audit_file.closed:
                self._audit_file.write(text)
                self._audit_file.flush()
            self._at_bol = text.endswith("\n")
            self._cond.notify_all()

    def tail_offset(self, lines: int | None) -> int:
        """Return the byte offset for the last `lines` lines (or 0 if `lines` is `None`)."""
        with self._cond:
            size = self._size
        if lines is None:
            return 0
        if lines <= 0 or size == 0:
            return size
        # Ignore a single trailing newline so `--tail 1` returns the last non-empty line.
        pos = size - 1 if os.pread(self._tmp_fd, 1, size - 1) == b"\n" else size
        found = 0
        while pos > 0:
            start = max(0, pos - 65536)
            chunk = os.pread(self._tmp_fd, pos - start, start)
            idx = len(chunk)
            while (idx := chunk.rfind(b"\n", 0, idx)) != -1:
                found += 1
                if found == lines:
                    return start + idx + 1
            pos = start
        return 0

    def read_from(self, offset: int) -> tuple[bytes, int]:
        """Return all bytes written since `offset` along with the new offset."""
        with self._cond:
            size = self._size
        chunks: list[bytes] = []
        pos = offset
        while pos < size:
            chunk = os.pread(self._tmp_fd, min(65536, size - pos), pos)
            if not chunk:
                break
            pos += len(chunk)
            chunks.append(chunk)
        return b"".join(chunks), pos

    def wait_for_data(self, offset: int, timeout: float) -> bool:
        """Wait until new bytes appear past `offset`, the log finishes, or `timeout` elapses.

        Returns `True` if the log is finished and no unread bytes remain.
        """
        with self._cond:
            if self._size > offset:
                return False
            if self._finished:
                return True
            self._cond.wait(timeout=timeout)
            return self._finished and self._size <= offset

    def finish(self) -> None:
        """Mark the transcript finished, close the audit file, and wake all followers."""
        with self._cond:
            self._finished = True
            # Keep `self._tmp` open until process exit so in-flight readers never hit EBADF.
            if self._audit_file is not None:
                with contextlib.suppress(OSError):
                    self._audit_file.close()
            self._cond.notify_all()


def _open_session_log(session_id: str, directory: Path) -> _SessionLog:
    """Open the session's ephemeral transcript, plus an audit file if `PYSESS_LOG_DIR` is set."""
    audit_path: Path | None = None
    if raw := os.environ.get("PYSESS_LOG_DIR", "").strip():
        base = Path(raw)
        base.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")  # local time
        audit_path = base / f"{stamp}-{session_id}.log"
    return _SessionLog(directory, audit_path=audit_path)


def _redirect_fd_to_unlinked_file(fd: int, directory: Path) -> None:
    """Point file descriptor `fd` at a new, already-deleted append-only file in `directory`.

    Python's `sys.stdout`/`sys.stderr`, child processes inheriting the descriptor, and C code
    then all write to the same open file, so their output keeps its order and nothing written
    after a step finished is lost. The kernel frees the file when the session exits.
    """
    with tempfile.TemporaryFile(dir=directory) as f:
        os.dup2(f.fileno(), fd)
    fcntl.fcntl(fd, fcntl.F_SETFL, fcntl.fcntl(fd, fcntl.F_GETFL) | os.O_APPEND)


class _OutputReader:
    """Thread-safe cursor that returns text appended to file descriptor `fd` since the last read."""

    def __init__(self, fd: int) -> None:
        self._fd = fd
        self._offset = 0
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._lock = threading.Lock()

    def read(self) -> str:
        with self._lock:
            chunks: list[bytes] = []
            while chunk := os.pread(self._fd, 65536, self._offset):
                self._offset += len(chunk)
                chunks.append(chunk)
            return self._decoder.decode(b"".join(chunks))


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


class SessionDaemon:
    """Main-thread executor + control-thread Unix socket server."""

    def __init__(self, session_id: str, sock_path: Path) -> None:
        self.session_id = session_id
        self.sock_path = sock_path
        self.idle_timeout = idle_timeout_seconds()

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
            if is_socket_alive(self.sock_path):
                self._server_sock.close()
                raise RuntimeError(f"Session '{session_id}' is already active.")
            self.sock_path.unlink()
        try:
            self._server_sock.bind(str(self.sock_path))
        except OSError as exc:
            self._server_sock.close()
            raise RuntimeError(f"Failed to bind Unix socket '{self.sock_path}': {exc}") from exc
        self.sock_path.chmod(0o600)
        self._server_sock.listen(16)
        self._server_sock.settimeout(0.5)
        self.session_log = _open_session_log(session_id, self.sock_path.parent)
        venv._session_started(session_id)

        # File descriptors 1 and 2 are inherited by child processes. Point them at files
        # that responses and the session log read back. `sys.stdout`/`sys.stderr` write to
        # them without buffering because the daemon runs with `python -u`.
        for fd in (1, 2):
            _redirect_fd_to_unlinked_file(fd, self.sock_path.parent)
        for stream in (sys.stdout, sys.stderr):
            if isinstance(stream, io.TextIOWrapper):
                stream.reconfigure(encoding="utf-8", errors="backslashreplace")
        self._real_stdout = sys.stdout
        self._real_stderr = sys.stderr
        self._stdout = _OutputReader(1)
        self._stderr = _OutputReader(2)
        self._log_stdout = _OutputReader(1)
        self._log_stderr = _OutputReader(2)

    def _output_delta(self) -> dict[str, str]:
        """Return output not yet delivered to a client, including output of earlier steps."""
        return {"stdout_delta": self._stdout.read(), "stderr_delta": self._stderr.read()}

    def _log_output(self) -> None:
        self.session_log.write(self._log_stdout.read())
        self.session_log.write(self._log_stderr.read())

    def _cleanup(self) -> None:
        self._log_output()
        self.session_log.finish()
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
                if req.get("op") == "logs":
                    self._handle_logs(conn, req)
                    return
                resp, job_to_notify = self._dispatch(req)
                kill_after_reply = req.get("op") == "kill" and resp.get("status") == "killed"
            except Exception as exc:  # noqa: BLE001 - report any request failure to the client
                resp = {"status": "error", "error": str(exc)}
            with contextlib.suppress(OSError):
                conn.sendall((json.dumps(resp) + "\n").encode("utf-8"))
        if job_to_notify is not None:
            job_to_notify.delivered_event.set()
        if kill_after_reply:
            self._initiate_kill()

    def _handle_logs(self, conn: socket.socket, req: dict[str, Any]) -> None:
        follow = bool(req.get("follow", False))
        raw_tail = req.get("tail")
        tail = int(raw_tail) if raw_tail is not None else None
        self._log_output()
        offset = self.session_log.tail_offset(tail)
        with contextlib.suppress(OSError):
            conn.sendall(b'{"status": "ok"}\n')
            while True:
                data, offset = self.session_log.read_from(offset)
                if data:
                    conn.sendall(data)
                if not follow:
                    break
                if self.session_log.wait_for_data(offset, timeout=0.1):
                    break
                self._log_output()

    def _job_response(self, job: _Job) -> tuple[dict[str, Any], _Job | None]:
        if not job.done_event.is_set():
            return (
                {
                    "status": "running",
                    "elapsed": job.elapsed,
                    "location": _main_thread_location(),
                    **self._output_delta(),
                },
                None,
            )
        job.result_delivered = True
        return (
            {
                "status": job.status,
                "elapsed": job.elapsed,
                **self._output_delta(),
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
                            **self._output_delta(),
                        },
                        None,
                    )
                # Output not yet delivered (of earlier steps, or written by child processes
                # and threads after they finished) is returned with this step's output.
                job = _Job(code=str(req.get("code", "")))
                self.current_job = job
                self.work_queue.put(job)

            job.done_event.wait(timeout=max(0.0, timeout))
            with self.state_lock:
                return self._job_response(job)

        if op == "wait":
            timeout = float(req.get("timeout", DEFAULT_WAIT_TIMEOUT))
            with self.state_lock:
                job = self.current_job
            if job is not None and not job.done_event.is_set() and timeout > 0:
                job.done_event.wait(timeout=timeout)
            with self.state_lock:
                if job is None or (job.done_event.is_set() and job.result_delivered):
                    return ({"status": "idle", **self._output_delta()}, None)
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
                        "venv": venv._ps_info(),
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
        venv._session_ended(self.session_id)
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
                self._log_output()  # written by child processes or threads since last step
                ts = datetime.now().astimezone().isoformat(timespec="seconds")
                self.session_log.write(
                    f"# pysess {ts}\n{_format_repl_cell(job.code)}",
                    ensure_bol=True,
                )
                try:
                    self._in_user_code = True
                    if job.cancel_requested:
                        raise KeyboardInterrupt
                    job.expr_repr = _execute_cell(job.code, self.namespace)
                    self._in_user_code = False
                    job.status = "ok"
                except BaseException as exc:  # noqa: BLE001 - user code may raise anything
                    self._in_user_code = False
                    if isinstance(exc, SystemExit):
                        if exc.code in (None, 0):
                            job.status = "ok"
                        elif isinstance(exc.code, int):
                            self._real_stderr.write(_format_repl_traceback(exc))
                            job.status = "error"
                        else:
                            self._real_stderr.write(f"{exc.code}\n")
                            job.status = "error"
                        exited_via_sys_exit = True
                        self.shutdown_event.set()
                    else:
                        self._real_stderr.write(_format_repl_traceback(exc))
                        job.status = "error"
                finally:
                    self._in_user_code = False
                    # Undo reassignments of `sys.stdout`/`sys.stderr` by the step.
                    sys.stdout, sys.stderr = self._real_stdout, self._real_stderr
                    self._log_output()
                    if job.expr_repr is not None:
                        self.session_log.write(f"{job.expr_repr}\n", ensure_bol=True)
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
            # After `_cleanup()` so `kill` is answered without waiting for the deletion.
            venv._session_ended(self.session_id)
        return 0


def _spawn_daemon(session_id: str) -> None:
    sock_path = socket_path(session_id)
    cmd = [sys.executable, "-u", "-m", "pysess", "--daemon", session_id, str(sock_path)]
    devnull = Path(os.devnull)
    with devnull.open("rb") as devnull_in, devnull.open("wb") as devnull_out:
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
                if send_request(session_id, {"op": "ps"}).get("status") == "ok":
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


def _use_color() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    return bool(os.environ.get("FORCE_COLOR")) or sys.stdout.isatty()


class _LogColorizer:
    """Incremental ANSI colorizer for streamed REPL transcript bytes."""

    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._buf = ""
        self._at_bol = True
        self._in_traceback = False

    def _color_line(self, line: str) -> str:
        body = line.rstrip("\r\n")
        eol = line[len(body) :]
        if body.startswith("# pysess "):
            self._in_traceback = False
            return f"\033[2m{body}\033[0m{eol}"
        if body in (">>>", "...") or body.startswith((">>> ", "... ")):
            self._in_traceback = False
            rest = f"\033[1m{body[3:]}\033[0m" if len(body) > 3 else ""
            return f"\033[1;36m{body[:3]}\033[0m{rest}{eol}"
        if body == "Traceback (most recent call last):":
            self._in_traceback = True
            return f"\033[31m{body}\033[0m{eol}"
        if self._in_traceback:
            if not body or body.startswith("  "):
                return f"\033[31m{body}\033[0m{eol}" if body else line
            self._in_traceback = False
            return f"\033[1;31m{body}\033[0m{eol}"
        return line

    def feed(self, chunk: bytes) -> str:
        text = self._buf + self._decoder.decode(chunk)
        self._buf = ""
        out: list[str] = []
        for piece in text.splitlines(keepends=True):
            if piece.endswith(("\n", "\r")):
                out.append(self._color_line(piece) if self._at_bol else piece)
                self._at_bol = True
            elif not self._at_bol:
                out.append(piece)
            elif self._in_traceback or piece[0] in "#>.T":
                self._buf = piece
            else:
                out.append(piece)
                self._at_bol = False
        return "".join(out)

    def flush(self) -> str:
        tail = self._buf + self._decoder.decode(b"", final=True)
        self._buf = ""
        return self._color_line(tail) if tail and self._at_bol else tail


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


def _format_ps_venv(info: dict[str, Any] | None) -> str:
    if not info:
        return "venv=-"
    return "venv=private" if info.get("private") else f"venv={info['name']}"


def _format_ps_rows(rows: list[dict[str, Any]]) -> list[str]:
    table = [
        [
            resp["id"],
            f"pid={resp['pid']}",
            f"state={resp['state']}",
            f"idle={_format_idle_duration(resp['idle_seconds'])}",
            _format_ps_venv(resp.get("venv")),
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


def _non_negative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be >= 0")
    return parsed


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
            "  pysess logs <id> -f            # follow session transcript live\n"
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

    logs_parser = subparsers.add_parser(
        "logs",
        help="Show or follow a session's REPL transcript.",
    )
    logs_parser.add_argument("session_id", metavar="ID", help="Target session ID.")
    logs_parser.add_argument(
        "-f",
        "--follow",
        action="store_true",
        help="Stream new transcript output live until the session exits or Ctrl-C.",
    )
    logs_parser.add_argument(
        "--tail",
        type=_non_negative_int,
        default=None,
        metavar="N",
        help="Lines of recent transcript to display (default: all).",
    )

    kill_parser = subparsers.add_parser(
        "kill",
        help="Stop session(s) cleanly and remove socket files.",
    )
    kill_group = kill_parser.add_mutually_exclusive_group(required=True)
    kill_group.add_argument("session_id", nargs="?", metavar="ID", help="Target session ID.")
    kill_group.add_argument("--all", action="store_true", help="Stop all active sessions.")

    venv_parser = subparsers.add_parser(
        "venv",
        help="List or remove venvs created with the `pysess.venv` Python API.",
        description=(
            "Venvs are created and populated from Python inside a session, e.g. "
            "`import pysess; pysess.venv.install('httpx')`. See `help(pysess.venv)`."
        ),
    )
    venv_sub = venv_parser.add_subparsers(dest="venv_command", required=True)
    venv_sub.add_parser("ls", help="List shared and private venvs.")
    rm_parser = venv_sub.add_parser(
        "rm",
        help="Remove shared venvs, or private venvs left behind by dead sessions.",
    )
    rm_parser.add_argument("names", nargs="*", metavar="NAME", help="Venv name(s) to remove.")
    rm_parser.add_argument(
        "--all",
        action="store_true",
        help="Remove all shared venvs and all orphaned private venvs.",
    )

    return parser


def _format_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "K", "M"):
        if size < 1024:
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}G"


def _cmd_venv(args: argparse.Namespace) -> int:
    items = venv.list()

    if args.venv_command == "ls":
        if not items:
            print("No venvs.")
            return 0
        table = [
            [
                item.name,
                "private" if item.private else "shared",
                f"python={item.python_version or '?'}",
                f"size={_format_size(item.size())}",
                f"packages={len(item.packages())}",
                "incompatible" if not item.compatible else "orphaned" if item.orphaned else "",
            ]
            for item in items
        ]
        widths = [max(len(row[i]) for row in table) for i in range(len(table[0]))]
        for row in table:
            print("  ".join(f"{col:<{widths[i]}}" for i, col in enumerate(row)).rstrip())
        return 0

    # rm
    if bool(args.names) == args.all:
        print("pysess: venv rm: give NAME(s) or --all.", file=sys.stderr)
        return 2
    targets: list[str | venv.Venv] = (
        [item for item in items if not item.private or item.orphaned] if args.all else args.names
    )
    if not targets:
        print("No venvs to remove.")
    rc = 0
    for target in targets:
        try:
            venv.remove(target)
        except RuntimeError as exc:
            print(f"pysess: {exc}", file=sys.stderr)
            rc = 1
            continue
        print(f"[pysess: venv {target if isinstance(target, str) else target.name} | removed]")
    return rc


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
                        send_request(sid, {"op": "ps"})
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
                    send_request(sid, {"op": "exec", "code": code, "timeout": timeout}),
                )
                if code.strip()
                else 0
            )
        elif args.command == "exec":
            code = _read_code(args.code, args.file, read_stdin_tty_safe=False)
            timeout = 0.0 if args.detach else args.timeout
            resp = send_request(
                args.session_id,
                {"op": "exec", "code": code, "timeout": timeout},
            )
            rc = _emit_step_output(args.session_id, resp)
        elif args.command == "wait":
            resp = send_request(args.session_id, {"op": "wait", "timeout": args.timeout})
            rc = _emit_step_output(args.session_id, resp)
        elif args.command == "cancel":
            resp = send_request(args.session_id, {"op": "cancel"})
            if resp.get("status") == "idle":
                print(_format_session_status(args.session_id, "idle"))
                rc = 0
            else:
                rc = _emit_step_output(args.session_id, resp)
                if resp.get("status") == "running":
                    rc = 1
        elif args.command == "ps":
            if args.session_id:
                resp = send_request(args.session_id, {"op": "ps"})
                print(_format_ps_rows([resp])[0])
                if user_vars := resp.get("vars", {}):
                    print("vars:")
                    width = max(len(name) for name in user_vars)
                    for name, type_name in sorted(user_vars.items()):
                        print(f"  {name:<{width}}  {type_name}")
                else:
                    print("vars: (none)")
            else:
                rows = list(live_sessions())
                if rows:
                    for line in _format_ps_rows(rows):
                        print(line)
                else:
                    print("No active sessions.")
            rc = 0
        elif args.command == "logs":
            payload: dict[str, Any] = {"op": "logs", "follow": args.follow}
            if args.tail is not None:
                payload["tail"] = args.tail
            colorizer = _LogColorizer() if _use_color() else None
            try:
                for chunk in stream_request(args.session_id, payload):
                    if colorizer is not None:
                        if rendered := colorizer.feed(chunk):
                            sys.stdout.write(rendered)
                            sys.stdout.flush()
                    else:
                        sys.stdout.buffer.write(chunk)
                        sys.stdout.buffer.flush()
                if colorizer is not None and (tail := colorizer.flush()):
                    sys.stdout.write(tail)
                    sys.stdout.flush()
            except KeyboardInterrupt:
                pass
            rc = 0
        elif args.command == "venv":
            rc = _cmd_venv(args)
        else:  # kill
            if args.all:
                killed_any = False
                for sock_file in sorted(runtime_dir().glob("*.sock")):
                    try:
                        send_request(sock_file.stem, {"op": "kill"})
                    except RuntimeError:
                        with contextlib.suppress(OSError):
                            sock_file.unlink()
                        continue
                    killed_any = True
                    print(_format_session_status(sock_file.stem, "killed"))
                if not killed_any:
                    print("No active sessions.")
            else:
                send_request(args.session_id, {"op": "kill"})
                print(_format_session_status(args.session_id, "killed"))
            rc = 0
    except (OSError, RuntimeError) as exc:
        print(f"pysess: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    if rc:
        raise SystemExit(rc)
