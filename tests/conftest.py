import os
import uuid

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
def postgres_reachable():
    try:
        psycopg.connect(TEST_DATABASE_URL, connect_timeout=3).close()
    except psycopg.OperationalError:
        info = conninfo.conninfo_to_dict(TEST_DATABASE_URL)
        where = f"{info.get('host')}:{info.get('port', 5432)}/{info.get('dbname')}"
        pytest.exit(
            f"Postgres is not reachable at {where}. Run: docker compose up -d db", returncode=2
        )


@pytest.fixture
def new_database_url():
    schemas = []

    def create():
        schema = f"vt_{uuid.uuid4().hex[:12]}"
        with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as c:
            c.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        schemas.append(schema)
        return conninfo.make_conninfo(TEST_DATABASE_URL, options=f"-c search_path={schema}")

    yield create
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as c:
        for schema in schemas:
            c.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


@pytest.fixture
def conn(new_database_url):
    c = db.connect(new_database_url())
    db.init_schema(c)
    yield c
    c.close()


@pytest.fixture
def settings(tmp_path):
    return Settings(
        api_token="t",
        db_path=str(tmp_path / "test.db"),
        base_url="http://localhost:8000",
        slack_webhook_url=None,
        required_sections=(),
    )


@pytest.fixture
def client(settings):
    return TestClient(create_app(settings), headers={"Sec-Fetch-Site": "same-origin"})


@pytest.fixture
def shared_settings(tmp_path):
    return Settings(
        api_token=None,
        db_path=str(tmp_path / "shared.db"),
        base_url="http://localhost:8000",
        slack_webhook_url=None,
        identity_header="X-Forwarded-Email",
        bootstrap_admin_id="admin@example.com",
    )


@pytest.fixture
def shared_client(shared_settings):
    return TestClient(
        create_app(shared_settings),
        headers={"Sec-Fetch-Site": "same-origin", "X-Forwarded-Email": "admin@example.com"},
    )


@pytest.fixture
def as_user(shared_client, shared_settings):
    def member(id, name=None):
        conn = db.connect(shared_settings.db_path)
        try:
            db.add_user(conn, id=id, name=name or id.partition("@")[0], role="member")
        finally:
            conn.close()
        return {"X-Forwarded-Email": id}

    return member
