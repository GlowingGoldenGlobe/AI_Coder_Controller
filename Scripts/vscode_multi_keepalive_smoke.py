from __future__ import annotations

"""Smoke test for multi-window VS Code chat keepalive.

Usage (from repo root, with venv):

    Scripts/python.exe Scripts/vscode_multi_keepalive_smoke.py

This will:
- Construct Controller + WindowsManager + CopilotOCR.
- Build a MultiWindowChatKeepalive orchestrator.
- Run a single cycle over all visible VS Code windows and print a summary.

The orchestrator uses image-based UI detection only for decisions: it
captures the chat ROI image and detects button-like elements/templates,
but does not inspect OCR text to decide when to click or send messages.
"""

import json
from pathlib import Path

from src.control import Controller, SafetyLimits
from src.ocr import CopilotOCR
from src.windows import WindowsManager
from src.vscode_automation import MultiWindowChatKeepalive


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    ocr_cfg_path = root / "config" / "ocr.json"

    try:
        import json as _json

        ocr_cfg = _json.loads(ocr_cfg_path.read_text(encoding="utf-8"))
    except Exception:
        ocr_cfg = {"enabled": True}

    limits = SafetyLimits(max_clicks_per_min=120, max_keys_per_min=240)
    ctrl = Controller(mouse_speed=0.25, limits=limits, mouse_control_seconds=6, mouse_release_seconds=3)
    # Only act when no other workflow owns controls.
    try:
        from src.control_state import get_controls_state  # type: ignore
    except Exception:
        get_controls_state = None  # type: ignore
    if get_controls_state is not None:
        def _controls_gate() -> bool:
            try:
                st = get_controls_state(root) or {}
                owner = str(st.get("owner", "") or "")
                return not owner
            except Exception:
                return True
        ctrl.set_window_gate(_controls_gate)
    win = WindowsManager()
    ocr = CopilotOCR(ocr_cfg, log=lambda m: None, debug_dir=root / "logs" / "ocr")

    keepalive = MultiWindowChatKeepalive(ctrl=ctrl, ocr=ocr, winman=win)
    summary = keepalive.cycle_once()
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
