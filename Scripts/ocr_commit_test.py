from __future__ import annotations
import json
import time
from pathlib import Path

from src.control import Controller, SafetyLimits
from src.vsbridge import VSBridge
from src.windows import WindowsManager
from src.ocr import CopilotOCR


def append_improvements(root: Path, title: str, text: str) -> Path | None:
    if not text:
        return None
    imp = root / "projects" / "Self-Improve" / "improvements.md"
    imp.parent.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(imp, "a", encoding="utf-8") as f:
            f.write(f"\n\n## {title} ({ts})\n\n")
            f.write(text + "\n")
        return imp
    except Exception:
        return None


def write_report(root: Path, report: dict) -> Path:
    out_dir = root / "logs" / "tests"
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    out_path = out_dir / f"ocr_commit_test_{ts}.json"
    out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return out_path


def main():
    root = Path(__file__).resolve().parent.parent
    rules_path = root / "config" / "policy_rules.json"
    ocr_cfg_path = root / "config" / "ocr.json"
    try:
        rules = json.loads(rules_path.read_text(encoding="utf-8"))
    except Exception:
        rules = {}
    vs_cfg = rules.get("vsbridge", {}) or {}

    # Controller setup
    limits = SafetyLimits(max_clicks_per_min=180, max_keys_per_min=240)
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

    report = {
        "app": {},
        "chat": {},
        "summary": {},
    }

    # Capture from Copilot app
    def capture_app():
        settle_ms = int((ocr_cfg or {}).get("app_settle_ms", 800))
        ok_focus = vs.focus_copilot_app()
        time.sleep(max(0, settle_ms) / 1000.0)
        # Guard: if a browser or unrelated window is foreground, skip to avoid wrong evidence
        fg = win.get_foreground()
        skipped_reason = ""
        if fg:
            info = win.get_window_info(fg)
            title = (info.get("title") or "").lower()
            cls = (info.get("class") or "").lower()
            is_browser = ("edge" in title) or ("chrome" in title) or ("github" in title)
            is_vscode = ("visual studio code" in title) or (" - visual studio code" in title)
            is_copilot_title = ("copilot" in title)
            if is_browser and not is_copilot_title:
                skipped_reason = f"foreground looks like browser: title='{info.get('title','')}', class='{info.get('class','')}'"
            if is_vscode:
                # If VS Code is foreground, prefer the chat path rather than app
                skipped_reason = skipped_reason or "foreground is VS Code; using chat capture for app text would be misleading"
        if skipped_reason:
            return {
                "focused": bool(ok_focus),
                "ok": False,
                "chars": 0,
                "preview": "",
                "image_path": "",
                "appended_path": "",
                "skipped": True,
                "reason": skipped_reason,
            }
        # Apply app ROI override if present
        alt_region = None
        orig_region = None
        try:
            alt_region = (ocr_cfg.get("targets") or {}).get("copilot_app") or ocr_cfg.get("app_region_percent")
            if alt_region:
                orig_region = getattr(ocr, "region_percent", None)
                setattr(ocr, "region_percent", alt_region)
        except Exception:
            pass
        try:
            res = ocr.capture_chat_text(save_dir=ocr_debug)
        finally:
            try:
                if alt_region and orig_region is not None:
                    setattr(ocr, "region_percent", orig_region)
            except Exception:
                pass
        # No OCR text available; instead summarize detected elements and attach image
        text = ""
        elems = res.get("elements") if isinstance(res, dict) else None
        # Fallback: do one more capture if no elements found
        if not elems:
            try:
                time.sleep(max(0, settle_ms + 700) / 1000.0)
            except Exception:
                pass
            try:
                if alt_region and orig_region is not None:
                    setattr(ocr, "region_percent", orig_region)
                res2 = ocr.capture_chat_text(save_dir=ocr_debug)
                if res2.get("ok") and (res2.get("elements") or []):
                    elems = res2.get("elements")
                    res = res2
            except Exception:
                pass
        path = None
        if elems:
            note = f"Captured {len(elems)} UI elements. See image: {str(res.get('image_path') or '')}"
            path = append_improvements(root, "Copilot App Summary (image)", note)
        return {
            "focused": bool(ok_focus),
            "ok": bool(res.get("ok")),
            "chars": len(text),
            "preview": text[:200],
            "image_path": str(res.get("image_path") or ""),
            "appended_path": str(path) if path else "",
        }

    # Capture from VS Code chat
    def capture_chat():
        ok_focus = vs.focus_copilot_chat_view()
        settle_ms = int((ocr_cfg or {}).get("chat_settle_ms", 1000))
        time.sleep(max(0, settle_ms) / 1000.0)
        # Guard: ensure VS Code is actually foreground; if not, try to refocus and skip if still wrong
        skipped_reason = ""
        fg = win.get_foreground()
        if fg:
            info = win.get_window_info(fg)
            title = (info.get("title") or "").lower()
            is_vscode = ("visual studio code" in title) or (" - visual studio code" in title)
            if not is_vscode:
                # best-effort refocus
                try:
                    vs.focus_vscode_window()
                    time.sleep(0.4)
                    fg2 = win.get_foreground()
                    if fg2:
                        info2 = win.get_window_info(fg2)
                        title2 = (info2.get("title") or "").lower()
                        is_vscode = ("visual studio code" in title2) or (" - visual studio code" in title2)
                except Exception:
                    pass
                if not is_vscode:
                    skipped_reason = f"foreground is not VS Code (title='{info.get('title','')}', class='{info.get('class','')}')"
        alt_region = None
        orig_region = None
        try:
            alt_region = (ocr_cfg.get("targets") or {}).get("vscode_chat") or ocr_cfg.get("chat_region_percent")
            if alt_region:
                orig_region = getattr(ocr, "region_percent", None)
                setattr(ocr, "region_percent", alt_region)
        except Exception:
            pass
        try:
            res = ocr.capture_chat_text(save_dir=ocr_debug)
        finally:
            try:
                if alt_region and orig_region is not None:
                    setattr(ocr, "region_percent", orig_region)
            except Exception:
                pass
        text = ""
        elems = res.get("elements") if isinstance(res, dict) else None
        if not elems:
            try:
                time.sleep(max(0, settle_ms + 700) / 1000.0)
            except Exception:
                pass
            try:
                if alt_region and orig_region is not None:
                    setattr(ocr, "region_percent", orig_region)
                res2 = ocr.capture_chat_text(save_dir=ocr_debug)
                if res2.get("ok") and (res2.get("elements") or []):
                    elems = res2.get("elements")
                    res = res2
            except Exception:
                pass
        path = None
        if elems:
            note = f"Captured {len(elems)} UI elements. See image: {str(res.get('image_path') or '')}"
            path = append_improvements(root, "Copilot Chat Summary (image)", note)
        return {
            "focused": bool(ok_focus),
            "ok": bool(res.get("ok")),
            "chars": len(text),
            "preview": text[:200],
            "image_path": str(res.get("image_path") or ""),
            "appended_path": str(path) if path else "",
            "skipped": bool(skipped_reason),
            "reason": skipped_reason,
        }

    app_info = capture_app()
    chat_info = capture_chat()

    report["app"] = app_info
    report["chat"] = chat_info
    report["summary"] = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "ok": bool(app_info.get("ok") or chat_info.get("ok")),
        "total_chars": int(app_info.get("chars", 0)) + int(chat_info.get("chars", 0)),
        "appended": bool(app_info.get("appended_path") or chat_info.get("appended_path")),
    }

    outp = write_report(root, report)
    print("OCR commit test report:", outp)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
