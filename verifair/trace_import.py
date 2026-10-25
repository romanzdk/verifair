import argparse
import importlib
import os
import sys
import tempfile
from pathlib import Path

from verifair.trace_importers.common import ImportOptions, TraceImportError, check_inputs
from verifair.traces import canonical_json, validate_document

_FORMATS = {
    "claude-stream-json": "claude",
    "codex-exec-jsonl": "codex",
    "opencode-v2-export": "opencode",
    "pi-session-jsonl": "pi",
}


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise TraceImportError("Invalid import arguments. Use --help for supported options.")


def write_output(path: Path, content: str) -> None:
    temporary = None
    try:
        fd, temporary = tempfile.mkstemp(prefix=".trace-import-", dir=path.parent)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    except FileExistsError:
        raise TraceImportError("Output already exists; choose a new output path.") from None
    except OSError:
        raise TraceImportError("Cannot write the local output file.") from None
    finally:
        if temporary is not None:
            os.unlink(temporary)


def main(argv: list[str] | None = None) -> int:
    parser = _Parser(
        description="Convert explicitly supplied local captures. No discovery or upload.",
        epilog=(
            "Inspect local output before submission. Redaction is incomplete; even names and "
            "timestamps may be sensitive. --source-version is your claim and is never inferred."
        ),
    )
    parser.add_argument("--format", choices=_FORMATS, required=True)
    parser.add_argument("--input", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-version")
    parser.add_argument("--include-details", action="store_true")
    parser.add_argument("--last-events", type=int)
    parser.add_argument("--leaf")
    try:
        args = parser.parse_args(argv)
        options = ImportOptions(
            args.include_details, args.source_version, args.last_events, args.leaf
        )
        paths = tuple(args.input)
        check_inputs(paths)
        try:
            converter = importlib.import_module(f"verifair.trace_importers.{_FORMATS[args.format]}")
        except ImportError:
            raise TraceImportError(
                "Selected format adapter is not available in this build."
            ) from None
        document = validate_document(converter.convert(paths, options))
        write_output(args.output, canonical_json(document))
        return 0
    except TraceImportError as error:
        print(f"Import failed: {error}", file=sys.stderr)
    except OSError, ValueError, TypeError, RecursionError:
        print("Import failed: invalid local capture or normalized document.", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
