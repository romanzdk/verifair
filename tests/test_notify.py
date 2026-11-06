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
    errors = {}
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
        if to_addrs[0] in self.errors:
            raise self.errors[to_addrs[0]]
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


def test_email_has_date_and_message_id(smtp):
    send("smtp://localhost:1025", {"bob@example.com": "T", "carol@example.com": "T"})
    [session] = smtp.sessions
    ids = [m["Message-ID"] for m, _ in session.sent]
    assert all(m["Date"] for m, _ in session.sent)
    assert all(m["Auto-Submitted"] == "auto-generated" for m, _ in session.sent)
    assert all(i.endswith("@example.com>") for i in ids) and len(set(ids)) == 2


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


def test_any_smtp_error_for_one_recipient_does_not_stop_the_others(smtp, monkeypatch, caplog):
    monkeypatch.setattr(smtp, "errors", {"bad@x.com": smtplib.SMTPNotSupportedError("utf8")})
    delivered = notify.send_email(
        mail_settings("smtp://localhost:1025"), "r", "S", {"bad@x.com": "T", "bob@example.com": "T"}
    )
    assert delivered == ["bob@example.com"]
    assert [r.getMessage() for r in caplog.records] == [
        "email failed report=r recipients=1 error=SMTPNotSupportedError code=-"
    ]


def test_a_dropped_connection_stops_sending_and_logs_once(smtp, monkeypatch, caplog):
    monkeypatch.setattr(smtp, "errors", {"c@x.com": smtplib.SMTPServerDisconnected("gone")})
    letters = {"b@x.com": "T", "c@x.com": "T", "d@x.com": "T"}
    delivered = notify.send_email(mail_settings("smtp://localhost:1025"), "r", "S", letters)
    assert delivered == ["b@x.com"]
    assert [r.getMessage() for r in caplog.records] == [
        "email failed report=r recipients=2 error=SMTPServerDisconnected code=-"
    ]


def test_a_failed_quit_after_all_mail_went_out_logs_nothing(smtp, monkeypatch, caplog):
    def bad_quit(self, *exc):
        raise smtplib.SMTPResponseException(421, b"bye")

    monkeypatch.setattr(smtp, "__exit__", bad_quit)
    letters = {"b@x.com": "T", "c@x.com": "T"}
    delivered = notify.send_email(mail_settings("smtp://localhost:1025"), "r", "S", letters)
    assert delivered == ["b@x.com", "c@x.com"]
    assert caplog.records == []


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


TARGETS = [("plan", "plan revision 3"), ("implementation", "implementation v3")]
PLAN_FIRST = REPORT | {"workflow": "plan_first"}


@pytest.mark.parametrize(("target", "label"), TARGETS)
def test_plan_first_draft_and_ready_identify_target_revision(target, label):
    assert notify.report_pushed_text(PLAN_FIRST, 3, "http://x", target=target) == (
        f"Draft {label} in billing-service: Prorate http://x/r/prorate?tab={target}"
    )
    assert notify.ready_for_review_text(PLAN_FIRST, 3, "Ann", "http://x", target=target) == (
        f"Ready for review: Prorate {label} (risk high), "
        f"signed by Ann http://x/r/prorate?tab={target}"
    )


def test_plan_first_formatters_default_to_implementation():
    assert notify.report_pushed_text(PLAN_FIRST, 3, "http://x") == (
        "Draft implementation v3 in billing-service: Prorate http://x/r/prorate?tab=implementation"
    )
    assert notify.ready_for_review_text(PLAN_FIRST, 3, "Ann", "http://x") == (
        "Ready for review: Prorate implementation v3 (risk high), "
        "signed by Ann http://x/r/prorate?tab=implementation"
    )
    subject, text = notify.review_email(PLAN_FIRST, 3, "", "http://x", signer="Ann")
    assert subject == 'Review requested: "Prorate" (implementation v3)'
    assert text.startswith('Ann signed implementation v3 of "Prorate".')
    assert "http://x/r/prorate?tab=implementation\n" in text


@pytest.mark.parametrize(("target", "label"), TARGETS)
@pytest.mark.parametrize("section", [None, "intent"])
def test_plan_first_comment_uses_captured_target_and_revision(target, label, section):
    comment = {
        "id": 7,
        "author_name": "bot",
        "section": section,
        "body": "a" * 200,
        "target": target,
        "version": 3,
    }
    report = PLAN_FIRST | {"version": 8, "plan_revision": 6, "stage": "implementation"}
    assert notify.comment_text(report, comment, "http://x") == (
        f"bot commented on Prorate {label} › {section or 'report'}: "
        f"{'a' * 140} http://x/r/prorate?tab={target}#vf_c7"
    )


@pytest.mark.parametrize(("target", "label"), TARGETS)
@pytest.mark.parametrize(
    ("verdict", "phrase"), [("approve", "approved"), ("request_changes", "requested changes on")]
)
def test_plan_first_verdict_uses_captured_target_and_revision(target, label, verdict, phrase):
    row = {
        "reviewer_name": "Ann",
        "verdict": verdict,
        "version": 3,
        "summary": "Looks fine",
        "target": target,
    }
    report = PLAN_FIRST | {"version": 8, "plan_revision": 6, "stage": "plan"}
    assert notify.verdict_text(report, row, "http://x") == (
        f"Ann {phrase} Prorate {label}: Looks fine http://x/r/prorate?tab={target}"
    )


