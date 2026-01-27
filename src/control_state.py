from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, Optional


STATE_FILENAME = "controls_state.json"


def _now() -> float:
	"""Internal helper for time; aids testing and staleness calculations."""
	return time.time()


def _state_path(root: Path) -> Path:
	return root / "config" / STATE_FILENAME


def get_controls_state(root: Path) -> Dict[str, Any]:
	"""Return the current shared controls state (owner/in_use), best effort."""
	path = _state_path(root)
	try:
		if path.exists():
			return json.loads(path.read_text(encoding="utf-8")) or {}
	except Exception:
		return {}
	return {}


def is_state_stale(state: Dict[str, Any], max_age_s: float) -> bool:
	"""Return True if a controls state snapshot is older than max_age_s.

	This never mutates the provided state; callers decide what to do with
	stale information. If no timestamp is present, the state is treated as
	non-stale so existing behavior is preserved.
	"""
	if max_age_s <= 0:
		return False
	try:
		ts = float(state.get("ts", 0.0) or 0.0)
	except Exception:
		return False
	if ts <= 0:
		return False
	age = _now() - ts
	return age > max_age_s


def set_controls_owner(root: Path, owner: Optional[str]) -> None:
	"""Best-effort: mark the current logical owner of controls.

	owner=None or empty string means "no one owns" (controls free).
	Other fields like in_control/remaining_s may be updated separately.
	"""
	path = _state_path(root)
	try:
		st: Dict[str, Any] = get_controls_state(root) or {}
		st["owner"] = (owner or "")
		st["in_use"] = bool(owner)
		st["ts"] = _now()
		path.parent.mkdir(parents=True, exist_ok=True)
		path.write_text(json.dumps(st, indent=2), encoding="utf-8")
	except Exception:
		pass


def update_control_window(root: Path, in_control: bool, remaining_s: float) -> None:
	"""Update current control-window status (active/release interval)."""
	path = _state_path(root)
	try:
		st: Dict[str, Any] = get_controls_state(root) or {}
		st["in_control_window"] = bool(in_control)
		st["control_remaining_s"] = float(remaining_s)
		st["ts"] = _now()
		path.parent.mkdir(parents=True, exist_ok=True)
		path.write_text(json.dumps(st, indent=2), encoding="utf-8")
	except Exception:
		pass
