import asyncio
import logging
from typing import Any, Coroutine, TypeVar

T = TypeVar("T")


async def log_exc(coro):
    try:
        return await coro
    except Exception as e:
        logging.exception("Exception in background task", exc_info=e)


def create_task(
    coro: Coroutine[Any, Any, T],
) -> asyncio.Task[T]:
    task = asyncio.create_task(coro)
    task.add_done_callback(_log_failed_task)
    return task


def _log_failed_task(task: asyncio.Task):
    try:
        task.result()
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logging.exception(
            "Background task failed with exception %s", type(e).__name__, exc_info=e
        )
