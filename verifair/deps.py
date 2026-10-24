from collections.abc import Iterator

from fastapi import Request

from verifair import db


def get_conn(request: Request) -> Iterator[db.Connection]:
    with request.app.state.pool.connection() as conn:
        yield conn
