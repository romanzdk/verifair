import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.concurrency import contextmanager_in_threadpool
from psycopg_pool import PoolTimeout
from starlette.concurrency import run_in_threadpool

from verifair import db


@asynccontextmanager
async def borrow(app: FastAPI) -> AsyncIterator[db.Connection]:
    # Wait for a free connection here on the event loop, never in pool.getconn() on a worker
    # thread: threads parked there can take every worker thread while the requests holding
    # the connections wait for one to run their handler, and nothing moves until PoolTimeout.
    pool, slots = app.state.pool, app.state.pool_slots
    try:
        await asyncio.wait_for(slots.acquire(), pool.timeout)
    except TimeoutError:
        raise PoolTimeout(f"couldn't get a connection after {pool.timeout:.2f} sec") from None
    try:
        async with contextmanager_in_threadpool(pool.connection()) as conn:
            yield conn
    finally:
        slots.release()


async def run_with_conn(app: FastAPI, func: Callable, *args, **kwargs):
    async with borrow(app) as conn:
        return await run_in_threadpool(func, conn, *args, **kwargs)


async def get_conn(request: Request) -> AsyncIterator[db.Connection]:
    async with borrow(request.app) as conn:
        yield conn
