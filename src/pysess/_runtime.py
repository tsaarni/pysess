"""Runtime directory, session socket, and error helpers shared by the CLI and the Python API."""

from __future__ import annotations

import contextlib
import json
import os
import re
import socket
import stat
from collections.abc import Iterator
from pathlib import Path
from typing import Any

_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,31}$")

# ID of the session this process serves, set by the session daemon at startup.
# `None` when `pysess` is imported in a regular Python process.
current_session_id: str | None = None


class Error(RuntimeError):
    """Error raised by the `pysess` Python API."""


def validate_name(name: str, kind: str) -> str:
    """Return `name` if it is a valid session ID or venv name, else raise `Error`."""
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise Error(f"Invalid {kind}: {name!r}.")
    return name


def runtime_dir() -> Path:
    """Return the per-user runtime directory for session sockets and venvs.

    Defaults to `/tmp/pysess-<uid>` (like tmux's `/tmp/tmux-<uid>`) rather than
    `$XDG_RUNTIME_DIR`, so a sandboxed agent needs no access to the directory that holds
    the user's keyring, D-Bus, and audio sockets. `/tmp` is hard-coded instead of using
    `$TMPDIR` so that a sandbox and the user's terminal resolve the same directory.
    """
    if custom := os.environ.get("PYSESS_RUNTIME_DIR"):
        base = Path(custom)
    else:
        base = Path("/tmp") / f"pysess-{os.getuid()}"
    base.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        base.chmod(0o700)
    st = base.lstat()
    if not stat.S_ISDIR(st.st_mode):
        raise Error(f"Runtime path '{base}' is not a directory.")
    if st.st_uid != os.getuid():
        raise Error(f"Runtime directory '{base}' is not owned by the current user.")
    if stat.S_IMODE(st.st_mode) & 0o077:
        raise Error(f"Runtime directory '{base}' has insecure permissions.")
    return base


def socket_path(session_id: str) -> Path:
    """Return the validated Unix domain socket path for `session_id`."""
    return runtime_dir() / f"{validate_name(session_id, 'session ID')}.sock"


def is_socket_alive(sock_path: Path) -> bool:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.2)
            probe.connect(str(sock_path))
            return True
    except OSError:
        return False


def send_request(session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Send one JSON request to session `session_id` and return its JSON response."""
    sock_path = socket_path(session_id)
    if not sock_path.exists():
        raise Error(f"Session '{session_id}' not found.")
    req_timeout = max(0.0, float(payload.get("timeout", 0.0)))
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(req_timeout + 5.0)
            sock.connect(str(sock_path))
            sock.sendall((json.dumps(payload) + "\n").encode("utf-8"))
            with sock.makefile("r", encoding="utf-8") as reader:
                line = reader.readline()
    except OSError as exc:
        raise Error(f"Session '{session_id}' is unreachable ({exc}).") from exc

    if not line:
        raise Error(f"Session '{session_id}' closed connection without a response.")
    return json.loads(line)


def live_sessions() -> Iterator[dict[str, Any]]:
    """Yield the `ps` response of every reachable session.

    Socket files nobody listens on are removed. Sessions that accept connections but do
    not answer in time (e.g. user code holding the GIL) are skipped but keep their socket.
    """
    for sock_file in sorted(runtime_dir().glob("*.sock")):
        try:
            yield send_request(sock_file.stem, {"op": "ps"})
        except Error:
            if not is_socket_alive(sock_file):
                with contextlib.suppress(OSError):
                    sock_file.unlink()
