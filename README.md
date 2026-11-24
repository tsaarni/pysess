# pysess

Persistent Python REPL sessions for coding agents.

`pysess` keeps a Python interpreter running in the background and gives you CLI access to it. It lets coding agents work in Python across separate shell calls, keeping imports, variables, and connections in memory instead of starting fresh or passing all intermediate data through the context window. It uses only the Python standard library (installing packages at runtime additionally requires the `uv` executable). See [SKILL.md](SKILL.md) for the agent skill definition.

## Usage

![pysess demo](assets/demo.svg)

| Command | Description |
| --- | --- |
| `pysess new [-n NAME] [-t SEC \| -d] [-c CODE \| FILE]` | Start a new session (random ID or `-n`/`--name`) and optionally run code from `-c`, `FILE`, or `stdin`. |
| `pysess exec ID [-t SEC \| -d] [-c CODE \| FILE]` | Run code in `ID` (reads `stdin` by default). Waits up to `-t`/`--timeout` (default `10`s), or use `-d`/`--detach` to run in the background. |
| `pysess wait ID [-t SEC]` | Wait for a background step in `ID` up to `-t`/`--timeout` (default `30`s; `-t 0` to poll). |
| `pysess cancel ID` | Interrupt the running step in `ID` with `KeyboardInterrupt` while keeping session state alive. |
| `pysess ps [ID]` | List active sessions, or inspect `ID` for state, running location, idle time, and variables. |
| `pysess kill [ID \| --all]` | Stop session `ID` (or all sessions with `--all`) and remove socket files. |
| `pysess venv ls` | List venvs created with the `pysess.venv` Python API, with Python version, size, and packages. |
| `pysess venv rm NAME... \| --all` | Remove shared venvs, or private venvs left behind by dead sessions. |

Output of `print()`, subprocesses, `os.system()`, and C extensions is captured in order. Output written after a step finished, for example by a background process, is shown by the next `exec` or `wait`.

## Installing Packages at Runtime

Inside a session, `import pysess` gives access to `pysess.venv`, which installs packages with [`uv`](https://docs.astral.sh/uv/) into ephemeral virtual environments and adds them to the running interpreter, without writing to user-wide site-packages. Each session gets a private venv deleted when it ends, or sessions can share a named venv. See [SKILL.md](SKILL.md) for the full API.

```bash
pysess new -t 60 -c 'import pysess; pysess.venv.install("httpx"); import httpx'
```

## Session Logging

Set `PYSESS_LOG_DIR` to record each session's executed cells, output, and tracebacks as a Python REPL transcript (`<YYYYMMDD-HHMMSS>-<ID>.log`). Log files are kept after `pysess kill` for auditing.

```bash
export PYSESS_LOG_DIR=/tmp/pysess-logs

pysess new -n audited-session -c "x = 40
y = 2"
pysess exec audited-session -c "x + y"
pysess exec audited-session -c "for i in range(2):
    print(i)"
```

Example `/tmp/pysess-logs/20261117-202636-audited-session.log`:

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
| `PYSESS_RUNTIME_DIR` | `/tmp/pysess-<uid>` | Directory for session sockets and venvs. Must be owned by the user with mode `0700`. Sessions are only visible to `pysess` commands that use the same directory. |
| `PYSESS_IDLE_TIMEOUT` | `3600` | Seconds without activity before a session exits; `0` or negative disables. |
| `PYSESS_LOG_DIR` | unset (no logging) | Directory for session transcripts, see [Session Logging](#session-logging). |
| `PYSESS_UV` | `uv` from `PATH` | `uv` executable used by `pysess.venv`. |
| `UV_CACHE_DIR` (uv's own) | `~/.cache/uv` | Download and wheel cache written by `uv` when installing packages. |

Directories, relative to the runtime directory:

| Path | Content | Lifetime |
| --- | --- | --- |
| `<id>.sock` | Unix socket of session `<id>` | Until the session ends |
| `sessions/<id>/venv/` | Private venv of session `<id>` | Until the session ends |
| `venvs/<name>/` | Shared venv `<name>` | Until `pysess venv rm <name>`, or until the OS clears `/tmp` |

The default is `/tmp/pysess-<uid>` (the same convention as tmux) rather than `$XDG_RUNTIME_DIR`, so a sandboxed agent only needs access to that directory, not to `/run/user/<uid>`, which holds keyring, D-Bus, and other session sockets. Many systems clear `/tmp` at boot and delete entries unused for some time (systemd's default is 10 days, some distributions use 30), which can remove a long-unused shared venv.

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

## Development

Manage dependencies, run the CLI, and execute checks with [`uv`](https://docs.astral.sh/uv/):

```bash
# Sync environment and dev dependencies
uv sync

# Run the CLI
uv run pysess --help

# Run tests, linter, and type checker
uv run pytest
uv run ruff check
uv run ty check
```
