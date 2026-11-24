"""Run a blocking call in the background while the session stays free for new steps."""

from __future__ import annotations

import threading
from collections.abc import Callable
from concurrent.futures import Future
from typing import Any


def bg[T](fn: Callable[..., T], /, *args: Any, **kwargs: Any) -> Future[T]:
    """Call `fn(*args, **kwargs)` in a daemon thread and return a `Future` for its result.

    Use it for a call that blocks while you still need the session, for example an HTTP
    request that waits at a debugger breakpoint. Later steps read the result with
    `f.result(timeout)`, which re-raises the call's exception, or check `f.done()`.

    The thread is a daemon thread, so `pysess kill` ends the session even while the call
    is blocked (`ThreadPoolExecutor` threads would keep the process alive until the call
    returns). A running call cannot be cancelled; give it its own timeout.
    """
    future: Future[T] = Future()
    future.set_running_or_notify_cancel()

    def run() -> None:
        try:
            result = fn(*args, **kwargs)
        except BaseException as exc:  # noqa: BLE001 - deliver every failure via the Future, like ThreadPoolExecutor
            future.set_exception(exc)
        else:
            future.set_result(result)

    name = f"pysess.bg-{getattr(fn, '__name__', type(fn).__name__)}"
    threading.Thread(target=run, name=name, daemon=True).start()
    return future
