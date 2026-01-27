from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from src.control import Controller, SafetyLimits
from src.ocr import CopilotOCR
from src.windows import WindowsManager

from .multi_window_keepalive import MultiWindowChatKeepalive


def run_multi_window_keepalive_cycle(root: Optional[Path] = None) -> Dict[str, Any]:
	"""Convenience helper: run a single multi-window keepalive cycle.

	This constructs standard controller/ocr/window instances using the
	repository's config/ocr.json, then runs `MultiWindowChatKeepalive.cycle_once()`.

	It is intentionally light-weight and side-effect free beyond logs and
	mouse clicks, so other modules/agents can call it as a one-shot
	"orchestrator tick" without having to duplicate setup logic.
	"""
	base = Path(root) if root is not None else Path(__file__).resolve().parent.parent
	ocr_cfg_path = base / "config" / "ocr.json"

	try:
		import json as _json

		ocr_cfg = _json.loads(ocr_cfg_path.read_text(encoding="utf-8"))
	except Exception:
		ocr_cfg = {"enabled": True}

	limits = SafetyLimits(max_clicks_per_min=120, max_keys_per_min=240)
	ctrl = Controller(mouse_speed=0.25, limits=limits, mouse_control_seconds=6, mouse_release_seconds=3)
	win = WindowsManager()
	ocr = CopilotOCR(ocr_cfg, log=lambda m: None, debug_dir=base / "logs" / "ocr")

	# Respect shared controls ownership so this helper never competes with
	# Agent Mode or other workflows that currently own automation.
	try:
		from src.control_state import get_controls_state  # type: ignore
	except Exception:
		get_controls_state = None  # type: ignore
	if get_controls_state is not None:
		def _controls_gate() -> bool:
			try:
				st = get_controls_state(base) or {}
				owner = str(st.get("owner", "") or "")
				# Only act when no owner is recorded; if Agent Mode or another
				# workflow owns controls, yield and avoid sending input.
			except Exception:
				return True
			return not owner

		ctrl.set_window_gate(_controls_gate)

	ork = MultiWindowChatKeepalive(ctrl=ctrl, ocr=ocr, winman=win)
	return ork.cycle_once()  # summary dict
