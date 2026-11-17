# pysess

Persistent Python REPL sessions for coding agents.

`pysess` keeps Python interpreters, stateful connections, imports, and variables alive across stateless shell calls using only the Python standard library. See [SKILL.md](SKILL.md) for the agent skill definition.

## Usage

```console
$ pysess new -c "import math; x = 41"
[session: yd98]

$ pysess exec yd98 -c "x + 1"
42

$ pysess ps
yd98  pid=11733  state=idle  idle=0s  vars=3

$ pysess kill yd98
Killed session 'yd98'.
```

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
