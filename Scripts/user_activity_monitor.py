from __future__ import annotations

import argparse
import json
import math
import threading
import time
from pathlib import Path
from queue import Queue, Empty

from pynput import keyboard, mouse


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


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _get_dpi() -> float:
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
        hdc = user32.GetDC(None)
        if not hdc:
            return 96.0
        LOGPIXELSX = 88
        dpi = float(gdi32.GetDeviceCaps(hdc, LOGPIXELSX))
        user32.ReleaseDC(None, hdc)
        if dpi > 0:
            return dpi
    except Exception:
        pass
    return 96.0


def _controls_state(root: Path) -> dict:
    return _load_json(root / "config" / "controls_state.json")


def _set_controls_state(root: Path, st: dict) -> None:
    _write_json(root / "config" / "controls_state.json", st)


def _log_event(root: Path, evt: dict) -> None:
    path = root / "logs" / "actions" / "user_activity_events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(evt, ensure_ascii=False) + "\n")


def _paused_reason(st: dict) -> str:
    try:
        return str(st.get("paused_reason") or "")
    except Exception:
        return ""


def _set_paused(root: Path, paused: bool, reason: str | None = None, event: dict | None = None) -> None:
    st = _controls_state(root)
    if not isinstance(st, dict):
        st = {}
    st["paused"] = bool(paused)
    st["ts"] = time.time()
    if paused:
        st["paused_by"] = "user_activity"
        st["paused_reason"] = reason or "user_activity"
        st["paused_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        if event:
            st["paused_event"] = event
    else:
        st["paused_by"] = ""
        st["paused_reason"] = ""
        st["paused_at"] = ""
        st["paused_event"] = {}
        st.pop("resume_at", None)
    _set_controls_state(root, st)


def _set_resume_at(root: Path, resume_at: float) -> None:
    st = _controls_state(root)
    if not isinstance(st, dict):
        st = {}
    st["resume_at"] = float(resume_at)
    st["ts"] = time.time()
    _set_controls_state(root, st)


def _should_ignore_activity(st: dict, allow_esc: bool) -> bool:
    if not isinstance(st, dict):
        return False
    if bool(st.get("in_control_window", False)) and not allow_esc:
        return True
    if bool(st.get("paused", False)):
        return True
    return False


def _get_user_activity_cfg(root: Path) -> dict:
    rules = _load_json(root / "config" / "policy_rules.json")
    return (rules.get("user_activity") or {}) if isinstance(rules, dict) else {}


def main() -> int:
    ap = argparse.ArgumentParser(description="Monitor user input and pause automation when interrupted.")
    ap.add_argument("--popup", action="store_true", help="Show resume popup on pause")
    ap.add_argument("--esc-only", action="store_true", help="Only ESC pauses (ignore other keys/mouse)")
    ap.add_argument("--once", action="store_true", help="Exit after first pause event")
    args = ap.parse_args()

    root = _root()
    cfg = _get_user_activity_cfg(root)

    popup_enabled = bool(cfg.get("popup_enabled", True)) or bool(args.popup)
    esc_only = bool(cfg.get("esc_only", False)) or bool(args.esc_only)
    any_key_pauses = bool(cfg.get("any_key_pauses", True)) and not esc_only
    mouse_cm = float(cfg.get("mouse_cm_threshold", 4.0) or 4.0)
    auto_resume_idle_s = float(cfg.get("auto_resume_after_idle_s", 20.0) or 0.0)
    resume_quiet_s = float(cfg.get("resume_requires_quiet_seconds", 3.0) or 0.0)
    popup_default_min = int(cfg.get("popup_default_minutes", 2) or 2)

    dpi = _get_dpi()
    cm_to_px = dpi / 2.54
    mouse_px_threshold = max(1.0, float(mouse_cm) * cm_to_px)

    event_q: Queue[dict] = Queue()
    last_input = {"ts": time.time()}
    mouse_state = {"x": None, "y": None, "accum": 0.0}
    def on_key_press(key):
        try:
            is_esc = key == keyboard.Key.esc
        except Exception:
            is_esc = False
        if is_esc:
            event_q.put({"type": "key", "key": "esc"})
            return
        event_q.put({"type": "key", "key": str(key)})

    def on_move(x, y):
        cfg_live = _get_user_activity_cfg(root)
        esc_only_live = bool(cfg_live.get("esc_only", False)) or bool(args.esc_only)
        if esc_only_live:
            return
        if mouse_state["x"] is None:
            mouse_state["x"], mouse_state["y"] = x, y
            return
        dx = float(x) - float(mouse_state["x"])
        dy = float(y) - float(mouse_state["y"])
        dist = math.hypot(dx, dy)
        mouse_state["accum"] += dist
        mouse_state["x"], mouse_state["y"] = x, y
        if mouse_state["accum"] >= mouse_px_threshold:
            mouse_state["accum"] = 0.0
            event_q.put({"type": "mouse", "distance_px": dist})

    kb_listener = keyboard.Listener(on_press=on_key_press)
    ms_listener = mouse.Listener(on_move=on_move)
    kb_listener.start()
    ms_listener.start()

    popup_state = {"window": None}

    def _show_popup():
        try:
            import tkinter as tk
        except Exception:
            return

        if popup_state["window"] is not None:
            return

        win = tk.Toplevel()
        win.title("Automation Paused")
        win.attributes("-topmost", True)
        win.geometry("360x200")
        tk.Label(win, text="Automation paused due to user input.", font=("Segoe UI", 10, "bold")).pack(pady=8)
        tk.Label(win, text="Resume options:").pack(pady=4)

        minutes_var = tk.StringVar(value=str(popup_default_min))
        frame = tk.Frame(win)
        frame.pack(pady=4)
        tk.Label(frame, text="Resume in (minutes):").pack(side="left")
        tk.Spinbox(frame, from_=1, to=60, width=4, textvariable=minutes_var).pack(side="left", padx=6)

        def _resume_now():
            _set_paused(root, False, reason="user_resume")
            popup_state["window"] = None
            win.destroy()

        def _resume_later():
            try:
                mins = int(minutes_var.get())
                mins = max(1, min(120, mins))
            except Exception:
                mins = popup_default_min
            _set_resume_at(root, time.time() + float(mins * 60))
            popup_state["window"] = None
            win.destroy()

        btns = tk.Frame(win)
        btns.pack(pady=8)
        tk.Button(btns, text="Resume Now", command=_resume_now).pack(side="left", padx=6)
        tk.Button(btns, text="Resume in Minutes", command=_resume_later).pack(side="left", padx=6)
        tk.Button(btns, text="Keep Paused", command=lambda: win.destroy()).pack(side="left", padx=6)

        def _on_close():
            popup_state["window"] = None
            win.destroy()

        win.protocol("WM_DELETE_WINDOW", _on_close)
        popup_state["window"] = win

    def _pause_if_needed(event: dict):
        st = _controls_state(root)
        if not isinstance(st, dict):
            st = {}
        is_esc = event.get("type") == "key" and event.get("key") == "esc"
        cfg_live = _get_user_activity_cfg(root)
        esc_only_live = bool(cfg_live.get("esc_only", False)) or bool(args.esc_only)
        any_key_live = bool(cfg_live.get("any_key_pauses", True)) and not esc_only_live
        esc_only_when_owner = bool(cfg_live.get("esc_only_when_owner_active", True))
        owner = str(st.get("owner", "") or "")
        owner_active = bool(owner)
        if esc_only_when_owner and owner_active and not is_esc:
            return
        if not is_esc and _should_ignore_activity(st, allow_esc=is_esc):
            return
        if bool(st.get("paused", False)):
            return
        if event.get("type") == "key" and not is_esc and not any_key_live:
            return
        _set_paused(root, True, reason="user_activity", event=event)
        _log_event(root, {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "event": event, "paused": True})
        if popup_enabled:
            _show_popup()

    def _auto_resume_if_ready():
        st = _controls_state(root)
        if not isinstance(st, dict):
            return
        if not bool(st.get("paused", False)):
            return
        if _paused_reason(st) != "user_activity":
            return
        now = time.time()
        resume_at = float(st.get("resume_at", 0) or 0)
        if resume_at and now >= resume_at:
            if (now - last_input["ts"]) >= max(0.0, resume_quiet_s):
                _set_paused(root, False, reason="auto_resume")
                _log_event(root, {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "event": {"type": "auto_resume_at"}, "paused": False})
                return
        if auto_resume_idle_s > 0 and (now - last_input["ts"]) >= auto_resume_idle_s and not resume_at:
            _set_paused(root, False, reason="auto_resume_idle")
            _log_event(root, {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "event": {"type": "auto_resume_idle"}, "paused": False})

    def _loop_no_popup():
        while True:
            try:
                evt = event_q.get(timeout=0.2)
                last_input["ts"] = time.time()
                _pause_if_needed(evt)
                if args.once:
                    break
            except Empty:
                _auto_resume_if_ready()
                continue

    if not popup_enabled:
        _loop_no_popup()
        return 0

    import tkinter as tk

    root_tk = tk.Tk()
    root_tk.withdraw()

    def _poll_queue():
        try:
            while True:
                evt = event_q.get_nowait()
                last_input["ts"] = time.time()
                _pause_if_needed(evt)
                if args.once:
                    root_tk.after(100, root_tk.destroy)
                    return
        except Empty:
            pass
        _auto_resume_if_ready()
        root_tk.after(200, _poll_queue)

    root_tk.after(200, _poll_queue)
    root_tk.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
