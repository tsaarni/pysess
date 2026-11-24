"""Ephemeral virtual environments for pysess sessions.

Packages are installed with `uv` into virtual environments under the pysess runtime
directory and attached to the running interpreter by adding their `site-packages` to
`sys.path`. Nothing is written to user-wide site-packages.

- Private venv (`use()`, or the first `install()`): owned by one session and deleted when
  the session ends.
- Shared venv (`use(name)`): usable by any session and kept until `remove(name)` /
  `pysess venv rm NAME`, or until the OS clears `/tmp` (typically at boot).

Example inside a session:

    import pysess
    pysess.venv.install("httpx")   # private venv of this session
    import httpx

    pysess.venv.use("ml")          # attach (or create) shared venv "ml"
    pysess.venv.install("numpy")

Limitations:
- Installing into or removing a shared venv affects every session using it.
- Modules already imported from a venv stay loaded after `detach()` or switching venvs.
- `sys.executable` remains the pysess interpreter. The venv's `bin/` is prepended to
  `PATH` and `VIRTUAL_ENV` is set, so subprocesses running `python` use the venv.
- Requires the `uv` executable on `PATH` (or set `PYSESS_UV`).
"""

from __future__ import annotations

import builtins
import contextlib
import importlib
import os
import platform
import shutil
import site
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pysess import _runtime
from pysess._runtime import Error

# `list` is left out so `from pysess.venv import *` does not shadow the builtin.
__all__ = ["Venv", "current", "detach", "install", "remove", "use"]


@dataclass(frozen=True)
class Venv:
    """A pysess virtual environment.

    `name` is the shared venv name, or the owning session ID for a private venv.
    """

    name: str
    private: bool
    path: Path

    @property
    def python(self) -> Path:
        """Path of the venv's `python` executable."""
        return self.path / "bin" / "python"

    @property
    def exists(self) -> bool:
        return (self.path / "pyvenv.cfg").exists()

    @property
    def python_version(self) -> str | None:
        """Python version recorded in `pyvenv.cfg`."""
        with contextlib.suppress(OSError):
            for line in (self.path / "pyvenv.cfg").read_text(encoding="utf-8").splitlines():
                key, _, value = line.partition("=")
                if key.strip() == "version_info":
                    return value.strip()
        return None

    @property
    def compatible(self) -> bool:
        """Whether the venv was created for the interpreter running this process."""
        return os.path.realpath(self.python) == os.path.realpath(sys.executable)

    @property
    def orphaned(self) -> bool:
        """Whether this is a private venv whose session is no longer running."""
        return self.private and not _runtime.is_socket_alive(_runtime.socket_path(self.name))

    @property
    def site_packages(self) -> Path | None:
        return next(iter(sorted(self.path.glob("lib/python*/site-packages"))), None)

    def packages(self) -> dict[str, str]:
        """Return installed distributions as `{name: version}`."""
        import importlib.metadata  # deferred: keeps `import pysess` (and every CLI call) fast

        if (site_dir := self.site_packages) is None:
            return {}
        dists = importlib.metadata.distributions(path=[str(site_dir)])
        return dict(sorted((d.metadata["Name"], d.version) for d in dists if d.metadata["Name"]))

    def size(self) -> int:
        """Return the total size of the venv's files in bytes."""
        return sum((root / f).lstat().st_size for root, _, files in self.path.walk() for f in files)


@dataclass
class _Attachment:
    venv: Venv
    paths: builtins.list[str]  # `sys.path` entries added for the venv
    saved_virtual_env: str | None


_attached: _Attachment | None = None


def current() -> Venv | None:
    """Return the venv attached to this interpreter, or `None`."""
    return _attached.venv if _attached is not None else None


