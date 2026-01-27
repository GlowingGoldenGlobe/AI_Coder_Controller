from __future__ import annotations
import json
import sys
import time
from pathlib import Path

from src.control import Controller, SafetyLimits
from src.vsbridge import VSBridge
from src.windows import WindowsManager
from src.ocr import CopilotOCR
from src.jsonlog import JsonActionLogger


def write_report(root: Path, report: dict) -> Path:
    out_dir = root / "logs" / "tests"
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    out_path = out_dir / f"navigation_test_{ts}.json"
    out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return out_path


def main():
    root = Path(__file__).resolve().parent.parent
    # Load config for VSBridge pacing and OCR
    rules_path = root / "config" / "policy_rules.json"
    ocr_cfg_path = root / "config" / "ocr.json"
    try:
        rules = json.loads(rules_path.read_text(encoding="utf-8"))
    except Exception:
        rules = {}
    vs_cfg = rules.get("vsbridge", {}) or {}
    # Controller with optional typing interval
    limits = SafetyLimits(max_clicks_per_min=120, max_keys_per_min=240)
    ctrl = Controller(mouse_speed=0.25, limits=limits, mouse_control_seconds=8, mouse_release_seconds=4)
    # Respect shared controls_state ownership when invoked alongside other workflows.
    try:
        from src.control_state import get_controls_state  # type: ignore
    except Exception:
        get_controls_state = None  # type: ignore
    if get_controls_state is not None:
        def _controls_gate() -> bool:
            try:
                st = get_controls_state(root) or {}
                owner = str(st.get("owner", "") or "")
                # Allow when no owner or when invoked under the workflow_test
                # umbrella; yield when another independent workflow (e.g., Agent
                # Mode) owns controls.
                return (not owner) or (owner == "workflow_test")
            except Exception:
                return True
        ctrl.set_window_gate(_controls_gate)
    try:
        kb_cfg = rules.get("keyboard", {}) or {}
        ti_ms = float(kb_cfg.get("type_interval_ms", 8))
        ctrl.type_interval = max(0.0, ti_ms / 1000.0)
    except Exception:
        pass
    win = WindowsManager()
    log = lambda m: print(m)
    vs = VSBridge(ctrl, log, winman=win, delay_ms=int(vs_cfg.get("delay_ms", 300)), dry_run=bool(vs_cfg.get("dry_run", False)))

    # OCR setup
    try:
        ocr_cfg = json.loads(ocr_cfg_path.read_text(encoding="utf-8"))
    except Exception:
        ocr_cfg = {"enabled": True}
    ocr_debug = root / "logs" / "ocr"
    ocr = CopilotOCR(ocr_cfg, log=log, debug_dir=ocr_debug)
    vs.set_ocr(ocr)
    elog = JsonActionLogger(root / "logs" / "errors" / "events.jsonl")

    report = {
        "steps": [],
        "summary": {},
    }

    def step(name: str, fn):
        ok = False
        detail = {}
        t0 = time.time()
        err = None
        try:
            res = fn()
            if isinstance(res, tuple):
                ok, detail = res
            else:
                ok = bool(res)
        except Exception as e:
            err = str(e)
            ok = False
        dt = time.time() - t0
        item = {"name": name, "ok": ok, "duration_s": round(dt, 3)}
        if detail:
            item["detail"] = detail
        if err:
            item["error"] = err
        report["steps"].append(item)
        print(f"Step: {name} -> {ok} ({dt:.2f}s)")
        if not ok:
            try:
                elog.log(
                    "navigation_step_failed",
                    step=name,
                    duration_s=round(dt, 3),
                    detail=item.get("detail") or {},
                    error=item.get("error") or "",
                )
            except Exception:
                pass
        return ok

    # Steps
    step("focus_vscode", lambda: vs.focus_vscode_window())
    step("focus_terminal", lambda: vs.focus_terminal())
    def guarded_terminal_echo():
        msg = (
            "echo Continue workflow - this test is only from an automated message "
            "to make you continue."
        )
        ok = vs.run_terminal_command(msg)
        if not ok:
            return False
        return True
    step("terminal_echo", guarded_terminal_echo)
    # VS Code-only path: avoid opening external Copilot app or protocols
    def focus_chat_only():
        ok1 = vs.focus_vscode_window()
        ok2 = vs.focus_copilot_chat_view()
        return bool(ok1 and ok2)
    step("focus_chat_only", focus_chat_only)

    # OCR capture from VS Code chat only
    def ocr_vscode_chat():
        text = vs.read_copilot_chat_text(ocr, save_dir=ocr_debug)
        return True, {"chars": len(text or ""), "preview": (text or "")[:160]}
    step("ocr_vscode_chat", ocr_vscode_chat)

    # Scroll chat down a bit (harmless if not focused)
    step("scroll_chat_down", lambda: vs.scroll_chat(direction="down", steps=2))

    # Summarize
    ok_all = all(s.get("ok") for s in report["steps"]) if report["steps"] else False
    report["summary"] = {
        "ok": bool(ok_all),
        "failed": [s for s in report["steps"] if not s.get("ok")],
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    outp = write_report(root, report)
    print("Report written:", outp)

    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
