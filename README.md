# pysess

Persistent Python REPL sessions for coding agents.

`pysess` keeps Python interpreters, stateful connections, imports, and variables alive across stateless shell calls using only the Python standard library. See [DESIGN.md](DESIGN.md) for the full architecture and protocol specification, and [SKILL.md](SKILL.md) for the agent skill definition.

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