def use(name: str | None = None) -> Venv:
    """Attach shared venv `name`, or this session's private venv if `name` is `None`.

    The venv is created if needed. Any other attached venv is detached first.
    """
    if name is None:
        if (sid := _runtime.current_session_id) is None:
            raise Error("Private venvs are only available inside a pysess session; use use(name).")
        venv = Venv(sid, True, _runtime.runtime_dir() / "sessions" / sid / "venv")
    else:
        _runtime.validate_name(name, "venv name")
        venv = Venv(name, False, _runtime.runtime_dir() / "venvs" / name)

    if current() == venv and venv.exists:
        return venv
    if not venv.exists:
        venv.path.parent.mkdir(parents=True, exist_ok=True)
        # `--allow-existing` tolerates a concurrent or interrupted creation of the same venv.
        _run_uv("venv", "--quiet", "--allow-existing", "--python", sys.executable, str(venv.path))
    elif not venv.compatible:
        raise Error(
            f"Venv '{venv.name}' was created for a different Python ({venv.python_version}) "
            f"than this interpreter ({platform.python_version()} at {sys.executable}); "
            f"remove it with `pysess venv rm {venv.name}`."
        )

    detach()
    global _attached  # noqa: PLW0603 - process-wide attachment state
    _attached = _Attachment(venv, _add_paths(venv), os.environ.get("VIRTUAL_ENV"))
    os.environ["PATH"] = os.pathsep.join([str(venv.path / "bin"), *_path_entries()])
    os.environ["VIRTUAL_ENV"] = str(venv.path)
    return venv


def detach() -> None:
    """Detach the attached venv (if any): undo its `sys.path`, `PATH`, `VIRTUAL_ENV` changes."""
    global _attached  # noqa: PLW0603 - process-wide attachment state
    if (attachment := _attached) is None:
        return
    _attached = None
    _remove_paths(attachment.paths)
    bin_dir = str(attachment.venv.path / "bin")
    os.environ["PATH"] = os.pathsep.join(p for p in _path_entries() if p != bin_dir)
    if os.environ.get("VIRTUAL_ENV") == str(attachment.venv.path):
        if attachment.saved_virtual_env is None:
            del os.environ["VIRTUAL_ENV"]
        else:
            os.environ["VIRTUAL_ENV"] = attachment.saved_virtual_env

    prefix = str(attachment.venv.path) + os.sep
    loaded = sorted(
        name
        for name, module in builtins.list(sys.modules.items())
        if "." not in name and (getattr(module, "__file__", None) or "").startswith(prefix)
    )
    if loaded:
        sys.stderr.write(
            f"pysess: modules imported from venv '{attachment.venv.name}' remain loaded: "
            f"{', '.join(loaded)}\n"
        )


def install(*packages: str, upgrade: bool = False) -> None:
    """Install `packages` into the attached venv with `uv pip install`.

    If no venv is attached, the session's private venv is created and attached first.
    `packages` are requirement specifiers or local paths, e.g. `"httpx>=0.28"`.
    """
    if not packages:
        raise Error("No packages given.")
    for pkg in packages:
        if not isinstance(pkg, str) or not pkg or pkg.startswith("-"):
            raise Error(f"Invalid package: {pkg!r} (uv options are not accepted).")
    if _attached is None or not _attached.venv.exists:
        use(_attached.venv.name if _attached and not _attached.venv.private else None)
    assert _attached is not None
    venv = _attached.venv
    _run_uv(
        "pip",
        "install",
        "--python",
        str(venv.python),
        *(["--upgrade"] if upgrade else []),
        "--",
        *packages,
    )
    # Re-add paths so `.pth` files of newly installed packages are processed.
    _remove_paths(_attached.paths)
    _attached.paths = _add_paths(venv)


def list() -> builtins.list[Venv]:  # noqa: A001 - public API name, excluded from __all__
    """Return all shared venvs and private session venvs."""
    base = _runtime.runtime_dir()
    shared = [Venv(p.parent.name, False, p.parent) for p in (base / "venvs").glob("*/pyvenv.cfg")]
    private = [
        Venv(p.parent.parent.name, True, p.parent)
        for p in (base / "sessions").glob("*/venv/pyvenv.cfg")
    ]
    return sorted(shared, key=lambda v: v.name) + sorted(private, key=lambda v: v.name)


