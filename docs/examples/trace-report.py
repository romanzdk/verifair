import argparse
import json
import sys

from verifair.traces import validate_document

MAX_REQUEST_BYTES = 1_048_576


def read_text(path: str, limit: int) -> str:
    try:
        with open(path, "rb") as source:
            data = source.read(limit + 1)
    except OSError:
        raise ValueError("Could not read an input file.") from None
    if len(data) > limit:
        raise ValueError("Input file exceeds the byte limit.")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("Input files must be UTF-8.") from None


def positive_int(value: str) -> int:
    try:
        result = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a positive integer") from None
    if result < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a Verifair report request as JSON.")
    parser.add_argument("--project", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--risk", required=True, choices=("low", "medium", "high"))
    parser.add_argument("--body", required=True, metavar="PATH")
    parser.add_argument("--trace", metavar="PATH")
    parser.add_argument("--plan", metavar="PATH")
    parser.add_argument("--id")
    parser.add_argument("--expected-version", type=positive_int)
    args = parser.parse_args()
    if args.expected_version is not None and args.id is None:
        parser.error("--expected-version requires --id")

    try:
        request = {
            "project": args.project,
            "title": args.title,
            "risk": args.risk,
            "body": read_text(args.body, MAX_REQUEST_BYTES),
        }
        if args.plan is not None:
            request["plan"] = read_text(args.plan, MAX_REQUEST_BYTES)
        if args.trace is not None:
            trace = json.loads(read_text(args.trace, MAX_REQUEST_BYTES))
            request["trace"] = validate_document(trace).model_dump(mode="json", exclude_none=True)
        if args.id is not None:
            request["id"] = args.id
        if args.expected_version is not None:
            request["expected_version"] = args.expected_version
        output = json.dumps(request, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        if len(output.encode("utf-8")) + 1 > MAX_REQUEST_BYTES:
            raise ValueError("Request exceeds the byte limit.")
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        message = (
            str(error)
            if str(error)
            in {
                "Input file exceeds the byte limit.",
                "Input files must be UTF-8.",
                "Request exceeds the byte limit.",
            }
            else "Invalid input file or trace document."
        )
        parser.error(message)

    sys.stdout.write(output + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
