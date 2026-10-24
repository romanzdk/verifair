import asyncio
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

import anyio
import anyio.to_thread
from fastapi import FastAPI, Request
from psycopg_pool import PoolTimeout
from starlette.concurrency import run_in_threadpool

from verifair import db


@asynccontextmanager
async def borrow(app: FastAPI, timeout: float | None = None) -> AsyncIterator[db.Connection]:
    # Wait for a free connection here on the event loop, never in pool.getconn() on a worker
    # thread: threads parked there can take every worker thread while the requests holding
    # the connections wait for one to run their handler, and nothing moves until PoolTimeout.
    pool, slots = app.state.pool, app.state.pool_slots
    timeout = pool.timeout if timeout is None else timeout
    timed_out = PoolTimeout(f"couldn't get a connection after {timeout:.2f} sec")
    deadline = time.monotonic() + timeout
    try:
        await asyncio.wait_for(slots.acquire(), timeout)
    except TimeoutError:
        raise timed_out from None
    try:
        # shielded so a dropped request still gets and returns the connection, and the GC
        # never has to (FastAPI's contextmanager_in_threadpool skips __exit__ on cancel)
        borrowed = pool.connection(timeout=deadline - time.monotonic())
        with anyio.CancelScope(shield=True):
            try:
                conn = await run_in_threadpool(borrowed.__enter__)
            except PoolTimeout:
                raise timed_out from None
        exc_info = (None, None, None)
        try:
            yield conn
        except BaseException as error:
            exc_info = (type(error), error, error.__traceback__)
            raise
        finally:
            # own limiter: returning a connection must not queue behind busy worker threads
            with anyio.CancelScope(shield=True):
                await anyio.to_thread.run_sync(
                    borrowed.__exit__, *exc_info, limiter=anyio.CapacityLimiter(1)
                )
    finally:
        slots.release()


async def run_with_conn(app: FastAPI, func: Callable, *args, **kwargs):
    async with borrow(app) as conn:
        return await run_in_threadpool(func, conn, *args, **kwargs)


async def get_conn(request: Request) -> AsyncIterator[db.Connection]:
    async with borrow(request.app) as conn:
        yield conn
