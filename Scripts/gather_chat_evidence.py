from __future__ import annotations
import json
import time
from pathlib import Path

from src.control import Controller, SafetyLimits
from src.vsbridge import VSBridge
from src.windows import WindowsManager
from src.ocr import CopilotOCR


def write_report(root: Path, report: dict) -> Path:
    out_dir = root / "logs" / "tests"
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    out_path = out_dir / f"chat_evidence_{ts}.json"
    out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return out_path


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    rules_path = root / "config" / "policy_rules.json"
    ocr_cfg_path = root / "config" / "ocr.json"
    try:
        rules = json.loads(rules_path.read_text(encoding="utf-8"))
    except Exception:
        rules = {}
    vs_cfg = rules.get("vsbridge", {}) or {}

    limits = SafetyLimits(max_clicks_per_min=120, max_keys_per_min=240)
    ctrl = Controller(mouse_speed=0.25, limits=limits, mouse_control_seconds=6, mouse_release_seconds=3)
    # Respect shared controls_state ownership when possible.
    try:
        from src.control_state import get_controls_state  # type: ignore
    except Exception:
        get_controls_state = None  # type: ignore
    if get_controls_state is not None:
        def _controls_gate() -> bool:
            try:
                st = get_controls_state(root) or {}
                owner = str(st.get("owner", "") or "")
                # Allow when no owner or when running under workflow_test; yield
                # when another independent workflow owns controls.
                return (not owner) or (owner == "workflow_test")
            except Exception:
                return True
        ctrl.set_window_gate(_controls_gate)
    win = WindowsManager()
    log = lambda m: None
    vs = VSBridge(ctrl, log, winman=win, delay_ms=int(vs_cfg.get("delay_ms", 300)), dry_run=bool(vs_cfg.get("dry_run", False)))

    try:
        ocr_cfg = json.loads(ocr_cfg_path.read_text(encoding="utf-8"))
    except Exception:
        ocr_cfg = {"enabled": True}
    ocr_debug = root / "logs" / "ocr"
    ocr = CopilotOCR(ocr_cfg, log=lambda m: None, debug_dir=ocr_debug)

    vs.focus_vscode_window()
    time.sleep(0.35)
    vs.focus_copilot_chat_view()
    settle_ms = int((ocr_cfg or {}).get("chat_settle_ms", 900))
    time.sleep(max(0.6, settle_ms / 1000.0))

    res = ocr.capture_chat_text(save_dir=ocr_debug)
    elems = (res.get("elements") or []) if isinstance(res, dict) else []

    # Prefer any explicit OCR text if available; otherwise fall back to a
    # compact stringified preview of the first few elements so callers always
    # have human-readable evidence fields.
    text = ""
    if isinstance(res, dict):
        t = res.get("text")
        if isinstance(t, str):
            text = t
    if not text and elems:
        try:
            text = " ".join(str(e) for e in elems[:3])
        except Exception:
            text = str(elems[:3])

    report = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "elements_count": len(elems),
        "preview_elements": repr((elems or [])[:6]),
        "image_path": str(res.get("image_path") or ""),
        "chars": len(text),
        "preview": text[:200],
    }
    outp = write_report(root, report)
    print("Chat evidence:", outp)
    print(json.dumps({k: report[k] for k in ("chars","preview","image_path")}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
