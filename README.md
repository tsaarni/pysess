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
[session: yd98 | killed]
```

### Blocking step with `curl`

```console
$ pysess new -t 0.2 <<'EOF'
import socket
srv = socket.create_server(("127.0.0.1", 0))
print(f"listening on http://127.0.0.1:{srv.getsockname()[1]}", flush=True)
conn, addr = srv.accept()
req = conn.recv(4096).decode()
conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
conn.close()
print("handled request from", addr, flush=True)
EOF
[session: 0zvo]
listening on http://127.0.0.1:55214
[session: 0zvo | running (0.2s) at <repl>:4 in accept() - use 'pysess wait 0zvo' or 'pysess cancel 0zvo']

$ curl -s http://127.0.0.1:55214/hello
ok

$ pysess exec 0zvo -c "req.splitlines()[0]"
handled request from ('127.0.0.1', 55216)
'GET /hello HTTP/1.1'

$ pysess kill 0zvo
[session: 0zvo | killed]
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
