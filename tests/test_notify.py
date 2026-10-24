import smtplib
import ssl
import threading
import time
from dataclasses import replace

import anyio
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
    refuse = ()
    delay = 0
    active = peak = 0
    lock = threading.Lock()

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

    def send_message(self, message, to_addrs=None):
        if to_addrs[0] in self.refuse:
            raise smtplib.SMTPRecipientsRefused({to_addrs[0]: (550, b"no")})
        with FakeSMTP.lock:
            FakeSMTP.active += 1
            FakeSMTP.peak = max(FakeSMTP.peak, FakeSMTP.active)
        time.sleep(self.delay)
        with FakeSMTP.lock:
            FakeSMTP.active -= 1
        self.sent.append((message, to_addrs))


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


def send(url, letters, subject="S"):
    notify.send_email(mail_settings(url), "r", subject, letters)


def test_email_over_starttls_logs_in_with_decoded_credentials(smtp):
    url = "smtp://me%40example.com:p%40ss%3Aword@mail.example.com:587"
    send(url, {"bob@example.com": "Text", "carol@example.com": "Other"}, "Subj")
    [session] = smtp.sessions
    assert type(session) is FakeSMTP
    assert (session.host, session.port, session.timeout) == ("mail.example.com", 587, 10)
    assert session.calls == [
        "ehlo",
        ("starttls", ssl.CERT_REQUIRED, True),
        ("login", "me@example.com", "p@ss:word"),
        "quit",
    ]
    assert [(m["From"], m["To"], m["Subject"], to) for m, to in session.sent] == [
        ("verifair@example.com", "bob@example.com", "Subj", ["bob@example.com"]),
        ("verifair@example.com", "carol@example.com", "Subj", ["carol@example.com"]),
    ]
    assert [m.get_content() for m, _ in session.sent] == ["Text\n", "Other\n"]


def test_email_without_starttls_or_credentials_sends_plain(smtp, monkeypatch):
    monkeypatch.setattr(smtp, "offers_tls", False)
    send("smtp://localhost:1025", {"bob@example.com": "T"})
    [session] = smtp.sessions
    assert session.calls == ["ehlo", "quit"] and len(session.sent) == 1


def test_credentials_are_never_sent_without_tls(smtp, monkeypatch, caplog):
    monkeypatch.setattr(smtp, "offers_tls", False)
    send("smtp://user:hunter2@mail.example.com:587", {"b@x.com": "T", "c@x.com": "T"})
    [session] = smtp.sessions
    assert session.calls == ["ehlo", "quit"] and session.sent == []
    [record] = caplog.records
    assert (record.levelname, record.getMessage()) == (
        "WARNING",
        "email not sent report=r recipients=2: the server offers no TLS,"
        " so verifair does not send the SMTP password",
    )
    assert "hunter2" not in caplog.text


def test_smtps_uses_implicit_tls_with_a_verifying_context(smtp):
    send("smtps://u:p@mail.example.com", {"bob@example.com": "T"})
    [session] = smtp.sessions
    assert type(session) is FakeSMTPSSL and session.port == 0
    assert (session.context.verify_mode, session.context.check_hostname) == (
        ssl.CERT_REQUIRED,
        True,
    )
    assert session.calls == ["ehlo", ("login", "u", "p"), "quit"] and len(session.sent) == 1


def test_email_is_skipped_without_smtp_or_recipients(smtp):
    notify.send_email(make_settings(None), "r", "S", {"bob@example.com": "T"})
    send("smtp://localhost:1025", {})
    assert smtp.sessions == []


def test_only_single_plain_addresses_get_mail(smtp):
    bad = ["a@x.com,b@y.com", "Bob <b@x.com>", "ops", "b@x.com\r\nBcc: e@evil.com", "a@x.com b@y"]
    send("smtp://localhost:1025", dict.fromkeys([*bad, "bob@example.com"], "T"))
    [session] = smtp.sessions
    assert [to for _, to in session.sent] == [["bob@example.com"]]
    send("smtp://localhost:1025", dict.fromkeys(bad, "T"))
    assert len(smtp.sessions) == 1


