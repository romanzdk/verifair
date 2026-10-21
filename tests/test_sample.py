import json
import os
import subprocess
import sys
from pathlib import Path


def test_sample_uses_example_author(tmp_path):
    curl = tmp_path / "curl"
    curl.write_text(f"#!{sys.executable}\nimport json, sys\nprint(json.dumps(sys.argv[1:]))\n")
    curl.chmod(0o755)
    script = Path(__file__).resolve().parents[1] / "scripts" / "push-sample.sh"
    result = subprocess.run(
        ["bash", str(script)],
        env=os.environ
        | {
            "VERIFAIR_API_TOKEN": "example",
            "VERIFAIR_URL": "http://localhost:8000",
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
        },
        check=True,
        capture_output=True,
        text=True,
    )
    args = json.loads(result.stdout)
    payload = json.loads(args[args.index("-d") + 1])
    assert payload["author"] == "Alice"
