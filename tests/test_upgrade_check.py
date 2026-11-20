import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "upgrade-check.py"


def load():
    spec = importlib.util.spec_from_file_location("upgrade_check", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_new_fields_are_allowed_but_old_values_must_stay():
    changed = load().changed
    before = {"id": "r", "versions": [{"body": "a", "plan": None}], "status": "approved"}

    assert changed(before, before | {"stage": "implementation"}) == []
    assert changed(before, {**before, "status": "unsigned"}) == ["status: 'approved' -> 'unsigned'"]
    assert changed(before, {"id": "r", "status": "approved"}) == ["versions: missing"]
    assert changed(before, {**before, "versions": []}) == ["versions: 1 items -> 0"]
    assert changed(before, {**before, "versions": [{"body": "b", "plan": None}]}) == [
        "versions[0].body: 'a' -> 'b'"
    ]
    assert changed(before, {**before, "versions": [None]}) == [
        "versions[0]: {'body': 'a', 'plan': None} -> None"
    ]
