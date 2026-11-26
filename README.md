<p align="center">
  <img src="assets/logo-light.svg#gh-light-mode-only" alt="pysess" width="200">
  <img src="assets/logo-dark.svg#gh-dark-mode-only" alt="pysess" width="200">
</p>

Persistent Python REPL sessions for coding agents.

`pysess` runs a Python interpreter in the background and gives you CLI access to it. Coding agents can run Python across separate shell calls. Imports, variables, and connections stay in memory, so agents do not have to start fresh or pass intermediate data through the context window. `pysess` uses only the Python standard library. Installing packages at runtime also requires the `uv` executable. See [SKILL.md](SKILL.md) for the agent skill definition.

![pysess demo](assets/demo.svg)

## Installation

Install `pysess` globally with [`uv`](https://docs.astral.sh/uv/):

```bash
# Install directly from GitHub into ~/.local/share/uv/tools/pysess
# and link the pysess executable into ~/.local/bin
uv tool install git+https://github.com/tsaarni/pysess

# Upgrade to the latest commit
uv tool upgrade pysess

# Install in editable mode from a local checkout so changes under src/ take effect without reinstalling
uv tool install --editable .

# Uninstall
uv tool uninstall pysess
```

## Usage

| Command | Description |
| --- | --- |
| `pysess new [-n NAME] [-t SEC \| -d] [-c CODE \| FILE]` | Start a new session (random ID or `-n`/`--name`) and optionally run code from `-c`, `FILE`, or `stdin`. |
| `pysess exec ID [-t SEC \| -d] [-c CODE \| FILE]` | Run code in `ID` (reads `stdin` by default). Wait up to `-t`/`--timeout` (default `10`s), or pass `-d`/`--detach` to run in the background. |
| `pysess wait ID [-t SEC]` | Wait for a background step in `ID` up to `-t`/`--timeout` (default `30`s; `-t 0` to poll). |
| `pysess cancel ID` | Interrupt the running step in `ID` with `KeyboardInterrupt` and keep the session state. |
| `pysess ps [ID]` | List active sessions, or inspect `ID` for state, running location, idle time, and variables. |
| `pysess kill [ID \| --all]` | Stop session `ID` (or all sessions with `--all`) and remove socket files. |
| `pysess venv ls` | List venvs created with the `pysess.venv` Python API, including Python version, size, and packages. |
| `pysess venv rm NAME... \| --all` | Remove shared venvs, or private venvs from dead sessions. |

`pysess` captures all stdout and stderr output in order, including output from `print()`, native extensions, and subprocesses. If a background process writes output after a step finishes, the next `exec` or `wait` shows it.

## Running Blocking Calls in the Background

A session runs one step at a time, so a blocking call keeps the session busy until it finishes. Use `pysess.bg(fn, *args, **kwargs)` to run a blocking call in a daemon thread instead. It returns a `concurrent.futures.Future`, and the session stays free for other commands. `pysess kill` still stops the session even if the background call has not finished.

```bash
pysess new -n web -c 'import pysess, urllib.request; req = pysess.bg(urllib.request.urlopen, "http://127.0.0.1:8080/slow", timeout=120)'
pysess exec web -c 'req.done()'                       # False while the request waits
pysess exec web -c 'req.result(timeout=5).status'     # re-raises the call's exception
```

## Installing Packages at Runtime

Inside a session, `import pysess` gives you `pysess.venv`. It uses [`uv`](https://docs.astral.sh/uv/) to install packages into temporary virtual environments and adds them to the running interpreter. It never writes to user-wide site-packages. Each session gets a private venv that `pysess` deletes when the session ends. Sessions can also share a named venv. See [SKILL.md](SKILL.md) for the full API.

```bash
pysess new -t 60 -c 'import pysess; pysess.venv.install("httpx"); import httpx'
```

## Session Logging

Set `PYSESS_LOG_DIR` to record each session's executed cells, output, and tracebacks as a Python REPL transcript (`<YYYYMMDD-HHMMSS>-<ID>.log`). `pysess` creates the directory automatically if it does not exist.

```bash
export PYSESS_LOG_DIR="/tmp/pysess-$(id -u)/logs"

pysess new -n audited-session -c "x = 40
y = 2"
pysess exec audited-session -c "x + y"
pysess exec audited-session -c "for i in range(2):
    print(i)"
```

Example `/tmp/pysess-<uid>/logs/20261117-202636-audited-session.log`:

```python
# pysess 2026-11-17T20:26:36+02:00
>>> x = 40
... y = 2
# pysess 2026-11-17T20:26:37+02:00
>>> x + y
42
# pysess 2026-11-17T20:26:38+02:00
>>> for i in range(2):
...     print(i)
0
1
```

## Configuration

| Environment variable | Default | Description |
| --- | --- | --- |
| `PYSESS_RUNTIME_DIR` | `/tmp/pysess-<uid>` | Directory for session sockets and venvs. `pysess` creates it automatically with mode `0700`, and the current user must own it. Only `pysess` commands that use the same directory can see its sessions. |
| `PYSESS_IDLE_TIMEOUT` | `3600` | Seconds of inactivity before a session exits. Set to `0` or a negative number to disable. |
| `PYSESS_LOG_DIR` | unset (no logging) | Directory for session transcripts. See [Session Logging](#session-logging). |
| `PYSESS_UV` | `uv` from `PATH` | `uv` executable used by `pysess.venv`. |
| [`UV_CACHE_DIR`](https://docs.astral.sh/uv/reference/environment/#uv_cache_dir) | `~/.cache/uv` | Used directly by `uv`, not `pysess`. Sets where `uv` stores its download and wheel cache when installing packages. |

## Directory Structure

| Path | Content | Lifetime |
| --- | --- | --- |
| `/tmp/pysess-<uid>/<id>.sock` | Unix domain socket for session `<id>` | Until the session ends |
| `/tmp/pysess-<uid>/sessions/<id>/venv/` | Private venv for session `<id>` | Until the session ends |
| `/tmp/pysess-<uid>/venvs/<name>/` | Shared venv `<name>` | Until `pysess venv rm <name>`, or until the OS clears `/tmp` |
| `$PYSESS_LOG_DIR/<YYYYMMDD-HHMMSS>-<id>.log` | Session transcript (when `PYSESS_LOG_DIR` is set) | Kept after `pysess kill` |
| `~/.cache/uv/` | Download and wheel cache written by `uv` | Managed by `uv` |

## Development

Run the CLI and development checks with [`uv`](https://docs.astral.sh/uv/):

```bash
# Install dev tools (pytest, ruff, ty)
uv sync

# Run the CLI
uv run pysess --help

# Run tests, linter, and type checker
uv run pytest
uv run ruff check
uv run ty check
```
