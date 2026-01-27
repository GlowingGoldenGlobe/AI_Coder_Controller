from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from src.safety.action_safety import ActionSafety


def _write_controls_state(root: Path, data: dict) -> None:
    p = root / "config" / "controls_state.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _write_emergency_stop(root: Path, stopped: bool) -> None:
    p = root / "config" / "emergency_stop.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"stopped": stopped, "timestamp": time.time()}, indent=2), encoding="utf-8")


def test_action_safety_allows_when_free(tmp_path: Path) -> None:
    _write_controls_state(tmp_path, {"owner": "", "paused": False, "ts": time.time()})
    safety = ActionSafety(tmp_path)
    d = safety.composite_gate(owner="agent", stale_after_s=10.0)
    assert d.allowed is True


def test_action_safety_blocks_when_paused(tmp_path: Path) -> None:
    _write_controls_state(tmp_path, {"owner": "", "paused": True, "ts": time.time()})
    safety = ActionSafety(tmp_path)
    d = safety.composite_gate(owner="agent", stale_after_s=10.0)
    assert d.allowed is False
    assert d.reason == "controls_paused"


def test_action_safety_blocks_other_owner_when_fresh(tmp_path: Path) -> None:
    _write_controls_state(tmp_path, {"owner": "workflow_x", "paused": False, "ts": time.time()})
    safety = ActionSafety(tmp_path)
    d = safety.composite_gate(owner="agent", stale_after_s=10.0)
    assert d.allowed is False
    assert d.reason.startswith("controls_owned_by:")


def test_action_safety_allows_other_owner_when_stale(tmp_path: Path) -> None:
    _write_controls_state(tmp_path, {"owner": "workflow_x", "paused": False, "ts": time.time() - 999})
    safety = ActionSafety(tmp_path)
    d = safety.composite_gate(owner="agent", stale_after_s=10.0)
    assert d.allowed is True
    assert d.reason.startswith("controls_owner_stale:")


def test_action_safety_blocks_emergency_stop(tmp_path: Path) -> None:
    _write_controls_state(tmp_path, {"owner": "", "paused": False, "ts": time.time()})
    _write_emergency_stop(tmp_path, True)
    safety = ActionSafety(tmp_path)
    d = safety.composite_gate(owner="agent", stale_after_s=10.0)
    assert d.allowed is False
    assert d.reason == "emergency_stop"


@pytest.mark.parametrize("paused_val", ["true", "True", " TRUE "])
def test_action_safety_tolerates_string_paused(tmp_path: Path, paused_val: str) -> None:
    _write_controls_state(tmp_path, {"owner": "", "paused": paused_val, "ts": time.time()})
    safety = ActionSafety(tmp_path)
    d = safety.composite_gate(owner="agent", stale_after_s=10.0)
    assert d.allowed is False
    assert d.reason == "controls_paused"
