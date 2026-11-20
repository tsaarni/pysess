"""Tests for the `pysess.venv` Python API and the `pysess venv` CLI."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from pysess.cli import build_parser

pytestmark = pytest.mark.skipif(shutil.which("uv") is None, reason="requires the uv executable")


@pytest.fixture
def env(tmp_path: Path) -> Iterator[dict[str, str]]:
    # Socket paths must stay short, so the runtime dir is not placed under tmp_path.
    with tempfile.TemporaryDirectory(prefix="ps-") as runtime:
        environ = {k: v for k, v in os.environ.items() if k not in ("VIRTUAL_ENV", "PYSESS_UV")}
        environ["PYSESS_RUNTIME_DIR"] = runtime
        environ["UV_CACHE_DIR"] = str(tmp_path / "uv-cache")
        environ["UV_OFFLINE"] = "1"
        try:
            yield environ
        finally:
            _cli("kill", "--all", env=environ)


def _cli(*args: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "pysess", *args],
        text=True,
        capture_output=True,
        env=env,
        timeout=60,
        check=False,
    )


def _ok(*args: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    res = _cli(*args, env=env)
    assert res.returncode == 0, f"stdout={res.stdout!r} stderr={res.stderr!r}"
    return res


def _wait_gone(path: Path) -> None:
    deadline = time.monotonic() + 5.0
    while path.exists():
        assert time.monotonic() < deadline, f"{path} still exists"
        time.sleep(0.05)


def _wheel(directory: Path, name: str, files: dict[str, str]) -> str:
    """Build a minimal pure-Python wheel so tests install without network access."""
    dist_info = f"{name}-1.0.dist-info"
    contents = {
        **files,
        f"{dist_info}/METADATA": f"Metadata-Version: 2.1\nName: {name}\nVersion: 1.0\n",
        f"{dist_info}/WHEEL": (
            "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
        ),
    }
    contents[f"{dist_info}/RECORD"] = "".join(
        f"{p},,\n" for p in [*contents, f"{dist_info}/RECORD"]
    )
    path = directory / f"{name}-1.0-py3-none-any.whl"
    with zipfile.ZipFile(path, "w") as zf:
        for arcname, text in contents.items():
            zf.writestr(arcname, text)
    return str(path)


def _fake_uv(directory: Path, body: str) -> str:
    script = directory / "fake-uv"
    script.write_text(f"#!/bin/sh\n{body}\n")
    script.chmod(0o755)
    return str(script)


def test_venv_cli_parser() -> None:
    parser = build_parser()
    assert parser.parse_args(["venv", "ls"]).venv_command == "ls"
    rm = parser.parse_args(["venv", "rm", "a", "b"])
    assert (rm.venv_command, rm.names, rm.all) == ("rm", ["a", "b"], False)
    assert parser.parse_args(["venv", "rm", "--all"]).all is True
    with pytest.raises(SystemExit):
        parser.parse_args(["venv"])


def test_private_venv_install_at_runtime_and_cleanup_on_kill(
    env: dict[str, str], tmp_path: Path
) -> None:
    whl = _wheel(tmp_path, "alpha", {"alpha.py": "VALUE = 41\n"})
    _ok("new", "-n", "s1", "-c", "import pysess", env=env)
    assert "ModuleNotFoundError" in _cli("exec", "s1", "-c", "import alpha", env=env).stderr

    res = _ok("exec", "s1", "-t", "30", "-c", f"pysess.venv.install({whl!r})", env=env)
    assert "+ alpha==1.0" in res.stderr
    assert _ok("exec", "s1", "-c", "import alpha; alpha.VALUE + 1", env=env).stdout.strip() == "42"
    res = _ok("exec", "s1", "-c", "v = pysess.venv.current(); (v.name, v.private)", env=env)
    assert res.stdout.strip() == "('s1', True)"
    res = _ok("exec", "s1", "-c", "pysess.venv.current().packages()", env=env)
    assert res.stdout.strip() == "{'alpha': '1.0'}"

    assert "venv=private" in _ok("ps", env=env).stdout
    ls = _ok("venv", "ls", env=env).stdout
    assert ls.startswith("s1  private")
    assert "packages=1" in ls
    assert "orphaned" not in ls
    rm = _cli("venv", "rm", "s1", env=env)
    assert rm.returncode == 1
    assert "private venv of a live session" in rm.stderr

    root = Path(env["PYSESS_RUNTIME_DIR"]) / "sessions" / "s1"
    assert (root / "venv" / "pyvenv.cfg").exists()
    assert "[pysess: s1 | killed]" in _ok("kill", "s1", env=env).stdout
    _wait_gone(root)
    assert _ok("venv", "ls", env=env).stdout.strip() == "No venvs."


def test_shared_venv_across_sessions_and_recreate_after_removal(
    env: dict[str, str], tmp_path: Path
) -> None:
    whl = _wheel(tmp_path, "alpha", {"alpha.py": "VALUE = 1\n"})
    setup = f"import pysess\npysess.venv.use('shared')\npysess.venv.install({whl!r})"
    _ok("new", "-n", "s1", "-t", "30", "-c", setup, env=env)
    res = _ok(
        "new", "-n", "s2", "-c", "import pysess; pysess.venv.use('shared'); import alpha", env=env
    )
    assert res.stderr == ""
    assert _ok("ps", env=env).stdout.count("venv=shared") == 2

    shared = Path(env["PYSESS_RUNTIME_DIR"]) / "venvs" / "shared"
    assert "[pysess: venv shared | removed]" in _ok("venv", "rm", "shared", env=env).stdout
    assert not shared.exists()

    # install() into the removed venv recreates it.
    code = f"pysess.venv.install({whl!r})\nimport alpha\npysess.venv.current().name"
    assert _ok("exec", "s1", "-t", "30", "-c", code, env=env).stdout.strip() == "'shared'"
    _ok("kill", "--all", env=env)
    assert shared.exists(), "shared venvs outlive sessions"


def test_detach_and_switch_restore_path_and_environ(env: dict[str, str], tmp_path: Path) -> None:
    whl = _wheel(
        tmp_path,
        "gamma",
        {"gamma.py": "", "gamma_extra.pth": "gamma_extra\n", "gamma_extra/ext.py": ""},
    )
    code = (
        "import os, sys, pysess\n"
        "orig_path, orig_env = list(sys.path), os.environ['PATH']\n"
        f"pysess.venv.install({whl!r})\n"
        "venv_path = str(pysess.venv.current().path)\n"
        "import gamma, ext\n"
        "site_idx = next(i for i, p in enumerate(sys.path) if p.endswith('site-packages')"
        " and not p.startswith(venv_path))\n"
        "assert all(sys.path.index(p) < site_idx for p in sys.path if p.startswith(venv_path))\n"
        "assert os.environ['VIRTUAL_ENV'] == venv_path\n"
        "assert os.environ['PATH'] == venv_path + '/bin' + os.pathsep + orig_env\n"
        "os.environ['PATH'] += os.pathsep + '/user/added'\n"
    )
    _ok("new", "-n", "s1", "-t", "30", "-c", code, env=env)

    res = _ok(
        "exec",
        "s1",
        "-c",
        "pysess.venv.detach()\n"
        "(sys.path == orig_path, os.environ['PATH'] == orig_env + ':/user/added', "
        "'VIRTUAL_ENV' in os.environ, pysess.venv.current())",
        env=env,
    )
    assert res.stdout.strip() == "(True, True, False, None)"
    assert "modules imported from venv 's1' remain loaded: ext, gamma" in res.stderr
    gone = _cli("exec", "s1", "-c", "del sys.modules['ext']\nimport ext", env=env)
    assert "ModuleNotFoundError: No module named 'ext'" in gone.stderr

    assert _ok("exec", "s1", "-c", "pysess.venv.use().name", env=env).stdout.strip() == "'s1'"
    _ok("exec", "s1", "-c", "import ext", env=env)

    code = (
        "pysess.venv.use('other')\n"
        "(any(p.startswith(venv_path) for p in sys.path), os.environ['VIRTUAL_ENV'])"
    )
    other = Path(env["PYSESS_RUNTIME_DIR"]) / "venvs" / "other"
    assert _ok("exec", "s1", "-c", code, env=env).stdout.strip() == f"(False, {str(other)!r})"


def test_incompatible_and_orphaned_venvs(env: dict[str, str]) -> None:
    runtime = Path(env["PYSESS_RUNTIME_DIR"])
    old = runtime / "venvs" / "old"
    (old / "bin").mkdir(parents=True)
    (old / "bin" / "python").symlink_to("/bin/sh")
    (old / "pyvenv.cfg").write_text("version_info = 2.7.18\n")
    orphan = runtime / "sessions" / "zz99" / "venv"
    (orphan / "bin").mkdir(parents=True)
    (orphan / "bin" / "python").symlink_to(os.path.realpath(sys.executable))
    (orphan / "pyvenv.cfg").write_text("version_info = 3\n")

    res = _cli("new", "-n", "s1", "-c", "import pysess; pysess.venv.use('old')", env=env)
    assert res.returncode == 1
    assert "created for a different Python (2.7.18)" in res.stderr

    ls = _ok("venv", "ls", env=env).stdout.splitlines()
    assert any(line.startswith("old ") and line.endswith("incompatible") for line in ls)
    assert any(line.startswith("zz99 ") and line.endswith("orphaned") for line in ls)

    res = _ok("venv", "rm", "--all", env=env)
    assert "[pysess: venv old | removed]" in res.stdout
    assert "[pysess: venv zz99 | removed]" in res.stdout
    assert not old.exists()
    assert not orphan.parent.exists()


def test_api_errors(env: dict[str, str]) -> None:
    code = (
        "import os, pysess\n"
        "errors = []\n"
        "def attempt(call):\n"
        "    try:\n"
        "        call()\n"
        "    except pysess.Error as exc:\n"
        "        errors.append(str(exc))\n"
        "attempt(lambda: pysess.venv.use('../x'))\n"
        "attempt(lambda: pysess.venv.install())\n"
        "attempt(lambda: pysess.venv.install('--index-url=http://x'))\n"
        "attempt(lambda: pysess.venv.remove('nope'))\n"
        "os.environ['PATH'] = '/dev'\n"
        "attempt(lambda: pysess.venv.use('x'))\n"
        "errors"
    )
    res = _ok("new", "-c", code, env=env)
    assert "Invalid venv name: '../x'." in res.stdout
    assert "No packages given." in res.stdout
    assert "uv options are not accepted" in res.stdout
    assert "Venv 'nope' not found." in res.stdout
    assert "The `uv` executable was not found" in res.stdout

    outside = subprocess.run(
        [sys.executable, "-c", "import pysess; pysess.venv.install('x')"],
        text=True,
        capture_output=True,
        env=env,
        timeout=15,
        check=False,
    )
    assert outside.returncode == 1
    assert "only available inside a pysess session" in outside.stderr


def test_pysess_uv_override_and_upgrade_flag(env: dict[str, str], tmp_path: Path) -> None:
    log = tmp_path / "uv-args"
    fake = _fake_uv(tmp_path, f'echo "$@" > {log}')
    code = (
        "import os, pysess\n"
        "pysess.venv.use('x')\n"
        f"os.environ['PYSESS_UV'] = {fake!r}\n"
        "pysess.venv.install('alpha', upgrade=True)"
    )
    _ok("new", "-t", "30", "-c", code, env=env)
    args = log.read_text().split()
    assert args[:3] == ["--color", "never", "pip"]
    assert args[-3:] == ["--upgrade", "--", "alpha"]


def test_cancel_during_install(env: dict[str, str], tmp_path: Path) -> None:
    fake = _fake_uv(tmp_path, "echo fake-start\nexec sleep 30")
    setup = f"import os, pysess\npysess.venv.use('x')\nos.environ['PYSESS_UV'] = {fake!r}"
    _ok("new", "-n", "s1", "-t", "30", "-c", setup, env=env)

    res = _ok("exec", "s1", "-t", "1", "-c", "pysess.venv.install('alpha')", env=env)
    assert "fake-start" in res.stderr
    assert "[pysess: s1 | running" in res.stdout

    cancelled = _cli("cancel", "s1", env=env)
    assert "`uv pip install` interrupted; the venv may be partially updated" in cancelled.stderr
    assert "KeyboardInterrupt" in cancelled.stderr
    assert _ok("exec", "s1", "-c", "1 + 1", env=env).stdout.strip() == "2"
