from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from src.control import Controller, SafetyLimits
from src.vsbridge import VSBridge
from src.windows import WindowsManager
from src.ocr import CopilotOCR


def _click_new_chat_button(vs: VSBridge) -> bool:
    try:
        import uiautomation as auto  # type: ignore
    except Exception:
        return False

    win = getattr(vs, "winman", None)
    hwnd = win.get_foreground() if win else None
    if not hwnd:
        return False

    try:
        root_ctl = auto.ControlFromHandle(int(hwnd))
    except Exception:
        try:
            root_ctl = auto.GetFocusedControl()
        except Exception:
            return False

    target = None
    for ctl, _depth in auto.WalkControl(root_ctl, maxDepth=10):
        try:
            ctn = str(getattr(ctl, "ControlTypeName", "") or "").lower()
            if ctn not in {"buttoncontrol", "splitbuttoncontrol"}:
                continue
            nm = str(getattr(ctl, "Name", "") or "").strip().lower()
            automation_id = str(getattr(ctl, "AutomationId", "") or "").lower()
        except Exception:
            continue

        tokens = [nm, automation_id]
        if any("new chat" in t or "new conversation" in t or "start chat" in t for t in tokens if t):
            target = ctl
            break

    if target is None:
        return False

    if vs.dry_run:
        return True

    try:
        target.Click()
        time.sleep(0.25)
        return True
    except Exception:
        return False


def _ensure_chat_input_ready(vs: VSBridge, forced_vscode: bool) -> bool:
    focus_cmds = [
        "Chat: Focus on Chat Input",
        "GitHub Copilot Chat: Focus on Chat Input",
        "Copilot Chat: Focus on Chat Input",
        "Chat: Focus on Chat View",
        "GitHub Copilot Chat: Focus on Chat View",
        "Copilot Chat: Focus on Chat View",
    ]

    for _ in range(3):
        try:
            if vs._vscode_chat_input_ready():
                return True
        except Exception:
            pass

        try:
            vs.focus_copilot_chat_view(skip_focus=True)
        except Exception:
            pass

        for cmd in focus_cmds:
            try:
                if vs.command_palette(cmd, allow_repeat=True, allow_unverified=forced_vscode):
                    time.sleep(0.25)
                    break
            except Exception:
                continue

    # UIA fallback: click an edit control that looks like the chat input
    try:
        import uiautomation as auto  # type: ignore

        hwnd = vs.winman.get_foreground() if getattr(vs, "winman", None) else None
        root_ctl = auto.ControlFromHandle(int(hwnd)) if hwnd else auto.GetFocusedControl()
        target = None
        for ctl, _depth in auto.WalkControl(root_ctl, maxDepth=12):
            try:
                ctn = str(getattr(ctl, "ControlTypeName", "") or "").lower()
                if ctn not in {"editcontrol", "documentscontrol", "richtextcontrol", "textcontrol"}:
                    continue
                nm = str(getattr(ctl, "Name", "") or "").strip().lower()
            except Exception:
                continue
            if nm and not any(k in nm for k in ("message", "chat", "type", "ask")):
                continue
            target = ctl
            break
        if target is not None:
            if not vs.dry_run:
                try:
                    target.Click()
                    time.sleep(0.25)
                except Exception:
                    pass
            try:
                if vs._vscode_chat_input_ready():
                    return True
            except Exception:
                pass
    except Exception:
        pass

    return False


def _root() -> Path:
    return Path(__file__).resolve().parent.parent


def _load_json(path: Path) -> dict:
    try:
        if path.exists():
            obj = json.loads(path.read_text(encoding="utf-8"))
            return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}
    return {}


