"""Strong references for fire-and-forget asyncio tasks.

``asyncio.create_task`` only keeps a *weak* reference to the task it
returns, so a job started with a bare ``asyncio.create_task(...)`` can be
garbage-collected mid-flight (documented CPython behaviour). Verification
jobs, the NER preload and similar background work are spawned through
:func:`spawn` instead, which keeps them alive until they finish and lets the
app's lifespan shutdown cancel and await everything that is still running.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any

_tasks: set[asyncio.Task[Any]] = set()


def spawn(coro: Coroutine[Any, Any, Any], *, name: str | None = None) -> asyncio.Task[Any]:
    task = asyncio.create_task(coro, name=name)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return task


def running() -> list[asyncio.Task[Any]]:
    return [t for t in _tasks if not t.done()]


async def cancel_all(timeout: float = 5.0) -> None:
    """Cancel every tracked task and wait (bounded) for them to unwind."""
    pending = running()
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.wait(pending, timeout=timeout)
