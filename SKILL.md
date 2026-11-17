---
name: pysess
description: Persistent Python REPL sessions that keep sockets, connections, imports, and variables alive across bash calls.
---

Use `pysess` instead of `python3 -c` when keeping Python state, sockets, or client connections alive across multiple bash calls, or when coordinating blocking operations (like a server `accept()` and a client request) across turns.

```bash
# 1. Start a session, bind a server socket, and get its URL synchronously
pysess new <<'EOF'
import socket
srv = socket.create_server(("127.0.0.1", 0))
f"http://127.0.0.1:{srv.getsockname()[1]}"
EOF
# Output:
# [pysess: k9x2]
# 'http://127.0.0.1:54321'

# 2. Run the blocking accept() step in the background (-d / --detach returns immediately)
pysess exec k9x2 -d <<'EOF'
conn, addr = srv.accept()
req = conn.recv(4096).decode()
conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
conn.close()
print("handled request from", addr, flush=True)
EOF
# Output:
# [pysess: k9x2 | running (0.0s) - use 'pysess wait k9x2' or 'pysess cancel k9x2']

# 3. Trigger the waiting server from another command
curl -s http://127.0.0.1:54321/hello

# 4. Come back to the session and inspect captured state (trailing expressions auto-print and bind to `_`)
#    Completed background output ("handled request from ...") is flushed automatically!
pysess exec k9x2 -c "req.splitlines()[0]"

# 5. Clean up when done
pysess kill k9x2
```

## Rules
- Pass code to `new` or `exec` via `-c CODE`, a `FILE` path, or `stdin` (`new` with no code starts an empty session).
- Keep persistent resources (clients, connections, sockets, open files) in top-level variables instead of closing them in `with` blocks.
- Trailing expressions auto-print (`repr()` truncated at 32 KB; full value stays in `_`; `print()` is never truncated).
- By default, `new` and `exec` block until the step finishes (or `-t`/`--timeout` expires). Use this when you need the result or setup output (like a bound port) right away.
- Pass `-d` / `--detach` when you do not want to wait for the step to finish (e.g., blocking `accept()` or a background task you will trigger or inspect later). Split setup that returns a value from the `-d` step.
- `-t SEC` / `--timeout SEC` accepts numeric seconds (e.g., `0.2`, `10`; default 10 seconds on `new`/`exec`, 30 seconds on `wait`); mutually exclusive with `-d`/`--detach`.
- When a step is running in the background (`[pysess: <id> | running ...]`):
  - Once it finishes, the next `pysess exec <id>` flushes its unread output automatically.
  - While still running, use `pysess wait <id>` (`-t 0` to poll) or `pysess cancel <id>` (`KeyboardInterrupt`).
- Use `pysess ps [<id>]` to inspect sessions, blocked locations, and variables; use `pysess kill <id>` or `pysess kill --all` to clean up.
