import pytest

from verifair.config import load_settings


def test_health(client):
    assert client.get("/healthz").json() == {"ok": True}


def test_missing_token_raises(monkeypatch):
    monkeypatch.delenv("VERIFAIR_API_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="VERIFAIR_API_TOKEN"):
        load_settings()


def test_defaults(monkeypatch):
    monkeypatch.setenv("VERIFAIR_API_TOKEN", "t")
    s = load_settings()
    assert (s.db_path, s.base_url, s.slack_webhook_url) == (
        "./verifair.db",
        "http://localhost:8000",
        None,
    )
