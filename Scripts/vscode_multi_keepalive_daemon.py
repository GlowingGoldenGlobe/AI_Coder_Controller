from __future__ import annotations

"""Long-running multi-window VS Code chat keepalive daemon.

This script is designed to be launched as an independent workflow,
separate from Agent Mode. It periodically scans all visible VS Code
windows and nudges their chat UIs (via OCR + mouse clicks) so that
pending buttons like "Continue generating" do not block other agents.

Usage (from repo root, with venv):

    Scripts/python.exe Scripts/vscode_multi_keepalive_daemon.py --interval-s 6

Ctrl+C to stop.
"""

import argparse
import json
import time
from pathlib import Path

from src.control import Controller, SafetyLimits
from src.ocr import CopilotOCR
from src.windows import WindowsManager
from src.control_state import get_controls_state
from src.vscode_automation import MultiWindowChatKeepalive


def build_keepalive(root: Path) -> MultiWindowChatKeepalive:
    ocr_cfg_path = root / "config" / "ocr.json"
    try:
        ocr_cfg = json.loads(ocr_cfg_path.read_text(encoding="utf-8"))
    except Exception:
        ocr_cfg = {"enabled": True}

    limits = SafetyLimits(max_clicks_per_min=120, max_keys_per_min=240)
    ctrl = Controller(mouse_speed=0.25, limits=limits, mouse_control_seconds=6, mouse_release_seconds=3)

    # Respect shared controls_state.json: only act when no other owner is set.
    def _controls_free_gate() -> bool:
        try:
            st = get_controls_state(root) or {}
            owner = str(st.get("owner", "") or "")
            # Allow when no owner, or when running under the workflow_test
            # umbrella (which coordinates ownership itself). Yield otherwise.
            return (not owner) or (owner == "workflow_test")
        except Exception:
            # Fail-open on read errors.
            return True

    ctrl.set_window_gate(_controls_free_gate)
    win = WindowsManager()
    ocr = CopilotOCR(ocr_cfg, log=lambda m: None, debug_dir=root / "logs" / "ocr")
    return MultiWindowChatKeepalive(ctrl=ctrl, ocr=ocr, winman=win)


def main() -> int:
    parser = argparse.ArgumentParser(description="VS Code multi-window chat keepalive daemon")
    parser.add_argument("--interval-s", type=float, default=6.0, help="Seconds between keepalive cycles (default: 6.0)")
    parser.add_argument("--max-cycles", type=int, default=0, help="Optional max cycles before exit (0 = infinite)")
    args = parser.parse_args()

    root = Path(__file__).resolve().parent.parent
    keepalive = build_keepalive(root)

    cycles = 0
    interval = max(0.5, float(args.interval_s))
    print(f"[keepalive] Starting multi-window VS Code chat keepalive; interval={interval}s, max_cycles={args.max_cycles or 'inf'}")
    try:
        while True:
            summary = keepalive.cycle_once()
            cycles += 1
            print(f"[keepalive] cycle={cycles} windows={summary.get('windows_scanned')} actions={summary.get('actions_taken')}")
            if args.max_cycles and cycles >= args.max_cycles:
                break
            time.sleep(interval)
    except KeyboardInterrupt:
        print("[keepalive] Stopped by user (KeyboardInterrupt)")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
