import httpx
import pytest

from verifair import notify
from verifair.config import Settings

REPORT = {"id": "prorate", "project": "billing-service", "title": "Prorate", "risk": "high"}


def make_settings(url):
    return Settings(api_token="t", db_path=":memory:", base_url="http://x", slack_webhook_url=url)


def test_report_pushed_text():
    assert notify.report_pushed_text(REPORT, 2, "http://x") == (
        "New report v2 in billing-service: Prorate (risk high) http://x/r/prorate"
    )


def test_comment_text_with_section_and_truncation():
    comment = {"id": 7, "author_name": "bot", "section": "intent", "body": "a" * 200}
    assert notify.comment_text(REPORT, comment, "http://x") == (
        f"bot commented on Prorate › intent: {'a' * 140} http://x/r/prorate#vf_c7"
    )


def test_comment_text_without_section():
    comment = {"id": 1, "author_name": "bot", "section": None, "body": "Hi"}
    assert notify.comment_text(REPORT, comment, "http://x") == (
        "bot commented on Prorate › report: Hi http://x/r/prorate#vf_c1"
    )


@pytest.mark.parametrize(
    ("verdict", "phrase"), [("approve", "approved"), ("request_changes", "requested changes on")]
)
def test_verdict_text(verdict, phrase):
    row = {"reviewer_name": "Ann", "verdict": verdict, "version": 3, "summary": "Looks fine"}
    assert notify.verdict_text(REPORT, row, "http://x") == (
        f"Ann {phrase} Prorate v3: Looks fine http://x/r/prorate"
    )


EVIL = {"id": "p", "project": "a&b", "title": "<T>", "risk": "high"}


def test_report_pushed_text_escapes_user_text():
    assert notify.report_pushed_text(EVIL, 1, "http://x?a=1&b=2") == (
        "New report v1 in a&amp;b: &lt;T&gt; (risk high) http://x?a=1&b=2/r/p"
    )


def test_comment_text_escapes_user_text():
    comment = {"id": 1, "author_name": "<@U1>", "section": "a<b", "body": "<!channel> a < b"}
    assert notify.comment_text(EVIL, comment, "http://x") == (
        "&lt;@U1&gt; commented on &lt;T&gt; › a&lt;b: &lt;!channel&gt; a &lt; b http://x/r/p#vf_c1"
    )


def test_verdict_text_escapes_user_text():
    row = {"reviewer_name": "A&B", "verdict": "approve", "version": 1, "summary": "<!here>"}
    assert notify.verdict_text(EVIL, row, "http://x") == (
        "A&amp;B approved &lt;T&gt; v1: &lt;!here&gt; http://x/r/p"
    )


def test_send_without_url_makes_no_call(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("httpx.post must not be called")

    monkeypatch.setattr(httpx, "post", boom)
    notify.send(make_settings(None), "hi")


def test_send_posts_text(monkeypatch):
    calls = []

    class Response:
        def raise_for_status(self):
            pass

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return Response()

    monkeypatch.setattr(httpx, "post", fake_post)
    notify.send(make_settings("https://hooks.example/x"), "hi")
    assert calls == [("https://hooks.example/x", {"json": {"text": "hi"}, "timeout": 5})]


@pytest.mark.parametrize("error", [httpx.HTTPError("boom"), httpx.InvalidURL("bad")])
def test_send_swallows_errors_without_logging_url(monkeypatch, caplog, error):
    def fail(*a, **k):
        raise error

    monkeypatch.setattr(httpx, "post", fail)
    notify.send(make_settings("https://hooks.example/secret-xyz"), "hi")
    assert type(error).__name__ in caplog.text
    assert "secret-xyz" not in caplog.text
    assert "boom" not in caplog.text


def test_send_logs_status_code_not_url(monkeypatch, caplog):
    url = "https://hooks.example/secret-xyz"

    def respond(*a, **k):
        return httpx.Response(404, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", respond)
    notify.send(make_settings(url), "hi")
    assert "HTTPStatusError 404" in caplog.text
    assert "secret-xyz" not in caplog.text
