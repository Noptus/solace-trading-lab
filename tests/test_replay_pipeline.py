"""Exercise the public replay journey, including business-level checks."""

import importlib.util
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "replay_demo", Path(__file__).resolve().parents[1] / "examples/replay_demo.py"
)
demo = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(demo)


def test_documented_replay_demo(capsys):
    demo.main()
    output = capsys.readouterr().out
    assert '"status": "PASS"' in output
    assert '"filled_quantity": 8' in output


def test_replay_oracle_rejects_missing_commands(tmp_path):
    path = tmp_path / "empty.jsonl"
    path.write_text("")
    with pytest.raises(ValueError, match="FIFO"):
        demo.run_once(path)
