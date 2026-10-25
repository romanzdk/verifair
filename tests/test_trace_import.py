import importlib
import json
import os
import stat
import subprocess
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

SECRET = "SECRET-MUST-NOT-APPEAR"
SYNTHETIC = """
import runpy, sys, types
from verifair.trace_importers.common import (
    ReadBudget, read_json, read_jsonl, sanitize_text, finalize,
)
from verifair.traces import TraceRun, TraceEvent
module = types.ModuleType("verifair.trace_importers.codex")
def convert(paths, options):
    budget = ReadBudget()
    events = []
    for path in paths:
        if path.suffix == ".jsonl":
            records = read_jsonl(path, budget=budget)
        else:
            records = [read_json(path, budget=budget)]
        for record in records:
            text, truncated = sanitize_text(record.get("detail"), options)
            events.append(TraceEvent(id=f"e{len(events)}", run_id="r", ordinal=len(events)+1,
                kind="tool", title="Tool", status="unknown", input=text, truncated=truncated))
    return finalize([TraceRun(id="r", agent="other", origin="recorded",
        source_format="synthetic", source_version=options.source_version,
        coverage="partial", warnings=[])], events, options)
module.convert = convert
sys.modules[module.__name__] = module
runpy.run_module("verifair.trace_import", run_name="__main__")
"""


def cli(*args, synthetic=False, script=None):
    code = script or (SYNTHETIC if synthetic else None)
    command = (
        [sys.executable, "-c", code] if code else [sys.executable, "-m", "verifair.trace_import"]
    )
    return subprocess.run(
        command + list(map(str, args)),
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        timeout=10,
    )


@pytest.fixture
def common():
    return importlib.import_module("verifair.trace_importers.common")


def run(id="r", **kwargs):
    from verifair.traces import TraceRun

    return TraceRun(
        id=id,
        agent="other",
        origin="recorded",
        source_format="synthetic",
        coverage="unknown",
        warnings=[],
        **kwargs,
    )


def event(id="e", run_id="r", ordinal=1, **kwargs):
    from verifair.traces import TraceEvent

    return TraceEvent(
        id=id, run_id=run_id, ordinal=ordinal, kind="tool", title="Tool", status="unknown", **kwargs
    )


def test_cli_help_without_provider_modules():
    result = cli("--help")
    assert result.returncode == 0, result.stderr
    for name in (
        "claude-stream-json",
        "codex-exec-jsonl",
        "opencode-v2-export",
        "pi-session-jsonl",
    ):
        assert name in result.stdout
    assert "inspect" in result.stdout.lower()


@pytest.mark.parametrize(
    "args",
    [
        (),
        ("--format", "unknown"),
        ("--format", "codex-exec-jsonl", "--output", "out"),
        ("--format", "codex-exec-jsonl", "--input", "missing"),
        (
            "--format",
            "codex-exec-jsonl",
            "--input",
            "missing",
            "--output",
            "out",
            "--last-events",
            "0",
        ),
    ],
)
def test_cli_rejects_missing_arguments_and_unknown_format(args):
    result = cli(*args)
    assert result.returncode != 0
    assert "Traceback" not in result.stderr


def test_argument_errors_do_not_echo_secrets():
    result = cli("--format", SECRET)
    assert result.returncode != 0
    assert SECRET not in result.stdout + result.stderr


def test_unavailable_provider_has_safe_actionable_error(tmp_path):
    source = tmp_path / "input"
    source.write_text("{}")
    result = cli(
        "--format",
        "pi-session-jsonl",
        "--input",
        source,
        "--output",
        tmp_path / "out",
        script=(
            'import sys, runpy; sys.modules["verifair.trace_importers.pi"] = None; '
            'runpy.run_module("verifair.trace_import", run_name="__main__")'
        ),
    )
    assert result.returncode != 0
    assert "not available" in result.stderr
    assert "Traceback" not in result.stderr


