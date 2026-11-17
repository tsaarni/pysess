# pysess

Persistent Python REPL sessions for coding agents.

`pysess` keeps a Python interpreter running in the background and gives you CLI access to it. It lets coding agents work in Python across separate shell calls, keeping imports, variables, and connections in memory instead of starting fresh or passing all intermediate data through the context window. It uses only the Python standard library. See [SKILL.md](SKILL.md) for the agent skill definition.

## Usage

```console
$ pysess new <<'EOF'
import socket
srv = socket.create_server(("127.0.0.1", 0))
f"http://127.0.0.1:{srv.getsockname()[1]}"
EOF
[pysess: 0zvo]
'http://127.0.0.1:55214'

$ pysess exec 0zvo -d <<'EOF'
conn, addr = srv.accept()
req = conn.recv(4096).decode()
conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
conn.close()
print("handled request from", addr, flush=True)
EOF
[pysess: 0zvo | running (0.0s) at <repl>:1 in accept() - use 'pysess wait 0zvo' or 'pysess cancel 0zvo']

$ curl -s http://127.0.0.1:55214/hello
ok

$ pysess exec 0zvo -c "req.splitlines()[0]"
handled request from ('127.0.0.1', 55216)
'GET /hello HTTP/1.1'

$ pysess kill 0zvo
[pysess: 0zvo | killed]
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
