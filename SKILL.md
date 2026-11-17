---
name: pysess
description: Persistent Python REPL sessions that keep sockets, connections, imports, and variables alive across bash calls.
---

Use `pysess` instead of `python3 -c` when keeping Python state, sockets, or client connections alive across multiple bash calls, or when coordinating blocking operations (like a server `accept()` and a client request) across turns.

```bash
# 1. Start a session, bind a server socket, and block on accept() (detaches after -t 0.2s)
pysess new -t 0.2 <<'EOF'
import socket
srv = socket.create_server(("127.0.0.1", 0))
print(f"listening on http://127.0.0.1:{srv.getsockname()[1]}", flush=True)
conn, addr = srv.accept()
req = conn.recv(4096).decode()
conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
conn.close()
print("handled request from", addr, flush=True)
EOF
# Output:
# [session: k9x2]
# listening on http://127.0.0.1:54321
# [session: k9x2 | RUNNING (0.2s) at <repl>:4 in accept() - use 'pysess wait k9x2' or 'pysess cancel k9x2']

# 2. Trigger the waiting server from another command
curl -s http://127.0.0.1:54321/hello

# 3. Come back to the session and inspect captured state (trailing expressions auto-print and bind to `_`)
#    Completed background output ("handled request from ...") is flushed automatically!
pysess exec k9x2 -c "req.splitlines()[0]"

# 4. Clean up when done
pysess kill k9x2
```

## Rules
- Keep persistent resources (clients, connections, sockets, open files) in top-level variables instead of closing them in `with` blocks at the end of an `exec` step.
- `-t SEC` accepts floats (default `10s`); pass a short timeout when intentionally starting a blocking or long-running step.
- If a step runs past `-t SEC`, `pysess` detaches it and prints `RUNNING (<elapsed>s) at <repl>:line in func()`:
  - Once it finishes, the next `pysess exec <id>` automatically flushes its unread output before running new code.
  - While it is still running, use `pysess wait <id>` (or `-t 0` to poll) to wait, or `pysess cancel <id>` to interrupt with `KeyboardInterrupt`.
- Run `pysess ps [<id>]` to inspect active sessions, blocked locations, and defined variables.
