import smtplib
import ssl
from dataclasses import replace

import httpx
import pytest

from verifair import notify
from verifair.config import Settings

REPORT = {"id": "prorate", "project": "billing-service", "title": "Prorate", "risk": "high"}


def make_settings(url):
    return Settings(
        api_token="t",
        database_url="postgresql://unused",
        base_url="http://x",
        slack_webhook_url=url,
    )


def test_report_pushed_text():
    assert notify.report_pushed_text(REPORT, 2, "http://x") == (
        "Draft report v2 in billing-service: Prorate http://x/r/prorate"
    )


def test_ready_for_review_text():
    assert notify.ready_for_review_text(REPORT, 2, "Ann", "http://x") == (
        "Ready for review: Prorate v2 (risk high), signed by Ann http://x/r/prorate"
    )


def test_ready_for_review_text_escapes_user_text():
    assert notify.ready_for_review_text(EVIL, 1, "<@U1>", "http://x") == (
        "Ready for review: &lt;T&gt; v1 (risk high), signed by &lt;@U1&gt; http://x/r/p"
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
        "Draft report v1 in a&amp;b: &lt;T&gt; http://x?a=1&b=2/r/p"
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


class FakeSMTP:
    sessions = []
    offers_tls = True
    fail = None

    def __init__(self, host, port, timeout, context=None):
        self.host, self.port, self.timeout, self.context = host, port, timeout, context
        self.calls, self.sent = [], []
        FakeSMTP.sessions.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.calls.append("quit")

    def ehlo(self):
        self.calls.append("ehlo")

    def has_extn(self, name):
        return name == "starttls" and self.offers_tls

    def starttls(self, context):
        self.calls.append(("starttls", context.verify_mode, context.check_hostname))

    def login(self, user, password):
        self.calls.append(("login", user, password))
        if FakeSMTP.fail:
            raise FakeSMTP.fail

    def send_message(self, message):
        self.sent.append(message)


class FakeSMTPSSL(FakeSMTP):
    pass


@pytest.fixture
def smtp(monkeypatch):
    monkeypatch.setattr(FakeSMTP, "sessions", [])
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTPSSL)
    return FakeSMTP


def mail_settings(url):
    return replace(make_settings(None), smtp_url=url, email_from="verifair@example.com")


def test_email_over_starttls_logs_in_with_decoded_credentials(smtp):
    url = "smtp://me%40example.com:p%40ss%3Aword@mail.example.com:587"
    notify.email(mail_settings(url), ["bob@example.com", "carol@example.com"], "Subj", "Text")
    [session] = smtp.sessions
    assert type(session) is FakeSMTP
    assert (session.host, session.port, session.timeout) == ("mail.example.com", 587, 10)
    assert session.calls == [
        "ehlo",
        ("starttls", ssl.CERT_REQUIRED, True),
        ("login", "me@example.com", "p@ss:word"),
        "quit",
    ]
    assert [(m["From"], m["To"], m["Subject"]) for m in session.sent] == [
        ("verifair@example.com", "bob@example.com", "Subj"),
        ("verifair@example.com", "carol@example.com", "Subj"),
    ]
    assert session.sent[0].get_content() == "Text\n"


def test_email_without_starttls_or_credentials_sends_plain(smtp, monkeypatch):
    monkeypatch.setattr(smtp, "offers_tls", False)
    notify.email(mail_settings("smtp://localhost:1025"), ["bob@example.com"], "S", "T")
    [session] = smtp.sessions
    assert session.calls == ["ehlo", "quit"] and len(session.sent) == 1


def test_smtps_uses_implicit_tls_with_a_verifying_context(smtp):
    notify.email(mail_settings("smtps://u:p@mail.example.com"), ["bob@example.com"], "S", "T")
    [session] = smtp.sessions
    assert type(session) is FakeSMTPSSL and session.port == 0
    assert (session.context.verify_mode, session.context.check_hostname) == (
        ssl.CERT_REQUIRED,
        True,
    )
    assert "ehlo" in session.calls and not any(c[0] == "starttls" for c in session.calls)


def test_email_is_skipped_without_smtp_or_recipients(smtp):
    notify.email(make_settings(None), ["bob@example.com"], "S", "T")
    notify.email(mail_settings("smtp://localhost:1025"), [], "S", "T")
    assert smtp.sessions == []


def test_email_failure_logs_one_warning_without_secrets(smtp, monkeypatch, caplog):
    monkeypatch.setattr(smtp, "fail", smtplib.SMTPAuthenticationError(535, b"bad"))
    notify.email(mail_settings("smtp://user:hunter2@mail.example.com"), ["b@x.com"], "S", "T")
    [record] = caplog.records
    assert (record.levelname, record.getMessage()) == (
        "WARNING",
        "email failed: SMTPAuthenticationError 535",
    )
    assert "hunter2" not in caplog.text and "mail.example.com" not in caplog.text


def test_review_email_text():
    subject, text = notify.review_email(
        REPORT | {"title": "Pro\r\nBcc: x@evil.com"}, "Ann", "Is a day right?", "http://x"
    )
    assert subject == "Review requested: Pro Bcc: x@evil.com"
    assert text == (
        "Ann asked you to review Pro\r\nBcc: x@evil.com.\n"
        "Project: billing-service\n"
        "Risk: high\n\n"
        "Judge this:\n"
        "Is a day right?\n\n"
        "http://x/r/prorate"
    )


def test_review_email_trims_judge_this_and_omits_it_when_empty(smtp):
    _, text = notify.review_email(REPORT, "Ann", "a" * 2000, "http://x")
    assert f"\n{'a' * 1500}…\n" in text
    _, text = notify.review_email(REPORT, "Ann", "", "http://x")
    assert "Judge this" not in text


def test_title_with_newlines_still_sends(smtp):
    subject, text = notify.review_email(REPORT | {"title": "A\nB"}, "Ann", "", "http://x")
    notify.email(mail_settings("smtp://localhost:1025"), ["bob@example.com"], subject, text)
    assert smtp.sessions[0].sent[0]["Subject"] == "Review requested: A B"
