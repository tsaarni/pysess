"""Persistent Python REPL sessions for coding agents.

Python API available inside sessions (`import pysess`):

- `pysess.venv`: install packages into ephemeral virtual environments at runtime.
- `pysess.Error`: raised by API calls on failure.

See `help(pysess.venv)` for details.
"""

from pysess import venv
from pysess._runtime import Error

__all__ = ["Error", "venv"]
