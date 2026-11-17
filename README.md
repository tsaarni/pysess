# pysess

Persistent Python REPL sessions for coding agents.

`pysess` keeps a Python interpreter running in the background and gives you CLI access to it. It lets coding agents work in Python across separate shell calls, keeping imports, variables, and connections in memory instead of starting fresh or passing all intermediate data through the context window. It uses only the Python standard library. See [SKILL.md](SKILL.md) for the agent skill definition.

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