def _controls_ok(root: Path) -> tuple[bool, str]:
    try:
        from src.control_state import get_controls_state, is_state_stale  # type: ignore
    except Exception:
        return True, ""

    rules = _load_json(root / "config" / "policy_rules.json")
    controls_cfg = (rules.get("controls") or {}) if isinstance(rules, dict) else {}
    stale_after_s = float(controls_cfg.get("stale_after_s", 10.0) or 10.0)

    st = get_controls_state(root) or {}
    owner = str(st.get("owner", "") or "")
    paused = bool(st.get("paused", False))
    try:
        stale = bool(is_state_stale(st, stale_after_s))
    except Exception:
        stale = False

    if stale:
        return False, "controls_state.stale"
    if paused:
        return False, "controls_state.paused"
    if owner and owner not in {"", "workflow_test", "agent", "orchestrator", "orchestrator_agent"}:
        return False, f"controls owned by '{owner}'"
    return True, ""


def main() -> int:
    ap = argparse.ArgumentParser(description="Open VS Code Copilot Chat tabs for Agent Mode roles (best-effort).")
    ap.add_argument("--count", type=int, default=4, help="Number of new chat tabs to open")
    ap.add_argument("--dry-run", action="store_true", help="Do not send UI input; only log intent")
    ap.add_argument("--force", action="store_true", help="Ignore controls_state gating (unsafe)")
    args = ap.parse_args()

    root = _root()
    rules = _load_json(root / "config" / "policy_rules.json")
    vs_cfg = (rules.get("vsbridge") or {}) if isinstance(rules, dict) else {}

    if not args.force:
        ok, reason = _controls_ok(root)
        if not ok:
            print(f"Controls not available ({reason}). Aborting.")
            return 2

    limits = SafetyLimits(max_clicks_per_min=60, max_keys_per_min=120)
    ctrl = Controller(mouse_speed=0.25, limits=limits, mouse_control_seconds=6, mouse_release_seconds=3)
    win = WindowsManager()
    log = lambda m: print(m)
    vs = VSBridge(ctrl, log, winman=win, delay_ms=int(vs_cfg.get("delay_ms", 300)), dry_run=bool(vs_cfg.get("dry_run", False)) or bool(args.dry_run))

    try:
        ocr_cfg = json.loads((root / "config" / "ocr.json").read_text(encoding="utf-8"))
    except Exception:
        ocr_cfg = {"enabled": True}
    ocr = CopilotOCR(ocr_cfg, log=log, debug_dir=root / "logs" / "ocr")
    try:
        vs.set_ocr(ocr)
    except Exception:
        pass

    commands = [
        "GitHub Copilot Chat: New Chat",
        "Copilot Chat: New Chat",
        "Chat: New Chat",
    ]

    def _foreground_is_vscode() -> bool:
        try:
            fg = win.get_foreground()
            info = win.get_window_info(fg) if fg else {}
            proc = str(info.get("process") or "").lower()
            title = str(info.get("title") or "").lower()
            return ("code" in proc) or ("visual studio code" in title) or ("vscode" in title)
        except Exception:
            return False

    results = []
    success_count = 0
    for i in range(max(0, int(args.count))):
        attempts = []
        success = False
        for attempt in range(3):
            ok_focus = bool(vs.focus_vscode_window()) and bool(vs.focus_copilot_chat_view())
            forced_vscode = False
            if not ok_focus:
                try:
                    wins = win.list_windows(include_empty_titles=True)
                    pick_hwnd = 0
                    for w in wins:
                        hwnd = int(w.get("hwnd") or 0)
                        if not hwnd:
                            continue
                        info = win.get_window_info(hwnd)
                        title = str(info.get("title") or "").lower()
                        proc = str(info.get("process") or "").lower()
                        if "code" in proc or "visual studio code" in title or "vscode" in title:
                            pick_hwnd = hwnd
                            break
                    if pick_hwnd and win.focus_hwnd(pick_hwnd):
                        forced_vscode = True
                        time.sleep(0.2)
                        ok_focus = bool(vs.focus_copilot_chat_view(skip_focus=True))
                except Exception:
                    pass

            cmd_ok = False
            used = ""
            tried = []
            fallback_ok = False
            input_ready = None
            fallback_name = ""

            ui_click_ok = False
            if ok_focus and (_foreground_is_vscode() or forced_vscode):
                ui_click_ok = _click_new_chat_button(vs)

            if not ui_click_ok and ok_focus and (_foreground_is_vscode() or forced_vscode):
                for cmd in commands:
                    tried.append(cmd)
                    if vs.command_palette(cmd, allow_repeat=True, allow_unverified=forced_vscode):
                        time.sleep(0.3)
                        if not vs._verify_vscode_foreground():
                            vs.focus_vscode_window()
                            continue
                        cmd_ok = True
                        used = cmd
                        break
                if not cmd_ok:
                    try:
                        if vs.focus_copilot_chat_view(skip_focus=forced_vscode):
                            for cmd in commands:
                                if cmd in tried:
                                    continue
                                tried.append(cmd)
                                if vs.command_palette(cmd, allow_repeat=True, allow_unverified=forced_vscode):
                                    time.sleep(0.3)
                                    if not vs._verify_vscode_foreground():
                                        vs.focus_vscode_window()
                                        continue
                                    cmd_ok = True
                                    used = cmd
                                    break
                    except Exception:
                        pass

            if ok_focus and not cmd_ok:
                try:
                    import uiautomation as auto  # type: ignore

                    hwnd = win.get_foreground() if win else None
                    root_ctl = auto.ControlFromHandle(int(hwnd)) if hwnd else auto.GetFocusedControl()
                    target = None
                    scanned = 0
                    for ctl, _depth in auto.WalkControl(root_ctl, maxDepth=10):
                        scanned += 1
                        if scanned > 1800:
                            break
                        try:
                            ctn = str(getattr(ctl, "ControlTypeName", "") or "").lower()
                            if ctn not in {"buttoncontrol", "splitbuttoncontrol", "menuitemcontrol"}:
                                continue
                            nm = str(getattr(ctl, "Name", "") or "").strip()
                        except Exception:
                            continue
                        nm_l = nm.lower()
                        if "new chat" in nm_l or "new conversation" in nm_l:
                            target = ctl
                            fallback_name = nm
                            break
                    if target is not None:
                        if not vs.dry_run:
                            try:
                                target.Click()
                            except Exception:
                                pass
                        fallback_ok = True
                except Exception:
                    fallback_ok = False

            if cmd_ok or fallback_ok:
                if args.dry_run:
                    input_ready = True
                else:
                    input_ready = bool(_ensure_chat_input_ready(vs, forced_vscode))
                    if not input_ready:
                        cmd_ok = False
                        fallback_ok = False
                        ui_click_ok = False

            attempts.append({
                "attempt": attempt + 1,
                "focused": ok_focus,
                "command_ok": cmd_ok,
                "ui_click_ok": ui_click_ok,
                "command": used,
                "command_tried": tried,
                "fallback_new_chat_ok": fallback_ok,
                "fallback_new_chat_name": fallback_name,
                "input_ready": input_ready,
            })
            success = bool(cmd_ok or fallback_ok or ui_click_ok)
            if success:
                break
            time.sleep(0.4)

        last = attempts[-1] if attempts else {"focused": False, "command_ok": False, "command": ""}
        entry = {
            "index": i + 1,
            "focused": bool(last.get("focused")),
            "command_ok": bool(last.get("command_ok")),
            "ui_click_ok": bool(last.get("ui_click_ok")),
            "command": str(last.get("command") or ""),
            "command_tried": last.get("command_tried", []),
            "fallback_new_chat_ok": bool(last.get("fallback_new_chat_ok")),
            "fallback_new_chat_name": str(last.get("fallback_new_chat_name") or ""),
            "attempts": attempts,
            "input_ready": last.get("input_ready"),
            "success": success,
        }
        results.append(entry)
        if success:
            success_count += 1

    out_dir = root / "logs" / "tests"
    out_dir.mkdir(parents=True, exist_ok=True)
    summary = {
        "requested": max(0, int(args.count)),
        "success_count": success_count,
        "results": results,
    }
    out_path = out_dir / f"open_agent_mode_tabs_{time.strftime('%Y%m%d_%H%M%S')}.json"
    out_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(str(out_path))
    if success_count < max(0, int(args.count)):
        print(f"Only opened {success_count} of {args.count} requested chat tabs.")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