def test_synthetic_cli_writes_canonical_private_output_without_discovery(tmp_path):
    source = tmp_path / "input.json"
    source.write_text(json.dumps({"detail": SECRET}))
    (tmp_path / "unselected.json").write_text("invalid " + SECRET)
    output = tmp_path / "out.json"
    result = cli(
        "--format",
        "codex-exec-jsonl",
        "--input",
        source,
        "--output",
        output,
        "--source-version",
        "test-1",
        synthetic=True,
    )
    assert result.returncode == 0, result.stderr
    data = output.read_text()
    assert SECRET not in data + result.stdout + result.stderr
    document = json.loads(data)
    assert document["runs"][0]["source_version"] == "test-1"
    assert "input" not in document["events"][0]
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert data == json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "input.json",
        "out.json",
        "unselected.json",
    ]


@pytest.mark.parametrize(
    "suffix,payload",
    [
        (".json", b"\xff"),
        (".json", b"{bad"),
        (".jsonl", b"{}\n{bad"),
        (".jsonl", b"{}\n\xff"),
        (".json", b"[" * 100 + b"]" * 100),
    ],
)
def test_cli_parse_errors_hide_source_payload(tmp_path, suffix, payload):
    source = tmp_path / (SECRET + suffix)
    source.write_bytes(payload + SECRET.encode())
    output = tmp_path / "out"
    result = cli(
        "--format", "codex-exec-jsonl", "--input", source, "--output", output, synthetic=True
    )
    assert result.returncode != 0
    assert SECRET not in result.stdout + result.stderr
    assert "Traceback" not in result.stderr
    assert not output.exists()


def test_cli_never_overwrites_existing_output(tmp_path):
    source = tmp_path / "in.json"
    source.write_text("{}")
    output = tmp_path / "out"
    output.write_text(SECRET)
    result = cli(
        "--format", "codex-exec-jsonl", "--input", source, "--output", output, synthetic=True
    )
    assert result.returncode != 0
    assert output.read_text() == SECRET
    assert SECRET not in result.stdout + result.stderr


def test_options_are_immutable_and_reject_invalid_selection(common):
    options = common.ImportOptions()
    with pytest.raises(FrozenInstanceError):
        options.leaf = "other"
    for value in (0, 1001, True, -1):
        with pytest.raises(common.TraceImportError):
            common.ImportOptions(last_events=value)


@pytest.mark.parametrize(
    "reader,payload",
    [
        ("read_json", b"{bad"),
        ("read_json", b"\xff"),
        ("read_jsonl", b"{}\n{bad"),
        ("read_jsonl", b"[]"),
        ("read_json", b'{"n":NaN}'),
        ("read_json", b"[" * 65 + b"]" * 65),
    ],
)
def test_readers_reject_malformed_or_unsupported_input(common, tmp_path, reader, payload):
    source = tmp_path / SECRET
    source.write_bytes(payload)
    with pytest.raises(common.TraceImportError) as error:
        value = getattr(common, reader)(source)
        if reader == "read_jsonl":
            list(value)
    assert SECRET not in str(error.value)


def test_readers_bound_records_and_aggregate_actual_bytes(common, tmp_path, monkeypatch):
    source = tmp_path / "input"
    source.write_text('{"text":"1234567890"}\n')
    monkeypatch.setattr(common, "MAX_RECORD_BYTES", 10)
    with pytest.raises(common.TraceImportError):
        list(common.read_jsonl(source))
    monkeypatch.setattr(common, "MAX_INPUT_BYTES", 30)
    budget = common.ReadBudget()
    assert common.read_json(source, budget=budget) == {"text": "1234567890"}
    with pytest.raises(common.TraceImportError):
        common.read_json(source, budget=budget)


@pytest.mark.parametrize("kind", ["directory", "fifo", "symlink"])
def test_readers_reject_nonregular_files_without_blocking(common, tmp_path, kind):
    source = tmp_path / "input"
    if kind == "directory":
        source.mkdir()
    elif kind == "fifo":
        os.mkfifo(source)
    else:
        source.symlink_to("/dev/null")
    with pytest.raises(common.TraceImportError):
        common.read_json(source)


def test_input_count_and_combined_size_rejected(common, tmp_path, monkeypatch):
    source = tmp_path / "input"
    source.write_text("{}")
    for paths in ((), (source,) * 17):
        with pytest.raises(common.TraceImportError):
            common.check_inputs(paths)
    monkeypatch.setattr(common, "MAX_INPUT_BYTES", 3)
    with pytest.raises(common.TraceImportError):
        common.check_inputs((source, source))