def test_a_refused_recipient_does_not_stop_the_others(smtp, monkeypatch, caplog):
    monkeypatch.setattr(smtp, "refuse", ("bad@x.com",))
    send("smtp://localhost:1025", {"bad@x.com": "T", "bob@example.com": "T"})
    assert [to for _, to in smtp.sessions[0].sent] == [["bob@example.com"]]
    assert [r.getMessage() for r in caplog.records] == [
        "email failed report=r recipients=1 error=SMTPRecipientsRefused code=-"
    ]


def test_email_failure_logs_one_warning_without_secrets_and_closes(smtp, monkeypatch, caplog):
    monkeypatch.setattr(smtp, "fail", smtplib.SMTPAuthenticationError(535, b"bad"))
    send("smtps://user:hunter2@mail.example.com", {"b@x.com": "T", "c@x.com": "T"})
    [record] = caplog.records
    assert (record.levelname, record.getMessage()) == (
        "WARNING",
        "email failed report=r recipients=2 error=SMTPAuthenticationError code=535",
    )
    assert "hunter2" not in caplog.text and "mail.example.com" not in caplog.text
    assert "b@x.com" not in caplog.text
    assert smtp.sessions[0].calls[-1] == "quit"


def test_emails_use_their_own_limit_of_two_threads(smtp, monkeypatch):
    monkeypatch.setattr(smtp, "delay", 0.1)
    monkeypatch.setattr(smtp, "peak", 0)
    settings = mail_settings("smtp://localhost:1025")

    async def main():
        async with anyio.create_task_group() as tg:
            for n in range(5):
                tg.start_soon(notify.email, settings, "r", "S", {f"b{n}@x.com": "T"})

    anyio.run(main)
    assert smtp.peak == 2 and len(smtp.sessions) == 5


TITLE = REPORT | {"title": "Pro\r\n  Bcc: x@evil.com"}


def test_review_email_after_signing_names_signer_and_asker():
    subject, text = notify.review_email(
        TITLE, 3, "Is a day right?", "http://x", signer="Alice", asker="Bob"
    )
    assert subject == 'Review requested: "Pro Bcc: x@evil.com"'
    assert text == (
        'Alice signed v3 of "Pro Bcc: x@evil.com". You were asked to review it by Bob.\n\n'
        "Project: billing-service · Risk: high\n\n"
        "Judge this:\n"
        "Is a day right?\n\n"
        "Open it to comment, approve or request changes:\n"
        "http://x/r/prorate\n\n"
        "— Sent by verifair. Replies to this mail are not read."
    )
    _, text = notify.review_email(REPORT, 3, "", "http://x", signer="Alice")
    assert text.startswith('Alice signed v3 of "Prorate". You were asked to review it.\n\n')
    assert "Judge this" not in text


def test_review_email_when_added_after_signing():
    _, text = notify.review_email(
        REPORT, 3, "", "http://x", signer="Alice", asker="Bob", added=True
    )
    assert text.startswith('Bob asked you to review "Prorate" (v3, signed by Alice).\n\n')


def test_review_email_cuts_judge_this_at_a_word_or_line():
    words = " ".join(["word"] * 400)
    _, text = notify.review_email(REPORT, 1, words, "http://x", signer="A")
    judge = text.split("Judge this:\n")[1].split("\n\nOpen it")[0]
    assert judge.endswith("word\nRead the rest on the page.")
    assert len(judge) <= 1500 + len("\nRead the rest on the page.")
    lines = "\n".join(["a" * 99] * 20)
    _, text = notify.review_email(REPORT, 1, lines, "http://x", signer="A")
    judge = text.split("Judge this:\n")[1].split("\n\nOpen it")[0]
    assert judge == "\n".join(["a" * 99] * 15) + "\nRead the rest on the page."
    _, text = notify.review_email(REPORT, 1, "x" * 1600, "http://x", signer="A")
    assert "x" * 1500 + "\nRead the rest on the page." in text


def test_title_with_newlines_still_sends(smtp):
    subject, text = notify.review_email(REPORT | {"title": "A\nB"}, 1, "", "http://x", signer="Ann")
    send("smtp://localhost:1025", {"bob@example.com": text}, subject)
    assert smtp.sessions[0].sent[0][0]["Subject"] == 'Review requested: "A B"'