@pytest.mark.parametrize(("target", "label"), TARGETS)
def test_plan_first_slack_text_escapes_user_content(target, label):
    report = EVIL | {"workflow": "plan_first"}
    assert notify.report_pushed_text(report, 3, "http://x", target=target) == (
        f"Draft {label} in a&amp;b: &lt;T&gt; http://x/r/p?tab={target}"
    )
    assert notify.ready_for_review_text(report, 3, "<@U1>&", "http://x", target=target) == (
        f"Ready for review: &lt;T&gt; {label} (risk high), "
        f"signed by &lt;@U1&gt;&amp; http://x/r/p?tab={target}"
    )
    comment = {
        "id": 1,
        "author_name": "<@U1>&",
        "section": "a<b",
        "body": "<script><!channel>&",
        "target": target,
        "version": 3,
    }
    assert notify.comment_text(report, comment, "http://x") == (
        f"&lt;@U1&gt;&amp; commented on &lt;T&gt; {label} › a&lt;b: "
        f"&lt;script&gt;&lt;!channel&gt;&amp; http://x/r/p?tab={target}#vf_c1"
    )
    verdict = {
        "reviewer_name": "A&B",
        "verdict": "approve",
        "version": 3,
        "summary": "<script><!here>&",
        "target": target,
    }
    assert notify.verdict_text(report, verdict, "http://x") == (
        f"A&amp;B approved &lt;T&gt; {label}: "
        f"&lt;script&gt;&lt;!here&gt;&amp; http://x/r/p?tab={target}"
    )


@pytest.mark.parametrize(("target", "label"), TARGETS)
@pytest.mark.parametrize("judge", ["", "A free-form plan excerpt without headings."])
def test_plan_first_review_email_identifies_target_without_requiring_headings(target, label, judge):
    subject, text = notify.review_email(
        PLAN_FIRST, 3, judge, "http://x", signer="Alice", asker="Bob", target=target
    )
    assert subject == f'Review requested: "Prorate" ({label})'
    blocks = [
        f'Alice signed {label} of "Prorate". You were asked to review it by Bob.',
        "Project: billing-service · Risk: high",
    ]
    if judge:
        blocks.append(f"Judge this:\n{judge}")
    blocks += [
        f"Open it to comment, approve or request changes:\nhttp://x/r/prorate?tab={target}",
        "— Sent by verifair. Replies to this mail are not read.",
    ]
    assert text == "\n\n".join(blocks)


@pytest.mark.parametrize(("target", "label"), TARGETS)
@pytest.mark.parametrize("signer", [None, "Alice"])
def test_plan_first_email_when_reviewer_added_names_selected_target(target, label, signer):
    subject, text = notify.review_email(
        PLAN_FIRST, 3, "", "http://x", signer=signer, asker="Bob", added=True, target=target
    )
    signed = ", signed by Alice" if signer else ""
    assert subject == f'Review requested: "Prorate" ({label})'
    assert text.startswith(f'Bob asked you to review "Prorate" ({label}{signed}).\n\n')
    assert f"http://x/r/prorate?tab={target}\n" in text


@pytest.mark.parametrize("workflow", [None, "artifact"])
@pytest.mark.parametrize("target", ["plan", "implementation"])
def test_artifact_formatters_keep_exact_legacy_text_and_bare_links(workflow, target):
    report = REPORT if workflow is None else REPORT | {"workflow": workflow}
    assert notify.report_pushed_text(report, 3, "http://x", target=target) == (
        "Draft report v3 in billing-service: Prorate http://x/r/prorate"
    )
    assert notify.ready_for_review_text(report, 3, "Ann", "http://x", target=target) == (
        "Ready for review: Prorate v3 (risk high), signed by Ann http://x/r/prorate"
    )
    comment = {
        "id": 1,
        "author_name": "bot",
        "section": None,
        "body": "Hi",
        "target": target,
        "version": 3,
    }
    assert notify.comment_text(report, comment, "http://x") == (
        "bot commented on Prorate › report: Hi http://x/r/prorate#vf_c1"
    )
    verdict = {
        "reviewer_name": "Ann",
        "verdict": "approve",
        "version": 3,
        "summary": "Fine",
        "target": target,
    }
    assert notify.verdict_text(report, verdict, "http://x") == (
        "Ann approved Prorate v3: Fine http://x/r/prorate"
    )
    subject, text = notify.review_email(report, 3, "", "http://x", signer="Alice", target=target)
    assert subject == 'Review requested: "Prorate"'
    assert text == (
        'Alice signed v3 of "Prorate". You were asked to review it.\n\n'
        "Project: billing-service · Risk: high\n\n"
        "Open it to comment, approve or request changes:\nhttp://x/r/prorate\n\n"
        "— Sent by verifair. Replies to this mail are not read."
    )


def test_sent_emails_are_logged_without_addresses(monkeypatch, caplog):
    caplog.set_level("INFO", logger="verifair")

    class Server:
        def __init__(self, *a, **k): ...
        def __enter__(self):
            return self

        def __exit__(self, *a): ...
        def ehlo(self): ...
        def has_extn(self, name):
            return False

        def send_message(self, message, to_addrs): ...

    monkeypatch.setattr(notify.smtplib, "SMTP", Server)
    settings = replace(make_settings(None), smtp_url="smtp://mail:25", email_from="v@example.com")
    assert notify.send_email(settings, "r", "S", {"a@example.com": "x", "b@example.com": "y"})
    assert "email sent report=r recipients=2" in caplog.text and "a@example.com" not in caplog.text