def test_actual_budget_catches_growth_after_stat(common, tmp_path, monkeypatch):
    source = tmp_path / "input"
    source.write_text('{"long":"12345678901234567890"}')
    monkeypatch.setattr(common, "MAX_INPUT_BYTES", 8)
    fake_stat = type("Stat", (), {"st_mode": stat.S_IFREG, "st_size": 1})()
    monkeypatch.setattr(common.os, "fstat", lambda fd: fake_stat)
    with pytest.raises(common.TraceImportError):
        common.read_json(source)


@pytest.mark.parametrize(
    "detail", ["private", {"text": SECRET}, [{"type": "image", "data": SECRET}], b"private"]
)
def test_details_are_opt_in_and_unknown_objects_are_dropped(common, detail):
    assert common.sanitize_text(detail, common.ImportOptions()) == (None, False)
    if not isinstance(detail, str):
        assert common.sanitize_text(detail, common.ImportOptions(include_details=True)) == (
            None,
            False,
        )


@pytest.mark.parametrize(
    "key", ["authorization", "PASSWORD", "token", "api_key", "apiKey", "secret"]
)
def test_optional_text_redacts_credentials_before_truncation(common, key):
    text, truncated = common.sanitize_text(
        f'{key}="{SECRET}" Bearer {SECRET} visible', common.ImportOptions(include_details=True)
    )
    assert SECRET not in text
    assert "visible" in text
    assert not truncated


def test_optional_text_bounds_and_labels_truncation(common):
    text, truncated = common.sanitize_text(
        "a\x00" + "x" * 16001, common.ImportOptions(include_details=True)
    )
    assert truncated
    assert len(text) <= 16000
    assert text.endswith("[truncated]")
    assert "\x00" not in text


def test_finalize_selects_before_document_limits_and_warns(common):
    events = [event(id=f"e{i}", ordinal=i + 1, output="x" * 16000) for i in range(1002)]
    document = common.finalize([run()], events, common.ImportOptions(last_events=2))
    assert [e.id for e in document.events] == ["e1000", "e1001"]
    assert document.runs[0].coverage == "partial"
    assert any("selection" in w.lower() for w in document.runs[0].warnings)


def test_finalize_preserves_surviving_parentage_and_warns_for_dropped_references(common):
    runs = [run(id="root"), run(id="child", parent_run_id="root", parent_event_id="old")]
    events = [
        event(id="old", run_id="root"),
        event(id="keep", run_id="root", ordinal=2),
        event(id="child1", run_id="child"),
        event(id="child2", run_id="child", ordinal=2, parent_id="child1"),
    ]
    document = common.finalize(runs, events, common.ImportOptions(last_events=3))
    assert document.runs[1].parent_run_id == "root"
    assert document.runs[1].parent_event_id is None
    assert document.events[-1].parent_id == "child1"
    assert any("reference" in w.lower() for w in document.runs[1].warnings)
    reduced = common.finalize(runs, events, common.ImportOptions(last_events=1))
    assert [r.id for r in reduced.runs] == ["child"]
    assert reduced.runs[0].parent_run_id is None
    assert reduced.events[0].parent_id is None


def test_finalize_cleans_unavailable_references_without_mutating_inputs(common):
    source_run = run(parent_run_id="missing")
    source_event = event(parent_id="missing")
    document = common.finalize([source_run], [source_event], common.ImportOptions())
    assert document.events[0].parent_id is None
    assert document.runs[0].parent_run_id is None
    assert source_run.parent_run_id == "missing"
    assert source_event.parent_id == "missing"
    assert document.runs[0].warnings


@pytest.mark.parametrize(
    "events", [[], [event(id=f"e{i}", ordinal=i + 1, output="x" * 16000) for i in range(40)]]
)
def test_finalize_rejects_empty_and_oversized_document_safely(common, events):
    with pytest.raises(common.TraceImportError):
        common.finalize([run()], events, common.ImportOptions(include_details=True))


def test_millisecond_timestamps_remain_exact(common):
    document = common.finalize(
        [run()], [event(started_at="2026-10-25T12:34:56.123Z")], common.ImportOptions()
    )
    assert document.events[0].started_at.microsecond == 123000


