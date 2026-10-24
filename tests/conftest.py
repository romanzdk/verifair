import os
import uuid
from contextlib import closing

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import conninfo, sql

from verifair import db
from verifair.app import create_app
from verifair.config import Settings

TEST_DATABASE_URL = os.environ.get(
    "VERIFAIR_TEST_DATABASE_URL", "postgresql://verifair:verifair@localhost:5432/verifair"
)


@pytest.fixture(scope="session", autouse=True)
def admin_conn():
    try:
        c = psycopg.connect(TEST_DATABASE_URL, connect_timeout=3, autocommit=True)
    except psycopg.OperationalError:
        info = conninfo.conninfo_to_dict(TEST_DATABASE_URL)
        where = f"{info.get('host')}:{info.get('port', 5432)}/{info.get('dbname')}"
        pytest.exit(
            f"Postgres is not reachable at {where}. Run: docker compose up -d db", returncode=2
        )
    with c:
        yield c


@pytest.fixture(autouse=True)
def database_url_env(monkeypatch):
    monkeypatch.setenv("VERIFAIR_DATABASE_URL", "postgresql://unused")


@pytest.fixture
def new_database_url(admin_conn):
    schemas = []

    def create():
        schema = f"vt_{uuid.uuid4().hex[:12]}"
        admin_conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        schemas.append(schema)
        # tests need no crash durability, and not waiting for the WAL flush keeps the suite fast
        options = f"-c search_path={schema} -c synchronous_commit=off"
        return conninfo.make_conninfo(TEST_DATABASE_URL, options=options)

    yield create
    for schema in schemas:
        admin_conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


@pytest.fixture
def conn(new_database_url):
    c = db.connect(new_database_url())
    db.init_schema(c)
    yield c
    c.close()


@pytest.fixture
def make_app():
    apps = []

    def make(settings):
        app = create_app(settings)
        apps.append(app)
        return app

    yield make
    for app in apps:
        app.state.pool.close()


# a pool of one: a handler that borrows a second connection while holding one times out
@pytest.fixture
def settings(new_database_url):
    return Settings(
        api_token="t",
        database_url=new_database_url(),
        base_url="http://localhost:8000",
        slack_webhook_url=None,
        required_sections=(),
        db_pool_size=1,
    )


@pytest.fixture
def client(settings, make_app):
    return TestClient(make_app(settings), headers={"Sec-Fetch-Site": "same-origin"})


@pytest.fixture
def shared_settings(new_database_url):
    return Settings(
        api_token=None,
        database_url=new_database_url(),
        base_url="http://localhost:8000",
        slack_webhook_url=None,
        identity_header="X-Forwarded-Email",
        bootstrap_admin_id="admin@example.com",
        db_pool_size=1,
    )


@pytest.fixture
def shared_client(shared_settings, make_app):
    return TestClient(
        make_app(shared_settings),
        headers={"Sec-Fetch-Site": "same-origin", "X-Forwarded-Email": "admin@example.com"},
    )


@pytest.fixture
def as_user(shared_client, shared_settings):
    def member(id, name=None):
        with closing(db.connect(shared_settings.database_url)) as conn:
            db.add_user(conn, id=id, name=name or id.partition("@")[0], role="member")
        return {"X-Forwarded-Email": id}

    return member
