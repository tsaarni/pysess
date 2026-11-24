"""Persistent Python REPL sessions for coding agents.

Python API available inside sessions (`import pysess`):

- `pysess.bg(fn, *args, **kwargs)`: run a blocking call in a daemon thread, return a `Future`.
- `pysess.venv`: install packages into ephemeral virtual environments at runtime.
- `pysess.Error`: raised by API calls on failure.

See `help(pysess.bg)` and `help(pysess.venv)` for details.
"""

from pysess import venv
from pysess._bg import bg
from pysess._runtime import Error

__all__ = ["Error", "bg", "venv"]