def test_atomic_output_race_cannot_overwrite_winner(tmp_path, monkeypatch):
    module = importlib.import_module("verifair.trace_import")
    output = tmp_path / "out"
    real_link = os.link

    def competing_link(source, destination):
        Path(destination).write_text(SECRET)
        return real_link(source, destination)

    monkeypatch.setattr(module.os, "link", competing_link)
    with pytest.raises(module.TraceImportError):
        module.write_output(output, "{}")
    assert output.read_text() == SECRET
    assert [p.name for p in tmp_path.iterdir()] == ["out"]


def test_cli_rejects_aggregate_file_and_record_limits(tmp_path):
    source = tmp_path / "input.jsonl"
    source.write_bytes(b'{"detail":"' + b"x" * (1024 * 1024) + b'"}\n')
    result = cli(
        "--format",
        "codex-exec-jsonl",
        "--input",
        source,
        "--output",
        tmp_path / "out",
        synthetic=True,
    )
    assert result.returncode != 0
    assert "record" in result.stderr.lower()
    source.write_bytes(b"{}")
    result = cli(
        "--format",
        "codex-exec-jsonl",
        *[part for _ in range(17) for part in ("--input", source)],
        "--output",
        tmp_path / "out",
        synthetic=True,
    )
    assert result.returncode != 0
    with source.open("wb") as stream:
        stream.truncate(64 * 1024 * 1024 + 1)
    result = cli(
        "--format",
        "codex-exec-jsonl",
        "--input",
        source,
        "--output",
        tmp_path / "out",
        synthetic=True,
    )
    assert result.returncode != 0
    assert "byte limit" in result.stderr
    assert not (tmp_path / "out").exists()


def test_cli_rejects_oversized_output_but_explicit_selection_reduces_it(tmp_path):
    source = tmp_path / "input.jsonl"
    source.write_text((json.dumps({"detail": "x" * 16000}) + "\n") * 40)
    output = tmp_path / "out"
    args = (
        "--format",
        "codex-exec-jsonl",
        "--input",
        source,
        "--output",
        output,
        "--include-details",
    )
    result = cli(*args, synthetic=True)
    assert result.returncode != 0
    assert not output.exists()
    result = cli(*args, "--last-events", "1", synthetic=True)
    assert result.returncode == 0, result.stderr
    document = json.loads(output.read_text())
    assert len(document["events"]) == 1
    assert document["runs"][0]["coverage"] == "partial"


def test_cli_does_not_discover_start_agents_or_connect_network(tmp_path):
    source = tmp_path / "input.json"
    source.write_text("{}")
    guard = """
import os, socket, subprocess
from pathlib import Path
def forbidden(*args, **kwargs):
    raise RuntimeError("forbidden side effect")
os.scandir = socket.socket = socket.create_connection = subprocess.Popen = forbidden
Path.glob = Path.rglob = Path.iterdir = forbidden
"""
    command = SYNTHETIC.replace("runpy.run_module(", guard + "\nrunpy.run_module(")
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            command,
            "--format",
            "codex-exec-jsonl",
            "--input",
            str(source),
            "--output",
            str(tmp_path / "out"),
        ],
        capture_output=True,
        text=True,
        timeout=10,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "out").exists()


def test_depth_limit_ignores_brackets_inside_strings(common, tmp_path):
    source = tmp_path / "input"
    source.write_text(json.dumps({"text": "[" * 100}))
    assert common.read_json(source) == {"text": "[" * 100}


def test_output_existing_symlink_is_not_followed(tmp_path):
    module = importlib.import_module("verifair.trace_import")
    target = tmp_path / "target"
    target.write_text(SECRET)
    output = tmp_path / "out"
    output.symlink_to(target)
    with pytest.raises(module.TraceImportError):
        module.write_output(output, "{}")
    assert target.read_text() == SECRET
    assert output.is_symlink()


def test_sanitation_is_stable_when_adapter_and_finalize_both_apply_it(common):
    options = common.ImportOptions(include_details=True)
    text, _ = common.sanitize_text('password="sensitive"', options)
    document = common.finalize([run()], [event(input=text)], options)
    assert document.events[0].input == "password=[REDACTED]"


