# pysess

Persistent Python REPL sessions for coding agents.

`pysess` keeps a Python interpreter running in the background and gives you CLI access to it. It lets coding agents work in Python across separate shell calls, keeping imports, variables, and connections in memory instead of starting fresh or passing all intermediate data through the context window. It uses only the Python standard library. See [SKILL.md](SKILL.md) for the agent skill definition.

## Usage

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