def remove(venv: str | Venv) -> None:
    """Delete a shared venv, or the private venv left behind by a dead session.

    `venv` is a `Venv` (e.g. from `list()`) or a name: a shared venv name, or else the
    session ID of a private venv. Sessions using a removed shared venv keep running, but
    later imports from it fail.
    """
    if isinstance(venv, str):
        name = _runtime.validate_name(venv, "venv name")
        candidates = [
            Venv(name, False, _runtime.runtime_dir() / "venvs" / name),
            Venv(name, True, _runtime.runtime_dir() / "sessions" / name / "venv"),
        ]
        venv = next((v for v in candidates if v.path.exists()), candidates[0])
    if not venv.path.exists():
        raise Error(f"Venv '{venv.name}' not found.")
    if venv.private and not venv.orphaned:
        raise Error(f"Venv '{venv.name}' is the private venv of a live session; kill it.")
    if current() == venv:
        detach()
    shutil.rmtree(venv.path.parent if venv.private else venv.path)


# Internal helpers ------------------------------------------------------------------------


def _path_entries() -> builtins.list[str]:
    return [p for p in os.environ.get("PATH", "").split(os.pathsep) if p]


def _add_paths(venv: Venv) -> builtins.list[str]:
    """Add the venv's site-packages (and its `.pth` entries) to `sys.path`, return them.

    They are placed before the interpreter's own site-packages so the venv takes precedence.
    """
    if (site_dir := venv.site_packages) is None:
        raise Error(f"Venv '{venv.name}' has no site-packages directory.")
    before = set(sys.path)
    site.addsitedir(str(site_dir))
    added = [p for p in dict.fromkeys(sys.path) if p not in before]
    _remove_paths(added)
    own_site = {os.path.realpath(p) for p in [*site.getsitepackages(), site.getusersitepackages()]}
    index = next(
        (i for i, p in enumerate(sys.path) if p and os.path.realpath(p) in own_site),
        len(sys.path),
    )
    sys.path[index:index] = added
    importlib.invalidate_caches()
    return added


def _remove_paths(paths: builtins.list[str]) -> None:
    drop = set(paths)
    sys.path[:] = [p for p in sys.path if p not in drop]
    importlib.invalidate_caches()


def _run_uv(*args: str) -> None:
    """Run uv with its output written to the session's stderr (file descriptor 2)."""
    if (exe := shutil.which(os.environ.get("PYSESS_UV") or "uv")) is None:
        raise Error("The `uv` executable was not found (install uv or set PYSESS_UV).")
    try:
        # `subprocess.run()` kills uv if the step is interrupted.
        proc = subprocess.run(
            [exe, "--color", "never", *args], stdin=subprocess.DEVNULL, stdout=2, check=False
        )
    except BaseException:
        sys.stderr.write(
            f"pysess: `uv {args[0]} {args[1]}` interrupted; the venv may be partially "
            "updated. Run the same call again to complete it.\n"
        )
        raise
    if proc.returncode != 0:
        raise Error(f"`uv {args[0]} {args[1]}` failed with exit code {proc.returncode}.")


# Hooks used by the session daemon ----------------------------------------------------------


def _session_started(session_id: str) -> None:
    _runtime.current_session_id = session_id
    _session_ended(session_id)  # drop a private venv left behind by a crashed namesake


def _session_ended(session_id: str) -> None:
    with contextlib.suppress(OSError, Error):
        shutil.rmtree(_runtime.runtime_dir() / "sessions" / session_id, ignore_errors=True)


def _ps_info() -> dict[str, Any] | None:
    if (venv := current()) is None:
        return None
    return {"name": venv.name, "private": venv.private}