def test_unknown_version_and_selection_warnings_fit_without_losing_selection(common):
    from verifair.traces import TraceRun

    source_run = TraceRun(
        id="r",
        agent="other",
        origin="recorded",
        source_format="synthetic",
        coverage="unknown",
        warnings=["source warning"] * 20,
    )
    document = common.finalize([source_run], [event()], common.ImportOptions(last_events=1))
    assert len(document.runs[0].warnings) <= 20
    assert any("selection" in w for w in document.runs[0].warnings)
    assert any("version" in w for w in document.runs[0].warnings)


@pytest.mark.parametrize(
    "detail",
    [
        "Authorization: Basic c3ludGhldGljOnNlY3JldA==\r\nVisible: retained",
        "authorization=Basic c3ludGhldGljOnNlY3JldA==\nVisible: retained",
        "AUTHORIZATION:\tNegotiate c3ludGhldGljOnNlY3JldA==\nVisible: retained",
        'Authorization: Digest username="synthetic", response="c3ludGhldGljOnNlY3JldA=="'
        "\nVisible: retained",
        "Authorization: CustomScheme c3ludGhldGljOnNlY3JldA== extra-credential\nVisible: retained",
        '{"Authorization": "Basic c3ludGhldGljOnNlY3JldA==", "visible": "retained"}',
        "{'authorization': 'Negotiate c3ludGhldGljOnNlY3JldA==', 'visible': 'retained'}",
        '"Authorization: Basic c3ludGhldGljOnNlY3JldA=="\nVisible: retained',
        "'Authorization: Negotiate c3ludGhldGljOnNlY3JldA=='\nVisible: retained",
        "Authorization: Bearer c3ludGhldGljOnNlY3JldA==\nVisible: retained",
    ],
    ids=[
        "basic-header",
        "basic-assignment",
        "negotiate",
        "digest",
        "custom-scheme",
        "json",
        "quoted-assignment",
        "quoted-header",
        "single-quoted-header",
        "bearer",
    ],
)
def test_authorization_redacts_complete_values_through_finalize(common, detail):
    options = common.ImportOptions(include_details=True)
    text, truncated = common.sanitize_text(detail, options)
    assert "c3ludGhldGljOnNlY3JldA==" not in text
    assert "extra-credential" not in text
    assert "retained" in text
    assert not truncated
    assert common.sanitize_text(text, options) == (text, False)
    document = common.finalize([run()], [event(input=text)], options)
    assert document.events[0].input == text
    raw_document = common.finalize([run()], [event(input=detail)], options)
    assert raw_document.events[0].input == text
    assert common.sanitize_text(detail, common.ImportOptions()) == (None, False)


def test_authorization_is_removed_before_output_truncation(common, tmp_path):
    credential = "synthetic-private-credential"
    source = tmp_path / "input.json"
    source.write_text(
        json.dumps(
            {"detail": "Authorization: CustomScheme " + credential * 700 + "\n" + "v" * 16001}
        )
    )
    output = tmp_path / "out"
    result = cli(
        "--format",
        "codex-exec-jsonl",
        "--input",
        source,
        "--output",
        output,
        "--include-details",
        synthetic=True,
    )
    assert result.returncode == 0, result.stderr
    text = output.read_text()
    assert credential not in text + result.stdout + result.stderr
    imported_event = json.loads(text)["events"][0]
    assert imported_event["input"].startswith("Authorization: [REDACTED]\n")
    assert imported_event["input"].endswith("[truncated]")
    assert len(imported_event["input"]) == 16000
    assert imported_event["truncated"] is True


@pytest.mark.parametrize(
    "detail",
    [
        '{"authorization":\n  "Basic c3ludGhldGljOnNlY3JldA==", "visible": "retained"}',
        '{"authorization": \n  "Basic c3ludGhldGljOnNlY3JldA==", "visible": "retained"}',
        '{"authorization"\n:\n  "CustomScheme c3ludGhldGljOnNlY3JldA==", "visible": "retained"}',
        "authorization=\t\r\n 'Negotiate c3ludGhldGljOnNlY3JldA=='\r\nVisible: retained",
    ],
    ids=["exact-review-repro", "space-before-newline", "newline-before-delimiter", "crlf-quoted"],
)
def test_authorization_quoted_values_after_newline_are_redacted(common, detail):
    options = common.ImportOptions(include_details=True)
    text, truncated = common.sanitize_text(detail, options)
    assert "c3ludGhldGljOnNlY3JldA==" not in text
    assert "retained" in text
    assert not truncated
    assert common.sanitize_text(text, options) == (text, False)
    assert common.finalize([run()], [event(input=detail)], options).events[0].input == text
    assert common.finalize([run()], [event(input=text)], options).events[0].input == text
    assert common.sanitize_text(detail, common.ImportOptions()) == (None, False)


