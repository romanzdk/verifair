import pytest
from fastapi.testclient import TestClient

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
    return TestClient(create_app(settings))
