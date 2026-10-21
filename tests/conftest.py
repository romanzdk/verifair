import pytest
from fastapi.testclient import TestClient

from verifair import db
from verifair.app import create_app
from verifair.config import Settings


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