def test_empty_authorization_header_does_not_consume_next_header(common):
    options = common.ImportOptions(include_details=True)
    text, truncated = common.sanitize_text("Authorization: \r\nVisible: retained", options)
    assert "\r\nVisible: retained" in text
    assert not truncated
    assert common.sanitize_text(text, options) == (text, False)


def test_authorization_nbsp_exact_review_repro(common):
    detail = "Authorization:\u00a0Basic synthetic-credential-for-nbsp-probe\nVisible: retained"
    options = common.ImportOptions(include_details=True)
    text, truncated = common.sanitize_text(detail, options)
    assert "synthetic-credential-for-nbsp-probe" not in text
    assert text.endswith("\nVisible: retained")
    assert not truncated
    assert common.sanitize_text(text, options) == (text, False)
    assert common.finalize([run()], [event(input=detail)], options).events[0].input == text
    assert common.finalize([run()], [event(input=text)], options).events[0].input == text
    assert common.sanitize_text(detail, common.ImportOptions()) == (None, False)


@pytest.mark.parametrize(
    "space",
    [" ", "\t", "\u00a0", "\u1680", "\u2007", "\u202f", "\u205f", "\u3000", "\x1f"],
    ids=["space", "tab", "nbsp", "ogham", "figure", "narrow-nbsp", "medium", "ideographic", "unit"],
)
def test_authorization_unicode_whitespace_around_key_delimiter_and_value(common, space):
    detail = f"{space}Authorization{space}={space}Negotiate synthetic-credential\nVisible: retained"
    options = common.ImportOptions(include_details=True)
    text, truncated = common.sanitize_text(detail, options)
    assert "synthetic-credential" not in text
    assert text.endswith("\nVisible: retained")
    assert not truncated
    assert common.sanitize_text(text, options) == (text, False)
    assert common.finalize([run()], [event(input=detail)], options).events[0].input == text


@pytest.mark.parametrize(
    "boundary",
    ["\n", "\r", "\r\n", "\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029"],
    ids=["lf", "cr", "crlf", "vt", "ff", "file", "group", "record", "nel", "line", "paragraph"],
)
def test_authorization_line_boundaries_preserve_next_header(common, boundary):
    options = common.ImportOptions(include_details=True)
    empty_header = f"Authorization:\u00a0{boundary}Visible: retained"
    assert common.sanitize_text(empty_header, options) == (empty_header, False)
    detail = (
        f"Authorization{boundary}:\u00a0CustomScheme synthetic-credential"
        f"{boundary}Visible: retained"
    )
    text, truncated = common.sanitize_text(detail, options)
    assert "synthetic-credential" not in text
    assert text.endswith(boundary + "Visible: retained")
    assert not truncated
    assert common.sanitize_text(text, options) == (text, False)
    assert common.finalize([run()], [event(input=detail)], options).events[0].input == text


@pytest.mark.parametrize(
    "boundary",
    ["\n", "\r", "\r\n", "\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029"],
    ids=["lf", "cr", "crlf", "vt", "ff", "file", "group", "record", "nel", "line", "paragraph"],
)
def test_authorization_unicode_line_whitespace_quoted_fallback(common, boundary):
    detail = (
        '{"authorization"'
        + boundary
        + ":"
        + boundary
        + '"Basic synthetic-credential", "visible": "retained"}'
    )
    options = common.ImportOptions(include_details=True)
    text, truncated = common.sanitize_text(detail, options)
    assert "synthetic-credential" not in text
    assert '"visible": "retained"' in text
    assert not truncated
    assert common.sanitize_text(text, options) == (text, False)
    assert common.finalize([run()], [event(input=detail)], options).events[0].input == text
