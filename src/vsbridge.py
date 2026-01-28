import time
import re
from pathlib import Path
from typing import Optional, Any, List
import json
from collections import deque
import os

try:
    import pyautogui  # type: ignore
except Exception:
    pyautogui = None


class VSBridge:
    """
    Keyboard/mouse-driven automation layer for VSCode + Copilot Chat.
    This layer is intentionally hotkey-centric.
    """

    def __init__(self, ctrl, logger, winman: Optional[Any] = None, delay_ms: int = 300, dry_run: bool = True):
        self.ctrl = ctrl
        self.log = logger
        self.winman = winman
        self.delay = max(0, delay_ms) / 1000.0
        self.dry_run = dry_run
        # Load palette policy on init
        try:
            root = Path(__file__).resolve().parent.parent
            cfg_path = root / "config" / "policy_rules.json"
            rules = {}
            if cfg_path.exists():
                rules = json.loads(cfg_path.read_text(encoding="utf-8"))
            pal = (rules.get("palette") or {}) if isinstance(rules, dict) else {}
            self._banned_palette: List[str] = [str(x).lower() for x in (pal.get("banned") or [])]
        except Exception:
            self._banned_palette = []
        # Track palette commands attempted this process to avoid repeats
        self._palette_attempted: List[str] = []
        self._ocr = None
        # Focus thrash detection
        self._focus_events = deque(maxlen=30)  # items: {"ts": float, "target": str, "ok": bool}
        # Optional: use Win32 SendInput for certain keypresses in Copilot app.
        self._copilot_use_sendkeys = str(os.environ.get("COPILOT_USE_SENDKEYS", "0")).strip() in {"1", "true", "yes"}
        # Track last known Copilot window handle for bbox OCR stability.
        self._copilot_hwnd: Optional[int] = None

    def _press_keys_copilot(self, keys: List[str]) -> bool:
        """Press keys intended for the Copilot app, optionally via SendInput.

        This still relies on foreground gating; SendInput can be more reliable than pyautogui
        for certain WinUI surfaces.
        """
        if self.dry_run:
            return True
        # Safety: never send keys unless the foreground is Copilot (or a standard file dialog).
        try:
            if self.winman:
                fg = self.winman.get_foreground()
                info = self.winman.get_window_info(fg) if fg else {}
                proc = (info.get("process") or "").lower()
                cls = (info.get("class") or "").lower()
                title = (info.get("title") or "").lower()
                # Never treat VS Code/terminals as a valid target for these keystrokes.
                if proc and (proc == "code.exe" or proc.startswith("code")):
                    return False
                if proc in {"windowsterminal.exe", "conhost.exe", "pwsh.exe", "powershell.exe", "cmd.exe"}:
                    return False
                # Classic common dialogs are class #32770.
                # Windows Copilot sometimes shows an Explorer-hosted file picker behind a
                # Copilot focus frame (title/class), which still needs to accept Alt+N / paste.
                is_dialog = ("#32770" in cls) or (
                    proc == "explorer.exe"
                    and (
                        "copilotkeypressfocusframe" in cls
                        or "copilotkeyfocuswindow" in cls
                        or "copilotkeyfocuswindow" in title
                    )
                )
                if not (bool(self._verify_copilot_foreground()) or bool(is_dialog)):
                    return False
        except Exception:
            # Fail closed if we cannot verify foreground.
            return False
        if self._copilot_use_sendkeys and self.winman and hasattr(self.winman, "send_input_keys"):
            try:
                ok = bool(self.winman.send_input_keys(keys))
                if ok:
                    return True
            except Exception:
                pass
        try:
            return bool(self.ctrl.press_keys(keys))
        except Exception:
            return False

    def _copilot_app_focused_name(self) -> tuple[str, str]:
        """Return (ControlTypeName, Name) for the currently focused UIA control."""
        try:
            import uiautomation as auto  # type: ignore

            fc = auto.GetFocusedControl()
            ct = str(getattr(fc, "ControlTypeName", "") or "")
            nm = str(getattr(fc, "Name", "") or "")
            return ct, nm
        except Exception:
            return "", ""

    def _copilot_app_click_attach_button(self, observe_fn=None) -> bool:
        """Best-effort click the attach/upload button near the input area.

        This intentionally uses mouse clicks for reliability (per user feedback).
        Returns True if a plausible attach button was clicked.
        """
        if self.dry_run:
            return True
        if not self.winman:
            return False
        try:
            if not self._verify_copilot_foreground():
                return False
        except Exception:
            return False
        try:
            hwnd = self.winman.get_foreground()
            rect = self.winman.get_window_rect(hwnd) if hwnd else None
        except Exception:
            rect = None
        if not rect:
            return False

        try:
            import uiautomation as auto  # type: ignore

            root = auto.ControlFromHandle(int(hwnd))
            win_left = int(rect.get("left", 0))
            win_top = int(rect.get("top", 0))
            win_w = int(rect.get("width", 0))
            win_h = int(rect.get("height", 0))
            # Search near bottom of window (where input bar typically is).
            # Keep this tight to avoid confusing message list items with input-bar controls.
            y_min = win_top + int(win_h * 0.86)
            # Attach/plus icon is typically left-of-input (avoid send button area).
            x_min = win_left + int(win_w * 0.03)
            x_max = win_left + int(win_w * 0.40)

            candidates = []
            scanned = 0
            for ctl, _depth in auto.WalkControl(root, maxDepth=9):
                scanned += 1
                if scanned > 1800:
                    break
                try:
                    ctn = str(getattr(ctl, "ControlTypeName", "") or "").lower()
                except Exception:
                    continue
                if ctn not in {"buttoncontrol", "splitbuttoncontrol", "menuitemcontrol"}:
                    continue
                try:
                    nm = str(getattr(ctl, "Name", "") or "").strip()
                except Exception:
                    nm = ""
                nm_l = nm.lower()
                if not nm_l:
                    # Icon-only buttons exist; still consider if in input area.
                    pass
                # Prefer explicit names when present; allow icon-only candidates too.
                if nm_l and ("send" in nm_l or "submit" in nm_l):
                    continue
                # Copilot's button naming varies across builds/locales; don't over-filter.
                # We keep this safe by constraining to bottom-left input region and by
                # requiring that a real file picker appears after the click.
                if nm_l and any(k in nm_l for k in ("microphone", "mic", "voice", "dictat", "speaker")):
                    continue
                try:
                    br = getattr(ctl, "BoundingRectangle", None)
                    cx = int((br.left + br.right) / 2) if br else 0
                    cy = int((br.top + br.bottom) / 2) if br else 0
                except Exception:
                    cx, cy = 0, 0
                if not cx or not cy:
                    continue
                if cy < y_min or cx < x_min or cx > x_max:
                    continue
                # Score: closer to bottom-right-ish and with explicit names.
                score = 0
                score += int((cy - y_min) / 8)
                score += int((cx - x_min) / 20)
                if nm_l:
                    score += 200
                candidates.append((score, cx, cy, nm))

            if not candidates:
                return False
            candidates.sort(key=lambda t: t[0], reverse=True)
            _score, cx, cy, nm = candidates[0]
            try:
                self._log_error_event("copilot_app_attach_click", ok=True, name=str(nm)[:120], x=int(cx), y=int(cy))
            except Exception:
                pass
            # Navigation is moving the cursor: observe after move, then click.
            try:
                self.ctrl.move_mouse(int(cx), int(cy))
                time.sleep(max(self.delay / 3, 0.12))
                if callable(observe_fn):
                    observe_fn("attach_click_candidate:after_move")
            except Exception:
                pass
            self.ctrl.click_at(int(cx), int(cy))
            time.sleep(max(self.delay / 2, 0.25))
            return True
        except Exception:
            return False

    def _log_error_event(self, event: str, **data: Any) -> None:
        try:
            from .jsonlog import JsonActionLogger  # type: ignore

            root = Path(__file__).resolve().parent.parent
            JsonActionLogger(root / "logs" / "errors" / "events.jsonl").log(event, **data)
        except Exception:
            pass

    def _record_focus(self, target: str, ok: bool, method: str) -> None:
        now = time.time()
        self._focus_events.append({"ts": now, "target": str(target), "ok": bool(ok)})
        image_path = ""
        try:
            # Capture the current foreground window as an image for later assessment.
            if getattr(self, "_ocr", None) and self.winman:
                fg = self.winman.get_foreground()
                bbox = None
                if fg:
                    rect = self.winman.get_window_rect(int(fg)) or {}
                    if rect and rect.get("width") and rect.get("height"):
                        bbox = {
                            "left": int(rect.get("left", 0)),
                            "top": int(rect.get("top", 0)),
                            "width": int(rect.get("width", 0)),
                            "height": int(rect.get("height", 0)),
                        }
                root = Path(__file__).resolve().parent.parent
                tag = f"focus_{target}_{'ok' if ok else 'fail'}"
                if bbox:
                    res = self._ocr.capture_bbox_text(bbox=bbox, save_dir=root / "logs" / "ocr", tag=tag)
                else:
                    res = self._ocr.capture_image(save_dir=root / "logs" / "ocr", tag=tag)
                if isinstance(res, dict):
                    image_path = str(res.get("image_path") or "")
        except Exception:
            image_path = ""

        self._log_error_event(
            "focus_attempt",
            target=str(target),
            ok=bool(ok),
            method=str(method),
            image_path=image_path,
        )

        # Detect rapid alternating VS Code <-> Copilot thrash
        try:
            window_s = 8.0
            recent = [e for e in self._focus_events if (now - float(e.get("ts", 0))) <= window_s]
            if len(recent) < 8:
                return
            seq = [e.get("target") for e in recent if e.get("ok")]
            if len(seq) < 8:
                return
            # Count alternations between consecutive targets
            alternations = 0
            for i in range(1, len(seq)):
                if seq[i] != seq[i - 1]:
                    alternations += 1
            # If most transitions are alternations, it's likely thrashing
            if alternations >= 6 and set(seq[-8:]).issubset({"vscode", "copilot_app"}):
                self._log_error_event(
                    "focus_thrash_detected",
                    window_s=window_s,
                    alternations=alternations,
                    recent_targets=seq[-12:],
                )
        except Exception:
            pass

    def set_ocr(self, ocr: Any):
        self._ocr = ocr

    def _ocr_observe(self, tag: str):
        try:
            if not self._ocr:
                return
            root = Path(__file__).resolve().parent.parent
            # IMPORTANT: do not focus/switch apps as part of observation.
            # We only capture the configured ROI as-is to avoid stealing focus
            # (previously this could redirect terminal typing into chat).
            res = self._ocr.capture_chat_text(save_dir=root / "logs" / "ocr")
            img = str(res.get("image_path") or "") if isinstance(res, dict) else ""
            elems = res.get("elements") if isinstance(res, dict) else None
            try:
                self._log_error_event(
                    "image_observation",
                    stage=tag,
                    ok=bool(res.get("ok")) if isinstance(res, dict) else True,
                    image_path=img,
                    elements_count=len(elems) if elems is not None else 0,
                )
            except Exception:
                pass
        except Exception:
            pass

    def _ocr_detect_wrong_field(self) -> bool:
        try:
            if not self._ocr:
                return False
            root = Path(__file__).resolve().parent.parent
            # Do not focus here; capture ROI image and look for large overlay elements.
            res = self._ocr.capture_chat_text(save_dir=root / "logs" / "ocr")
            elems = res.get("elements") if isinstance(res, dict) else None
            # If a large rectangle-like element covers most of the region, assume wrong field (palette)
            if elems:
                try:
                    for e in elems:
                        b = e.get("bbox") or {}
                        w = float(b.get("width") or 0)
                        h = float(b.get("height") or 0)
                        # If an element covers large fraction, consider it an overlay
                        if w > 200 and h > 60:
                            self._log_error_event("image_input_wrong_field", reason="large_overlay_detected", bbox=b)
                            return True
                except Exception:
                    pass
            return False
        except Exception:
            return False

    def press_tab(self):
        try:
            self._ocr_observe("before_tab")
            if not self.dry_run:
                self.ctrl.press_keys(["tab"])
                time.sleep(self.delay)
            self._ocr_observe("after_tab")
            return True
        except Exception:
            return False

    def _type_path(self, path_str: str):
        if self.dry_run:
            self.log(f"DRY-RUN type path: {path_str}")
            return
        self._ocr_observe("before_type_path")
        self.ctrl.type_text(path_str)
        self.ctrl.press_keys(["enter"])
        self._ocr_observe("after_type_path_enter")

    def open_vscode(self):
        """Best-effort: focus VS Code if already open.

        This project generally runs inside VS Code already; launching VS Code via Start menu
        is intentionally avoided here to prevent sending keystrokes to the wrong surface.
        """
        self.log("VSBridge: Open VSCode")
        if self.dry_run:
            self.log("DRY-RUN open vscode")
            return True
        return bool(self.focus_vscode_window())

    def command_palette(self, command: str) -> bool:
        """Open VS Code command palette and run a command string.
        Enforces palette policy (banned/repeated) and foreground gating.
        """
        try:
            cmd = str(command or "").strip()
            if not cmd:
                return False

            low = cmd.lower()
            if any(b in low for b in (self._banned_palette or [])):
                self._log_error_event("palette_command_bypassed", command=cmd, reason="banned")
                return False
            if low in [c.lower() for c in (self._palette_attempted or [])]:
                self._log_error_event("palette_command_repeated", command=cmd)
                return False

            self._palette_attempted.append(cmd)
            if self.dry_run:
                self.log(f"DRY-RUN command palette: {cmd}")
                return True

            # Gate inputs to VS Code foreground while sending palette command.
            prev_gate = None
            try:
                prev_gate = getattr(self.ctrl, "_window_gate", None)
                self.ctrl.set_window_gate(lambda: bool(self._verify_vscode_foreground()))
            except Exception:
                prev_gate = None

            try:
                if not self._verify_vscode_foreground():
                    self._log_error_event("foreground_not_vscode_before_send", command=cmd)
                    return False
                self._ocr_observe("palette_before_open")
                self.ctrl.press_keys(["ctrl", "shift", "p"])
                time.sleep(self.delay)
                self._ocr_observe("palette_after_open")
                if not bool(self.ctrl.type_text(cmd)):
                    self._log_error_event("terminal_type_failed", context="command_palette", command_preview=cmd[:120])
                    return False
                time.sleep(self.delay / 2)
                if not bool(self.ctrl.press_keys(["enter"])):
                    self._log_error_event("terminal_type_failed", context="command_palette_enter", command_preview=cmd[:120])
                    return False
                time.sleep(self.delay)
                self._ocr_observe("palette_after_enter")
                return True
            finally:
                try:
                    if prev_gate is not None:
                        self.ctrl.set_window_gate(prev_gate)
                    else:
                        self.ctrl.set_window_gate(None)
                except Exception:
                    pass
        except Exception:
            return False

    def focus_copilot_chat_view(self) -> bool:
        """Focus the Copilot/Chat panel inside VS Code (not the Windows Copilot app)."""
        self.log("VSBridge: Focus Copilot chat view (cursor-select input)")
        if self.dry_run:
            self.log("DRY-RUN focus copilot chat view")
            return True
        try:
            if not self.focus_vscode_window():
                return False
        except Exception:
            return False
        # Preferred path: use cursor to select the input field inside the Chat tab
        try:
            try:
                root = Path(__file__).resolve().parent.parent
                # capture chat region and detect UI elements (requires CopilotOCR set via `set_ocr`)
                if getattr(self, "_ocr", None):
                    res = self._ocr.capture_chat_text(save_dir=root / "logs" / "ocr")
                else:
                    res = None
            except Exception:
                res = None

            if isinstance(res, dict):
                elems = res.get("elements") or []
                best = None
                best_score = -1.0
                for e in elems:
                    try:
                        b = e.get("bbox") or {}
                        left = float(b.get("left") or 0)
                        top = float(b.get("top") or 0)
                        width = float(b.get("width") or 0)
                        height = float(b.get("height") or 0)
                        bottom = top + height
                        # Score: prefer elements near the bottom, reasonable width, and not very tall
                        score = bottom
                        if width > 120:
                            score += 50
                        if height < 160:
                            score += 40
                        # small boost for template-detected elements
                        if str(e.get("type") or "").lower().startswith("template"):
                            score += 20
                        if score > best_score:
                            best_score = score
                            best = (left, top, width, height)
                    except Exception:
                        continue

                if best:
                    lx, ty, w, h = best
                    cx = int(lx + w / 2)
                    cy = int(ty + h / 2)
                    try:
                        # move cursor and click to focus input
                        self.ctrl.move_mouse(cx, cy)
                        time.sleep(max(self.delay / 3, 0.12))
                        self.ctrl.click_at(cx, cy)
                        time.sleep(max(self.delay / 2, 0.12))
                        self._record_focus("vscode_chat_input", True, method="cursor_select")
                        return True
                    except Exception:
                        pass

            # Fallback: approximate input location by window geometry (bottom-center)
            try:
                if self.winman:
                    fg = self.winman.get_foreground()
                    rect = self.winman.get_window_rect(fg) if fg else None
                else:
                    rect = None
            except Exception:
                rect = None
            if rect and rect.get("width") and rect.get("height"):
                left = int(rect.get("left", 0))
                top = int(rect.get("top", 0))
                w = int(rect.get("width", 0))
                h = int(rect.get("height", 0))
                cx = left + int(w * 0.5)
                cy = top + int(h * 0.92)
                try:
                    self.ctrl.move_mouse(cx, cy)
                    time.sleep(max(self.delay / 3, 0.12))
                    self.ctrl.click_at(cx, cy)
                    time.sleep(max(self.delay / 2, 0.12))
                    self._record_focus("vscode_chat_input_guess", True, method="bbox_guess")
                    return True
                except Exception:
                    pass

        except Exception:
            pass

        # Legacy: if cursor-based selection failed, fall back to palette/hotkey approach
        self.log("VSBridge: cursor-select failed; falling back to palette/hotkey")
        try:
            self.ctrl.press_keys(["ctrl", "alt", "i"])
            time.sleep(self.delay)
            if self._verify_vscode_foreground():
                return True
        except Exception:
            pass

        default_cmds = [
            "Chat: Focus on Chat View",
            "Chat: Focus on Chat Input",
            "Chat: Focus on Chat",
            "GitHub Copilot Chat: Focus on Chat View",
            "GitHub Copilot Chat: Focus on Chat Input",
            "Copilot Chat: Focus on Chat View",
            "Copilot Chat: Focus on Chat Input",
            "View: Open View...",
        ]
        for cmd in default_cmds:
            try:
                ok = self.command_palette(cmd)
                time.sleep(self.delay)
                if ok and self._verify_vscode_foreground():
                    return True
            except Exception:
                continue
        return False

    # Windows Copilot (system app) support
    def focus_copilot_app(self) -> bool:
        """Focus or toggle the Windows Copilot app (Win + C on Windows 11)."""
        self.log("VSBridge: Focus Windows Copilot app")
        if self.dry_run:
            self.log("DRY-RUN focus copilot app")
            return True
        # Avoid unnecessary toggles if already foreground
        try:
            if self._verify_copilot_foreground():
                self._record_focus("copilot_app", True, method="already_foreground")
                return True
        except Exception:
            pass

        # Prefer focusing an existing *main* Copilot window first (avoids Win+C toggle closing it).
        # Some Copilot processes expose tiny hotkey/registration windows that should be ignored.
        try:
            if self.winman:
                bad_classes = {
                    "Copilot_HotKeyRegistrationWindow",
                    "CopilotKeyPressFocusFrame",
                    "CopilotkeyFocusWindow",
                }

                best_hwnd = None
                best_score = -1
                try:
                    windows = self.winman.list_windows(include_empty_titles=True)
                except Exception:
                    windows = []

                for w in windows or []:
                    try:
                        hwnd_i = int(w.get("hwnd") or 0)
                    except Exception:
                        continue
                    if not hwnd_i:
                        continue
                    try:
                        proc = (self.winman.get_window_process_name(hwnd_i) or "").lower()
                    except Exception:
                        proc = ""
                    if "copilot" not in proc:
                        continue
                    title = str(w.get("title") or "")
                    cls = str(w.get("class") or "")
                    if cls in bad_classes:
                        continue
                    try:
                        r = self.winman.get_window_rect(hwnd_i) or {}
                        area = int(r.get("width", 0)) * int(r.get("height", 0))
                    except Exception:
                        area = 0
                    # Ignore tiny utility windows.
                    if area and area < 120 * 120:
                        continue
                    score = 0
                    score += min(1_000_000, max(0, area))
                    if "winuidesktopwin32windowclass" in cls.lower():
                        score += 500_000
                    if title.strip():
                        score += 30_000
                    if "microsoft" in title.lower() or "copilot" in title.lower():
                        score += 10_000

                    if score > best_score:
                        best_score = score
                        best_hwnd = hwnd_i

                hwnd = best_hwnd

                # Fallback to first match if scoring did not find a suitable candidate.
                if not hwnd:
                    try:
                        if hasattr(self.winman, "find_first_any"):
                            hwnd = self.winman.find_first_any(process_contains="copilot")
                    except Exception:
                        hwnd = None

                if hwnd and self.winman.focus_hwnd(hwnd):
                    time.sleep(max(self.delay, 0.6))
                    ok = bool(self._verify_copilot_foreground())
                    self._record_focus("copilot_app", ok, method="existing_window")
                    if ok:
                        try:
                            self._copilot_hwnd = int(hwnd)
                        except Exception:
                            self._copilot_hwnd = None
                        try:
                            info = self.winman.get_window_info(hwnd) if self.winman else {}
                            self._log_error_event(
                                "copilot_app_focus_selected",
                                ok=True,
                                hwnd=int(hwnd),
                                title=str(info.get("title") or "")[:120],
                                cls=str(info.get("class") or "")[:120],
                                process=str(info.get("process") or "")[:80],
                            )
                        except Exception:
                            pass
                        return True
        except Exception:
            pass
        try:
            # Use Win+C to toggle Copilot; pyautogui hotkey expects 'winleft'
            self.ctrl.press_keys(["winleft", "c"])
            # Increase settle time to allow OCR/foreground to stabilize
            time.sleep(max(self.delay, 1.2))
            # best-effort tiny input to ensure focus context
            self.ctrl.type_text("")
            ok = bool(self._verify_copilot_foreground())
            self._record_focus("copilot_app", ok, method="win_c")
            if ok:
                # Best-effort: store current foreground hwnd.
                try:
                    if self.winman:
                        self._copilot_hwnd = int(self.winman.get_foreground() or 0) or None
                except Exception:
                    self._copilot_hwnd = None
                return True
        except Exception:
            self.log("Failed to focus Copilot app via Win+C")

        # Final attempt: process/title fallback after Win+C
        try:
            if self.winman:
                hwnd = None
                try:
                    if hasattr(self.winman, "find_first_any"):
                        hwnd = self.winman.find_first_any(process_contains="copilot")
                except Exception:
                    hwnd = None
                if not hwnd:
                    for title_hint in ["copilot", "microsoft copilot", "copilot (preview)"]:
                        hwnd = self.winman.find_first(title_contains=title_hint)
                        if hwnd:
                            break
                if hwnd and self.winman.focus_hwnd(hwnd):
                    time.sleep(max(self.delay, 0.6))
                    ok = bool(self._verify_copilot_foreground())
                    self._record_focus("copilot_app", ok, method="after_win_c")
                    if ok:
                        try:
                            self._copilot_hwnd = int(hwnd)
                        except Exception:
                            self._copilot_hwnd = None
                        return True
        except Exception:
            pass

        self._record_focus("copilot_app", False, method="failed")
        try:
            self._log_error_event(
                "copilot_app_focus_failed_foreground",
                note="Could not make Copilot app the foreground window",
            )
        except Exception:
            pass
        return False

    def ask_copilot_app(self, question: str) -> bool:
        ok = self.focus_copilot_app()
        if self.dry_run:
            self.log(f"DRY-RUN ask Copilot app: {question}")
            return True
        try:
            # IMPORTANT: Many automation backends translate newlines into Enter presses.
            # In the Copilot app, Enter can send the message early, causing the prompt to be
            # split across multiple sends (and sometimes dropping the token line entirely).
            # Collapse whitespace to keep the request single-message and deterministic.
            question_to_type = str(question or "")
            if "\n" in question_to_type or "\r" in question_to_type:
                try:
                    collapsed = re.sub(r"\s+", " ", question_to_type).strip()
                    if collapsed and collapsed != question_to_type:
                        try:
                            self._log_error_event(
                                "copilot_app_prompt_sanitized",
                                orig_chars=len(question_to_type),
                                new_chars=len(collapsed),
                            )
                        except Exception:
                            pass
                        question_to_type = collapsed
                except Exception:
                    # Fail safe: if sanitization fails, keep original.
                    pass

            # Hard gate: while sending to Copilot app, reject any input unless the Copilot window is truly foreground.
            prev_gate = None
            try:
                prev_gate = getattr(self.ctrl, "_window_gate", None)
                # Add a tiny amount of hysteresis: foreground detection can briefly blip
                # (e.g. during WinUI transitions). We still keep strict "is VS Code" blocks.
                gate_ok_until = {"ts": 0.0}

                def _gate() -> bool:
                    try:
                        now = time.time()
                        if now <= float(gate_ok_until["ts"]):
                            return True
                        ok_fg = bool(self._verify_copilot_foreground())
                        if ok_fg:
                            gate_ok_until["ts"] = now + 0.9
                        return ok_fg
                    except Exception:
                        return False

                self.ctrl.set_window_gate(_gate)
            except Exception:
                prev_gate = None

            # Foreground gating: must be Copilot app and not VS Code
            is_app = self._verify_copilot_foreground()
            is_vscode = self._verify_vscode_foreground()
            # Sequential enforcement: if one is true, supersede the other to false
            if is_app:
                is_vscode = False
                try:
                    from .jsonlog import JsonActionLogger  # type: ignore
                    root = Path(__file__).resolve().parent.parent
                    JsonActionLogger(root / "logs" / "errors" / "events.jsonl").log(
                        "foreground_superseded", now="copilot_app", prev="vscode", prev_set_false=True)
                except Exception:
                    pass
            elif is_vscode:
                is_app = False
                try:
                    from .jsonlog import JsonActionLogger  # type: ignore
                    root = Path(__file__).resolve().parent.parent
                    JsonActionLogger(root / "logs" / "errors" / "events.jsonl").log(
                        "foreground_superseded", now="vscode", prev="copilot_app", prev_set_false=True)
                except Exception:
                    pass
            if not is_app or is_vscode:
                try:
                    from .jsonlog import JsonActionLogger  # type: ignore
                    root = Path(__file__).resolve().parent.parent
                    JsonActionLogger(root / "logs" / "errors" / "events.jsonl").log(
                        "copilot_app_send_blocked",
                        note="Foreground not Copilot app or VS Code still focused",
                        is_app=is_app,
                        is_vscode=is_vscode,
                    )
                except Exception:
                    pass
                return False

            # Assess the Copilot window context and recover if no conversation is selected.
            # This prevents the "typed but not sent" failure mode when Copilot is open but
            # sitting on a conversation picker / home surface.
            try:
                ok_prepare = bool(self._copilot_app_prepare_for_send())
                if not ok_prepare:
                    try:
                        self._log_error_event("copilot_app_prepare_failed", reason="needs_conversation_open_but_open_failed")
                    except Exception:
                        pass
                    return False
            except Exception:
                pass
            time.sleep(self.delay)
            # Observe and ensure input readiness to prevent wrong-field typing
            self._ocr_observe("before_copilot_app_type")
            if not self._copilot_app_input_ready():
                try:
                    from .jsonlog import JsonActionLogger  # type: ignore
                    root = Path(__file__).resolve().parent.parent
                    JsonActionLogger(root / "logs" / "errors" / "events.jsonl").log(
                        "input_aborted_not_ready", context="copilot_app_input_ready", reason="app_input_not_ready")
                except Exception:
                    pass
                return False
            # Type (retry once after refocus if the controller rejects input)
            typed_ok = False
            pasted_ok = False
            prev_clip = None
            for attempt in (1, 2):
                try:
                    typed_ok = bool(self.ctrl.type_text(question_to_type))
                except Exception:
                    typed_ok = False
                if typed_ok:
                    break

                # Fallback: clipboard paste is often more reliable on WinUI/WebView surfaces.
                # This uses _press_keys_copilot which enforces strict foreground gating.
                if (not typed_ok) and (not pasted_ok):
                    try:
                        if self.winman and hasattr(self.winman, "get_clipboard_text") and hasattr(self.winman, "set_clipboard_text"):
                            try:
                                prev_clip = self.winman.get_clipboard_text(timeout_s=0.35) or ""
                            except Exception:
                                prev_clip = None
                            if bool(self.winman.set_clipboard_text(question_to_type, timeout_s=0.6)):
                                # Best-effort ensure input has focus; then select all + paste.
                                try:
                                    self._copilot_app_input_ready()
                                except Exception:
                                    pass
                                sel_ok = bool(self._press_keys_copilot(["ctrl", "a"]))
                                paste_ok = bool(self._press_keys_copilot(["ctrl", "v"]))
                                pasted_ok = bool(paste_ok)
                                try:
                                    self._log_error_event(
                                        "copilot_app_paste_attempt",
                                        attempt=attempt,
                                        select_all_ok=bool(sel_ok),
                                        paste_ok=bool(paste_ok),
                                    )
                                except Exception:
                                    pass
                                if pasted_ok:
                                    typed_ok = True
                                    break
                    except Exception:
                        pasted_ok = False

                if attempt == 1:
                    try:
                        self._log_error_event("copilot_app_type_retry", note="type_text rejected; refocusing and retrying")
                    except Exception:
                        pass
                    try:
                        self.focus_copilot_app()
                        time.sleep(max(self.delay, 0.6))
                        self._copilot_app_input_ready()
                    except Exception:
                        pass
            if not typed_ok:
                self._log_error_event(
                    "copilot_app_type_failed",
                    reason="controller_rejected_or_failed",
                )
                return False
            self._ocr_observe("after_copilot_app_type")
            # Re-verify foreground before ENTER
            is_app_fg = False
            is_vscode_fg = False
            try:
                is_app_fg = bool(self._verify_copilot_foreground())
                is_vscode_fg = bool(self._verify_vscode_foreground())
            except Exception:
                pass

            # If VS Code is foreground, do not send Enter (unsafe).
            if is_vscode_fg:
                # Recovery: VS Code sometimes steals focus between type and enter.
                # Attempt a single refocus to Copilot app, then re-check and proceed.
                self._log_error_event(
                    "copilot_app_enter_refocus_attempt",
                    note="VS Code was foreground at enter-pre; refocusing Copilot app once",
                )
                try:
                    self.focus_copilot_app()
                    time.sleep(max(self.delay, 0.6))
                except Exception:
                    pass
                try:
                    is_app_fg = bool(self._verify_copilot_foreground())
                    is_vscode_fg = bool(self._verify_vscode_foreground())
                except Exception:
                    pass
                if is_vscode_fg:
                    self._log_error_event(
                        "input_aborted_focus_changed",
                        context="copilot_app_enter_pre",
                        reason="foreground_is_vscode_after_refocus",
                        is_app=is_app_fg,
                        is_vscode=is_vscode_fg,
                    )
                    return False

            # If Copilot foreground detection is flaky, still attempt Enter once
            # as long as VS Code is NOT foreground (user observed typed-but-not-sent).
            if not is_app_fg:
                self._log_error_event(
                    "copilot_app_foreground_uncertain_enter_attempt",
                    note="Foreground check did not confirm Copilot, but VS Code not foreground; attempting Enter",
                )

            enter_ok = False
            try:
                enter_ok = bool(self.ctrl.press_keys(["enter"]))
            except Exception:
                enter_ok = False
            if not enter_ok:
                self._log_error_event(
                    "copilot_app_enter_failed",
                    reason="controller_rejected_or_failed",
                )
                return False
            self._log_error_event("copilot_app_enter_pressed")
            self._ocr_observe("after_copilot_app_enter")
            time.sleep(self.delay)
            # Optional second attempt if still Copilot foreground (helps missed key events)
            try:
                if self._verify_copilot_foreground():
                    time.sleep(self.delay / 2)
                    second_ok = bool(self.ctrl.press_keys(["enter"]))
                    if second_ok:
                        self._log_error_event("copilot_app_enter_pressed", attempt=2)
                    else:
                        self._log_error_event("copilot_app_enter_failed", attempt=2, reason="controller_rejected_or_failed")
            except Exception:
                pass
            self.log("VSBridge: Copilot app asked")
            return True
        except Exception:
            return False
        finally:
            # Best-effort: restore clipboard if we changed it for paste.
            try:
                if prev_clip is not None and self.winman and hasattr(self.winman, "set_clipboard_text"):
                    self.winman.set_clipboard_text(prev_clip, timeout_s=0.4)
            except Exception:
                pass
            try:
                # Restore prior gate (or clear) after send attempt.
                if prev_gate is not None:
                    self.ctrl.set_window_gate(prev_gate)
                else:
                    self.ctrl.set_window_gate(None)
            except Exception:
                pass

    def compose_message_vscode_chat(self, text: str):
        """Type a message into the VS Code Copilot chat and send it.

        This method is careful about foreground focus and overlays, and inserts
        a brief pause between typing and pressing Enter to reduce the chance of
        modifier-key bleed (for example, Shift still being held resulting in a
        newline instead of send).
        """
        self.focus_copilot_chat_view()
        if self.dry_run:
            self.log(f"DRY-RUN compose message ({len(text)} chars)")
            return

        # Gate VS Code inputs to VS Code foreground to prevent wrong-window typing.
        prev_gate = None
        try:
            prev_gate = getattr(self.ctrl, "_window_gate", None)
            self.ctrl.set_window_gate(lambda: bool(self._verify_vscode_foreground()))
        except Exception:
            prev_gate = None

        try:
            time.sleep(self.delay)
            self._ocr_observe("compose_before_type")

            # Palette/search overlay handling
            if self._ocr_detect_wrong_field():
                try:
                    self.ctrl.press_keys(["esc"])
                    time.sleep(self.delay / 2)
                    self._ocr_observe("compose_overlay_closed")
                except Exception:
                    pass
                # If we're still in a wrong field after ESC, abort safely.
                if self._ocr_detect_wrong_field():
                    return

            # Ensure chat input is actually focused and ready
            if not self._vscode_chat_input_ready():
                try:
                    from .jsonlog import JsonActionLogger  # type: ignore
                    root = Path(__file__).resolve().parent.parent
                    JsonActionLogger(root / "logs" / "errors" / "events.jsonl").log(
                        "input_aborted_not_ready",
                        context="vscode_chat_compose_ready",
                        reason="chat_input_not_ready",
                    )
                except Exception:
                    pass
                return

            # Foreground must be VS Code; if true, supersede app to false
            is_vscode = bool(self._verify_vscode_foreground())
            if is_vscode:
                try:
                    from .jsonlog import JsonActionLogger  # type: ignore
                    root = Path(__file__).resolve().parent.parent
                    JsonActionLogger(root / "logs" / "errors" / "events.jsonl").log(
                        "foreground_superseded", now="vscode", prev="copilot_app", prev_set_false=True
                    )
                except Exception:
                    pass
            else:
                try:
                    from .jsonlog import JsonActionLogger  # type: ignore
                    root = Path(__file__).resolve().parent.parent
                    JsonActionLogger(root / "logs" / "errors" / "events.jsonl").log(
                        "input_aborted_focus_changed",
                        context="vscode_compose_type_pre",
                        reason="foreground_not_vscode",
                    )
                except Exception:
                    pass
                return

            # Type the message
            try:
                if not bool(self.ctrl.type_text(text)):
                    self._log_error_event("vscode_chat_type_failed", reason="controller_rejected_or_failed")
                    return
            except Exception:
                self._log_error_event("vscode_chat_type_failed", reason="controller_exception")
                return

            try:
                self._ocr_observe("compose_after_type")
            except Exception:
                pass

            # Small pause before Enter to avoid any lingering modifier state
            time.sleep(max(self.delay, 0.3))

            # Pre-enter foreground re-check; if true, supersede app to false
            is_vscode2 = bool(self._verify_vscode_foreground())
            if is_vscode2:
                try:
                    from .jsonlog import JsonActionLogger  # type: ignore
                    root = Path(__file__).resolve().parent.parent
                    JsonActionLogger(root / "logs" / "errors" / "events.jsonl").log(
                        "foreground_superseded", now="vscode", prev="copilot_app", prev_set_false=True
                    )
                except Exception:
                    pass
            else:
                try:
                    from .jsonlog import JsonActionLogger  # type: ignore
                    root = Path(__file__).resolve().parent.parent
                    JsonActionLogger(root / "logs" / "errors" / "events.jsonl").log(
                        "input_aborted_focus_changed",
                        context="vscode_compose_enter_pre",
                        reason="foreground_not_vscode",
                    )
                except Exception:
                    pass
                return

            # Press Enter to send
            try:
                if not bool(self.ctrl.press_keys(["enter"])):
                    self._log_error_event("vscode_chat_enter_failed", reason="controller_rejected_or_failed")
                    return
            except Exception:
                self._log_error_event("vscode_chat_enter_failed", reason="controller_exception")
                return

            try:
                self._ocr_observe("compose_after_enter")
            except Exception:
                pass

            time.sleep(self.delay)
        finally:
            # Restore prior gate (or clear) after send attempt.
            try:
                if prev_gate is not None:
                    self.ctrl.set_window_gate(prev_gate)
                else:
                    self.ctrl.set_window_gate(None)
            except Exception:
                pass

    def scroll_chat(self, direction: str = "down", steps: int = 3, amount: int = 500) -> bool:
        """Scroll Copilot chat or editor.
        - direction: 'down' or 'up'
        - steps: how many discrete scroll/key steps
        - amount: pixel-lines per step (pyautogui.scroll units)
        Uses mouse-wheel scroll when available, else PageUp/PageDown fallback.
        """
        self.log(f"VSBridge: Scroll {direction} x{steps}")
        if self.dry_run:
            self.log("DRY-RUN scroll")
            return True
        try:
            if pyautogui is not None and self.ctrl.is_controls_allowed():
                dy = -abs(amount) if direction.lower().startswith("down") else abs(amount)
                for _ in range(max(1, int(steps))):
                    pyautogui.scroll(dy)
                    time.sleep(self.delay / 2)
                return True
        except Exception:
            pass
        # Fallback: PageDown/PageUp keys
        key = "pagedown" if direction.lower().startswith("down") else "pageup"
        ok = False
        for _ in range(max(1, int(steps))):
            ok = self.ctrl.press_keys([key]) or ok
            time.sleep(self.delay / 2)
        return ok

    def copy_copilot_app_selected_message(
        self,
        ocr: Any,
        expect_substring: str,
        save_dir: Optional[Path] = None,
        max_page_down: int = 12,
        tab_count: int = 0,
        shift_tab_count: int = 0,
        tab_cycle_limit: int = 18,
        copy_retries: int = 2,
        clipboard_timeout_s: float = 0.8,
        max_focus_walk: int = 40,
        use_enter_copy_button: bool = True,
    ) -> dict:
        """Safely copy a Copilot app message by selection + clipboard.

        Procedure (fail-closed):
        - Ensure Copilot app is foreground (strict gate)
        - PageDown to focus messages
        - OCR-confirm expected substring is visible before copying
        - Clear clipboard, Ctrl+C, read clipboard
        - Verify clipboard contains expected substring
        """
        result = {
            "ok": False,
            "expected": str(expect_substring or ""),
            "steps": 0,
            "copied": False,
            "sentinel": "",
            "sentinel_set": False,
            "enter_copy_attempted": False,
            "enter_copy_enabled": bool(use_enter_copy_button),
            "clipboard_chars": 0,
            "clipboard_preview": "",
            "clipboard_contains_expected": False,
            "focus_moves": [],
        }

        expected = str(expect_substring or "").strip()
        generic_copy = not bool(expected)
        if generic_copy:
            # Still gated: we only attempt Enter-copy when OCR shows a Copy label
            # and we verify clipboard changed from sentinel and is non-trivial.
            result["generic_copy"] = True

        prefer_ui_copy = str(os.environ.get("COPILOT_COPY_PREFER_UI_COPY", "1")).strip().lower() in {"1", "true", "yes"}
        smart_nav = str(os.environ.get("COPILOT_COPY_SMART_NAV", "1")).strip().lower() in {"1", "true", "yes"}

        def _expected_visible(obj) -> bool:
            # obj may be a text string or a list/dict of detected elements.
            if generic_copy:
                return True
            try:
                # If caller provided detected elements, treat presence as visible
                if isinstance(obj, (list, tuple)):
                    return len(obj) > 0
                if isinstance(obj, dict) and obj.get("elements") is not None:
                    return len(obj.get("elements") or []) > 0
            except Exception:
                pass
            raw = (obj or "") if not isinstance(obj, (list, dict)) else ""
            raw = str(raw or "")
            if expected and expected in raw:
                return True
            exp_hex = re.sub(r"[^0-9a-fA-F]", "", expected).lower()
            if not exp_hex or len(exp_hex) < 8:
                return False
            raw_hex = re.sub(r"[^0-9a-fA-F]", "", raw).lower()
            return exp_hex in raw_hex

        def _clipboard_satisfies(clip: str, sentinel: str) -> bool:
            if (clip or "").strip() == (sentinel or "").strip():
                return False
            if generic_copy:
                return len((clip or "").strip()) >= 20
            if expected and expected in (clip or ""):
                return True
            exp_hex = re.sub(r"[^0-9a-fA-F]", "", expected).lower()
            if not exp_hex or len(exp_hex) < 8:
                return False
            clip_hex = re.sub(r"[^0-9a-fA-F]", "", (clip or "")).lower()
            return exp_hex in clip_hex

        if self.dry_run:
            result["ok"] = True
            result["copied"] = True
            result["clipboard_contains_expected"] = True
            return result

        # Strictly gate all keys to Copilot app foreground.
        prev_gate = None
        try:
            prev_gate = getattr(self.ctrl, "_window_gate", None)
            self.ctrl.set_window_gate(lambda: bool(self._verify_copilot_foreground()))
        except Exception:
            prev_gate = None

        try:
            if not self.focus_copilot_app():
                result["error"] = "copilot_not_foreground"
                return result

            # Optional: allow arrow-key walking when on message objects.
            # Copilot's focus chain sometimes requires arrows (e.g. Right) to reach per-message actions.
            use_arrows = str(os.environ.get("COPILOT_COPY_USE_ARROWS", "1")).strip().lower() in {"1", "true", "yes"}
            arrow_max_walk = int(os.environ.get("COPILOT_COPY_ARROW_MAX_WALK", "10"))
            arrow_right_warmup = int(os.environ.get("COPILOT_COPY_ARROW_RIGHT", "2"))
            arrow_left_warmup = int(os.environ.get("COPILOT_COPY_ARROW_LEFT", "1"))
            arrow_down_warmup = int(os.environ.get("COPILOT_COPY_ARROW_DOWN", "2"))
            arrow_up_warmup = int(os.environ.get("COPILOT_COPY_ARROW_UP", "0"))

            # When the message list item is focused, user-observed behavior: arrows are required to change
            # which message is focused; then Tab/Shift+Tab moves among per-message actions (Copy, etc.).
            # These knobs define that sequence.
            arrow_down_to_messages = int(os.environ.get("COPILOT_COPY_ARROW_DOWN_TO_MESSAGES", "2"))
            item_arrow_right = int(os.environ.get("COPILOT_COPY_ITEM_ARROW_RIGHT", "1"))
            item_arrow_down = int(os.environ.get("COPILOT_COPY_ITEM_ARROW_DOWN", "1"))
            item_then_tab = int(os.environ.get("COPILOT_COPY_ITEM_THEN_TAB", "1"))
            smart_nav_steps = int(os.environ.get("COPILOT_COPY_SMART_STEPS", "60"))

            # Best-effort: close overlays so PageDown navigates messages.
            try:
                self.ctrl.press_keys(["esc"]) 
                time.sleep(max(self.delay / 2, 0.15))
            except Exception:
                pass

            def _uia_focus_info() -> dict:
                """Best-effort focused-control snapshot via UIA."""
                info: dict = {
                    "name": "",
                    "class": "",
                    "ctrl": "",
                }
                try:
                    import uiautomation as auto  # type: ignore

                    fc = auto.GetFocusedControl()
                    try:
                        info["name"] = str(getattr(fc, "Name", "") or "")
                    except Exception:
                        info["name"] = ""
                    try:
                        info["class"] = str(getattr(fc, "ClassName", "") or "")
                    except Exception:
                        info["class"] = ""
                    try:
                        info["ctrl"] = str(getattr(fc, "ControlTypeName", "") or "")
                    except Exception:
                        info["ctrl"] = ""
                except Exception:
                    pass
                return info

            def _uia_is_message_item(uia: dict) -> bool:
                ctrl = (uia.get("ctrl") or "").lower()
                cls = (uia.get("class") or "").lower()
                # Empirically seen: ListViewItem / ListItemControl when a response message is focused.
                if "listitem" in ctrl:
                    return True
                if "listviewitem" in cls:
                    return True
                return False

            def _uia_is_input(uia: dict) -> bool:
                ctrl = (uia.get("ctrl") or "").lower()
                nm = (uia.get("name") or "").lower()
                if "edit" in ctrl:
                    return True
                # Common placeholder-style names.
                if "type a message" in nm or "send a message" in nm or "ask" in nm and "anything" in nm:
                    return True
                return False

            def _observe(move: str, idx: int) -> bool:
                cap = {}
                try:
                    cap = self.read_copilot_app_text(ocr, save_dir=save_dir, return_meta=True, focus_first=False) or {}
                except Exception:
                    cap = {}
                elems = (cap.get("elements") or []) if isinstance(cap, dict) else []
                visible = _expected_visible(elems)
                image_path = (cap.get("image_path") or "") if isinstance(cap, dict) else ""
                # Simple signature for "did anything change" detection.
                sig = (str(image_path), int(len(elems)), repr((elems or [])[:3]))
                try:
                    last_sig = _observe.__dict__.get("_last_sig")
                    streak = int(_observe.__dict__.get("_no_change_streak") or 0)
                    same = bool(last_sig == sig)
                    streak = (streak + 1) if same else 0
                    _observe.__dict__["_last_sig"] = sig
                    _observe.__dict__["_no_change_streak"] = streak
                    _observe.__dict__["_sig_same"] = same
                except Exception:
                    streak = 0
                    same = False
                try:
                    result["focus_moves"].append({
                        "move": move,
                        "i": idx,
                        "expected_visible": visible,
                        "elements_count": len(elems),
                        "preview_elements": repr((elems or [])[:3]),
                        "image_path": image_path,
                        "method": (cap.get("method") or "") if isinstance(cap, dict) else "",
                        "sig_same_as_prev": bool(same),
                        "no_change_streak": int(streak),
                    })
                except Exception:
                    pass
                return visible

            def _press_move(keys: List[str], label: str, idx: int) -> bool:
                ok = False
                # Focus thrash guard: if Copilot isn't foreground, re-acquire it before sending keys.
                try:
                    if not self._verify_copilot_foreground():
                        fg_info = {}
                        try:
                            if self.winman:
                                fg = self.winman.get_foreground()
                                if fg:
                                    fg_info = self.winman.get_window_info(fg) or {}
                        except Exception:
                            fg_info = {}
                        try:
                            self._log_error_event(
                                "copilot_app_focus_lost_before_move",
                                move=label,
                                keys=keys,
                                idx=idx,
                                fg_title=str((fg_info.get("title") or ""))[:180],
                                fg_process=str((fg_info.get("process") or ""))[:80],
                                fg_class=str((fg_info.get("class") or ""))[:80],
                            )
                        except Exception:
                            pass
                        if not self.focus_copilot_app():
                            return False
                        time.sleep(max(self.delay, 0.35))
                except Exception:
                    pass

                ok = False
                try:
                    ok = bool(self._press_keys_copilot(keys))
                except Exception:
                    ok = False
                try:
                    self._log_error_event("copilot_app_focus_move", move=label, ok=bool(ok), keys=keys)
                except Exception:
                    pass
                # Explicitly flag problems for tab-based navigation.
                try:
                    if (not ok) and (tuple(keys) in {("tab",), ("shift", "tab")}):
                        self._log_error_event(
                            "copilot_app_tab_move_failed",
                            move=label,
                            keys=keys,
                            idx=idx,
                        )
                except Exception:
                    pass
                time.sleep(max(self.delay / 2, 0.18))
                _observe(label + ":after", idx)

                # OCR/image-reactive recovery: if repeated tabbing doesn't change what we see,
                # we may not be in the right focus chain (or keys aren't taking effect).
                try:
                    streak = int(_observe.__dict__.get("_no_change_streak") or 0)
                    last_recover = int(_press_move.__dict__.get("_last_recover_idx") or -999999)
                    focus_keys = {("tab",), ("shift", "tab"), ("left",), ("right",), ("up",), ("down",)}
                    if streak >= 3 and (tuple(keys) in focus_keys) and (idx - last_recover) > 15:
                        _press_move.__dict__["_last_recover_idx"] = int(idx)
                        # Conservative: Esc to close overlays, then End/PageDown to re-anchor.
                        try:
                            self._press_keys_copilot(["esc"])
                        except Exception:
                            pass
                        time.sleep(max(self.delay / 2, 0.2))
                        try:
                            self._press_keys_copilot(["end"])
                            time.sleep(max(self.delay / 2, 0.2))
                            self._press_keys_copilot(["pagedown"])
                        except Exception:
                            pass
                        time.sleep(max(self.delay / 2, 0.2))
                        _observe("recovery", idx)
                except Exception:
                    pass
                return ok

            def _clipboard_set_sentinel(s: str) -> bool:
                try:
                    if self.winman and hasattr(self.winman, "set_clipboard_text"):
                        return bool(self.winman.set_clipboard_text(s, timeout_s=clipboard_timeout_s))
                except Exception:
                    return False
                return False

            def _clipboard_read() -> str:
                try:
                    if self.winman and hasattr(self.winman, "get_clipboard_text"):
                        return self.winman.get_clipboard_text(timeout_s=clipboard_timeout_s) or ""
                except Exception:
                    return ""
                return ""

            def _attempt_copy_with_fallback(sentinel: str, attempt: int) -> str:
                """Try to copy selected message. Returns clipboard text after attempt."""
                # Ctrl+C first
                try:
                    copied = bool(self._press_keys_copilot(["ctrl", "c"]))
                except Exception:
                    copied = False
                result["copied"] = result["copied"] or copied
                try:
                    result["focus_moves"].append({
                        "move": "copy",
                        "attempt": attempt,
                        "copied_keypress_ok": bool(copied),
                        "expected_visible": True,
                    })
                except Exception:
                    pass
                time.sleep(max(self.delay / 2, 0.2))
                clip = _clipboard_read()

                # If clipboard unchanged, try Ctrl+Insert.
                if (clip or "") == (sentinel or ""):
                    try:
                        copied2 = bool(self._press_keys_copilot(["ctrl", "insert"]))
                    except Exception:
                        copied2 = False
                    result["copied"] = result["copied"] or copied2
                    try:
                        result["focus_moves"].append({
                            "move": "copy_fallback_ctrl_insert",
                            "attempt": attempt,
                            "copied_keypress_ok": bool(copied2),
                            "expected_visible": True,
                        })
                    except Exception:
                        pass
                    time.sleep(max(self.delay / 2, 0.2))
                    clip = _clipboard_read()
                return clip or ""

            def _attempt_enter_copy_button(sentinel: str, attempt: int, ctx: str) -> str:
                """Try activating a focused 'Copy' button via Enter.

                Guardrails:
                - Only do this when OCR text indicates a 'copy' label is present on-screen.
                - Only do this after we have already performed focus moves (to avoid sending messages).
                """
                if not use_enter_copy_button:
                    return ""
                # Snapshot current screen text (no refocus) and look for copy UI.
                capx = {}
                try:
                    # Avoid tight loops OCRing the wrong surface; re-focus Copilot once if needed.
                    if not self._verify_copilot_foreground():
                        try:
                            self.focus_copilot_app()
                        except Exception:
                            pass
                        time.sleep(max(self.delay, 0.35))
                    capx = self.read_copilot_app_text(ocr, save_dir=save_dir, return_meta=True, focus_first=False) or {}
                except Exception:
                    capx = {}
                elemsx = (capx.get("elements") or []) if isinstance(capx, dict) else []
                low = ""
                # Avoid pressing Enter if we still appear to be on the input field.
                # Keep these specific; generic words like "ask" appear in normal responses.
                input_hints = [
                    "send a message",
                    "type a message",
                    "message input",
                    "write a message",
                    "ask anything",
                    "ask me anything",
                ]
                looks_like_input = False
                try:
                    imgp = (capx.get("image_path") or "") if isinstance(capx, dict) else ""
                    if elemsx and imgp:
                        from PIL import Image
                        im = Image.open(imgp)
                        w_img, h_img = im.size
                        for e in elemsx:
                            b = e.get("bbox") or {}
                            if (b.get("height") or 0) < 80 and (b.get("top") or 0) > (0.65 * h_img):
                                looks_like_input = True
                                break
                except Exception:
                    looks_like_input = False

                # Allow a brief settle for focus tooltip/label rendering after Shift+Tab.
                try:
                    tooltip_ms = int(os.environ.get("COPILOT_COPY_TOOLTIP_MS", "180"))
                    time.sleep(max(0, tooltip_ms) / 1000.0)
                except Exception:
                    pass

                # Targeted probes to detect a Copy label/tooltip near the focused button.
                probe_text = ""
                probe_images: List[str] = []
                found_copy_in_probe = False
                try:
                    if self.winman and hasattr(ocr, "capture_bbox_text"):
                        hwndp = None
                        try:
                            hwndp = self.winman.get_foreground()
                        except Exception:
                            hwndp = None
                        rect = self.winman.get_window_rect(hwndp) if hwndp else None
                        if rect and int(rect.get("width", 0)) > 50 and int(rect.get("height", 0)) > 50:
                            probes = [
                                {"left": 62, "top": 18, "width": 36, "height": 62},  # right-side strip
                                {"left": 55, "top": 55, "width": 43, "height": 40},  # lower-right quadrant
                                {"left": 2, "top": 55, "width": 40, "height": 40},   # lower-left quadrant
                                {"left": 8, "top": 78, "width": 84, "height": 18},   # bottom strip
                                {"left": 40, "top": 25, "width": 58, "height": 60},  # mid-right
                            ]
                            for pi, pc in enumerate(probes):
                                bx_left = int(rect["left"] + (rect["width"] * float(pc.get("left", 0)) / 100.0))
                                bx_top = int(rect["top"] + (rect["height"] * float(pc.get("top", 0)) / 100.0))
                                bx_w = int(max(1, rect["width"] * float(pc.get("width", 0)) / 100.0))
                                bx_h = int(max(1, rect["height"] * float(pc.get("height", 0)) / 100.0))
                                r = ocr.capture_bbox_text(
                                    {"left": bx_left, "top": bx_top, "width": bx_w, "height": bx_h},
                                    save_dir=save_dir,
                                    tag=f"copilot_copy_probe_{pi}",
                                    preprocess_mode="soft",
                                )
                                if r and r.get("ok"):
                                    try:
                                        probe_images.append(str(r.get("image_path") or ""))
                                    except Exception:
                                        pass
                                    elems = r.get("elements") if isinstance(r, dict) else None
                                    if elems:
                                        found_copy_in_probe = True

                            # If we still haven't seen 'copy', do a lightweight grid scan.
                            if not found_copy_in_probe:
                                try:
                                    grid_lefts = [8, 38, 68]
                                    grid_tops = [18, 46, 74]
                                    gi = 100
                                    for gy in grid_tops:
                                        for gx in grid_lefts:
                                            gi += 1
                                            bx_left = int(rect["left"] + (rect["width"] * (gx / 100.0)))
                                            bx_top = int(rect["top"] + (rect["height"] * (gy / 100.0)))
                                            bx_w = int(max(1, rect["width"] * 0.26))
                                            bx_h = int(max(1, rect["height"] * 0.16))
                                            r = ocr.capture_bbox_text(
                                                {"left": bx_left, "top": bx_top, "width": bx_w, "height": bx_h},
                                                save_dir=save_dir,
                                                tag=f"copilot_copy_probe_g{gi}",
                                                preprocess_mode="soft",
                                            )
                                            if r and r.get("ok"):
                                                try:
                                                    probe_images.append(str(r.get("image_path") or ""))
                                                except Exception:
                                                    pass
                                                elems = r.get("elements") if isinstance(r, dict) else None
                                                if elems:
                                                    found_copy_in_probe = True
                                                    break
                                                    break
                                        if found_copy_in_probe:
                                            break
                                except Exception:
                                    pass
                except Exception:
                    probe_text = probe_text or ""

                combined_low = (low + "\n" + (probe_text or "").lower()).strip()

                has_copy = ("copy" in combined_low)

                # If OCR cannot see a Copy label (icon-only UI), optionally fall back to UIA
                # to read the currently focused control name.
                uia_enabled = str(os.environ.get("COPILOT_COPY_USE_UIA", "1")).strip().lower() in {"1", "true", "yes"}
                uia_focus_name = ""
                uia_focus_class = ""
                uia_focus_ctrl = ""
                # Also collect UIA focus info for diagnostics even when OCR sees copy.
                if uia_enabled:
                    try:
                        uia = _uia_focus_info()
                        uia_focus_name = str(uia.get("name") or "")
                        uia_focus_class = str(uia.get("class") or "")
                        uia_focus_ctrl = str(uia.get("ctrl") or "")
                        if (not has_copy) and ("copy" in (uia_focus_name or "").lower()):
                            has_copy = True
                    except Exception:
                        pass

                # Log probe outcome even if we don't attempt Enter.
                try:
                    result["focus_moves"].append({
                        "move": "copy_probe",
                        "attempt": attempt,
                        "ctx": str(ctx),
                        "full_has_copy": ("copy" in low),
                        "combined_has_copy": bool(has_copy),
                        "input_hints_in_full": bool(looks_like_input),
                        "input_hints_in_combined": any(h in combined_low for h in input_hints),
                        "found_copy_in_probe": bool(found_copy_in_probe),
                        "uia_enabled": bool(uia_enabled),
                        "uia_focus_name": (uia_focus_name or "")[:120],
                        "uia_focus_class": (uia_focus_class or "")[:80],
                        "uia_focus_ctrl": (uia_focus_ctrl or "")[:80],
                        "probe_chars": len((probe_text or "").strip()),
                        "probe_preview": (probe_text or "").strip()[:180],
                        "probe_images": probe_images[:3],
                    })
                except Exception:
                    pass

                # Require positive Copy detection (OCR or UIA) before pressing Enter.
                if not has_copy:
                    return ""
                if (not generic_copy) and (not _expected_visible(txtx or "")):
                    return ""
                # Set sentinel and press Enter.
                _clipboard_set_sentinel(sentinel)
                try:
                    ok = bool(self._press_keys_copilot(["enter"]))
                except Exception:
                    ok = False
                result["enter_copy_attempted"] = True
                try:
                    self._log_error_event(
                        "copilot_app_enter_copy_attempt",
                        attempt=int(attempt),
                        ctx=str(ctx),
                        ok=bool(ok),
                    )
                except Exception:
                    pass
                try:
                    result["focus_moves"].append({
                        "move": "enter_copy_button",
                        "attempt": attempt,
                        "ctx": str(ctx),
                        "ok": bool(ok),
                        "image_path": (capx.get("image_path") or "") if isinstance(capx, dict) else "",
                        "method": (capx.get("method") or "") if isinstance(capx, dict) else "",
                        "probe_chars": len((probe_text or "").strip()),
                        "probe_preview": (probe_text or "").strip()[:140],
                    })
                except Exception:
                    pass
                time.sleep(max(self.delay / 2, 0.25))
                clip = _clipboard_read()
                return clip or ""

            # First: capture baseline.
            _observe("start", 0)

            # If we are stuck in the message input focus chain, user-observed fix is to use arrows
            # to move focus into the message list, then tab/shift-tab among message actions.
            if use_arrows and smart_nav and arrow_down_to_messages > 0:
                for j in range(max(0, int(arrow_down_to_messages))):
                    uia0 = _uia_focus_info() if str(os.environ.get("COPILOT_COPY_USE_UIA", "1")).strip().lower() in {"1","true","yes"} else {}
                    if _uia_is_input(uia0):
                        _press_move(["down"], "arrow_down_to_messages", 1500 + j)
                    else:
                        break

            # Ensure expected text is visible before any copy attempt (unless generic mode).
            found = True if generic_copy else _observe("pre_copy_confirm", 999)
            if (not generic_copy) and (not found):
                # Try jumping to the end (newest messages) then page down.
                _press_move(["end"], "end_seed", 1000)
                _press_move(["pagedown"], "pagedown_seed", 1001)
                found = _observe("pre_copy_confirm_after_seed", 1002)

            if (not generic_copy) and (not found):
                # If still not visible, page down a bit and re-check.
                for idx in range(1, max(1, int(max_page_down)) + 1):
                    if not self._verify_copilot_foreground():
                        if not self.focus_copilot_app():
                            result["error"] = "copilot_focus_lost"
                            return result
                        time.sleep(max(self.delay, 0.35))
                        _observe("refocus", 1100 + idx)
                    _press_move(["pagedown"], "pagedown", 1200 + idx)
                    if _observe("observe", 1300 + idx):
                        found = True
                        break

            if (not generic_copy) and (not found):
                result["error"] = "expected_not_observed_before_copy"
                return result

            # Copy with OCR-driven focus-walk and direction correction.
            clipboard_text = ""
            sentinel = f"COPILOT_SENTINEL_{int(time.time())}"
            result["sentinel"] = sentinel
            result["sentinel_set"] = _clipboard_set_sentinel(sentinel)

            # Try both directions; this is the "correct direction" fix.
            # User-observed behavior: from message input, Shift+Tab a couple times reaches Copy.
            # So we try Shift+Tab first, then Tab.
            directions: List[tuple[str, List[str], int]] = [
                ("shift_tab", ["shift", "tab"], max(0, int(shift_tab_count))),
                ("tab", ["tab"], max(0, int(tab_count))),
            ]
            if use_arrows:
                directions.extend(
                    [
                        ("right", ["right"], max(0, int(arrow_right_warmup))),
                        ("left", ["left"], max(0, int(arrow_left_warmup))),
                        ("down", ["down"], max(0, int(arrow_down_warmup))),
                        ("up", ["up"], max(0, int(arrow_up_warmup))),
                    ]
                )

            # Seed into message surface, then walk focus while the expected token remains visible.
            _press_move(["pagedown"], "pagedown_seed", 2000)
            _observe("seed_observe", 2001)

            def _smart_step(attempt: int, step: int) -> str:
                """Perform one stateful navigation step and optionally attempt Enter-copy.

                Returns clipboard text if a copy occurred; otherwise empty string.
                """
                # Stateful preference: first drive focus to the bottom-most message item using Down arrows,
                # then traverse per-message actions (Copy, etc.) with Right/Tab.
                nav_state = _smart_step.__dict__.setdefault(
                    "_nav",
                    {"down_no_change": 0, "at_bottom": False},
                )

                uia_enabled_local = str(os.environ.get("COPILOT_COPY_USE_UIA", "1")).strip().lower() in {"1", "true", "yes"}
                uia = _uia_focus_info() if uia_enabled_local else {}
                mode = "unknown"
                if _uia_is_message_item(uia):
                    mode = "message_item"
                elif _uia_is_input(uia):
                    mode = "input"
                else:
                    mode = (str(uia.get("ctrl") or "") or "unknown")[:60]

                try:
                    result["focus_moves"].append({
                        "move": "smart_nav_state",
                        "attempt": int(attempt),
                        "step": int(step),
                        "mode": str(mode),
                        "uia_ctrl": (str(uia.get("ctrl") or "") or "")[:80],
                        "uia_class": (str(uia.get("class") or "") or "")[:80],
                        "uia_name": (str(uia.get("name") or "") or "")[:120],
                    })
                except Exception:
                    pass

                # On message items: first move down through messages until we appear to be at bottom.
                # Then (and only then) move to the per-message action strip and hunt for Copy.
                if mode == "message_item":
                    if not bool(nav_state.get("at_bottom")):
                        before_name = str(uia.get("name") or "")
                        _press_move(["down"], "smart_item_down", 6100 + (attempt * 1000) + step)
                        uia_after = _uia_focus_info() if uia_enabled_local else {}
                        after_name = str(uia_after.get("name") or "")
                        if after_name and before_name and (after_name.strip() == before_name.strip()):
                            nav_state["down_no_change"] = int(nav_state.get("down_no_change") or 0) + 1
                        else:
                            nav_state["down_no_change"] = 0
                        # Two consecutive no-change downs is a strong signal we've hit the bottom.
                        if int(nav_state.get("down_no_change") or 0) >= 2:
                            nav_state["at_bottom"] = True
                        return ""

                    # At bottom: move to action strip and find Copy.
                    for k in range(max(0, int(item_arrow_right))):
                        _press_move(["right"], "smart_item_right", 6000 + (attempt * 1000) + (step * 10) + k)
                        clipr = _attempt_enter_copy_button(sentinel=sentinel, attempt=attempt, ctx=f"smart:right:{step}:{k}")
                        if clipr:
                            return clipr
                    for k in range(max(0, int(item_then_tab))):
                        _press_move(["tab"], "smart_item_tab", 6050 + (attempt * 1000) + (step * 10) + k)
                        clipt = _attempt_enter_copy_button(sentinel=sentinel, attempt=attempt, ctx=f"smart:tab:{step}:{k}")
                        if clipt:
                            return clipt
                    return ""

                # From input: down arrow is safer than Enter; try to drop into messages.
                if mode == "input":
                    before_name = str(uia.get("name") or "")
                    before_ctrl = str(uia.get("ctrl") or "")
                    _press_move(["down"], "smart_from_input_down", 6200 + (attempt * 1000) + step)
                    uia_after = _uia_focus_info() if uia_enabled_local else {}
                    after_ctrl = str(uia_after.get("ctrl") or "")
                    after_name = str(uia_after.get("name") or "")
                    if (after_ctrl.lower().find("edit") >= 0) and (before_ctrl.lower().find("edit") >= 0) and (after_name.strip() == before_name.strip()):
                        nav_state["input_down_no_change"] = int(nav_state.get("input_down_no_change") or 0) + 1
                    else:
                        nav_state["input_down_no_change"] = 0
                    if int(nav_state.get("input_down_no_change") or 0) >= 3:
                        # Escape hatch: some Copilot builds don't move off input with Down.
                        nav_state["input_down_no_change"] = 0
                        _press_move(["pagedown"], "smart_from_input_pagedown", 6210 + (attempt * 1000) + step)
                        _press_move(["tab"], "smart_from_input_tab", 6220 + (attempt * 1000) + step)
                    return ""

                # If we landed on a per-message action button (Good/Bad/Share/Copy), don't immediately shift-tab away.
                # First, try to activate Copy if we're already on it; otherwise scan along the action strip.
                ctrl_l = str(uia.get("ctrl") or "").lower()
                if "button" in ctrl_l:
                    action_scan = int(os.environ.get("COPILOT_COPY_ACTION_TAB_STEPS", "6"))

                    # If already on Copy, attempt Enter-copy now.
                    clip0 = _attempt_enter_copy_button(sentinel=sentinel, attempt=attempt, ctx=f"smart:button:{step}:pre")
                    if clip0:
                        return clip0

                    # Try Tab forward a few steps, attempting Enter-copy after each.
                    for j in range(max(1, action_scan)):
                        _press_move(["tab"], "smart_action_tab", 6350 + (attempt * 1000) + (step * 10) + j)
                        clipf = _attempt_enter_copy_button(sentinel=sentinel, attempt=attempt, ctx=f"smart:action_tab:{step}:{j}")
                        if clipf:
                            return clipf

                    # Try Shift+Tab backward a few steps.
                    for j in range(max(1, action_scan)):
                        _press_move(["shift", "tab"], "smart_action_shift_tab", 6450 + (attempt * 1000) + (step * 10) + j)
                        clipb = _attempt_enter_copy_button(sentinel=sentinel, attempt=attempt, ctx=f"smart:action_shift_tab:{step}:{j}")
                        if clipb:
                            return clipb

                    # If still not found, fall back toward message items to continue arrow navigation.
                    _press_move(["shift", "tab"], "smart_back_to_message", 6250 + (attempt * 1000) + step)
                    return ""

                # Otherwise: use Tab to traverse focusable elements, then try Enter-copy.
                _press_move(["tab"], "smart_tab", 6300 + (attempt * 1000) + step)
                return _attempt_enter_copy_button(sentinel=sentinel, attempt=attempt, ctx=f"smart:tab_only:{step}")


            for attempt in range(1, max(1, int(copy_retries)) + 1):
                # Confirm expected still visible before copying (skip in generic mode).
                if not generic_copy:
                    cap3 = {}
                    try:
                        cap3 = self.read_copilot_app_text(ocr, save_dir=save_dir, return_meta=True, focus_first=False) or {}
                    except Exception:
                        cap3 = {}
                    elems3 = (cap3.get("elements") or []) if isinstance(cap3, dict) else []
                    if not _expected_visible(elems3):
                        result["error"] = "lost_expected_before_copy"
                        return result

                # Reset sentinel each attempt.
                result["sentinel_set"] = _clipboard_set_sentinel(sentinel) or bool(result.get("sentinel_set"))

                # Smart navigation: alternate arrow+tab based on focused control type.
                if smart_nav and use_arrows:
                    for step in range(max(1, int(smart_nav_steps))):
                        # Only copy if expected is still visible.
                        if not _observe("smart_nav_confirm", 5000 + (attempt * 1000) + step):
                            break
                        clip_s = _smart_step(attempt=attempt, step=step)
                        if clip_s:
                            clipboard_text = clip_s
                            if _clipboard_satisfies(clipboard_text, sentinel):
                                break
                    if _clipboard_satisfies(clipboard_text, sentinel):
                        break

                for dir_label, dir_keys, warmup in directions:
                    # Warmup tuned steps first.
                    for w in range(int(warmup)):
                        _press_move(dir_keys, f"{dir_label}:warmup", 3000 + (attempt * 100) + w)

                        # After each warmup move, try the Copy button path (Enter) first.
                        # This matches the "Shift+Tab a couple times then Enter" workflow.
                        if _observe(f"{dir_label}:warmup_confirm", 3100 + (attempt * 100) + w):
                            clipw = _attempt_enter_copy_button(
                                sentinel=sentinel,
                                attempt=attempt,
                                ctx=f"{dir_label}:warmup:{w}",
                            )
                            if clipw:
                                clipboard_text = clipw
                                if _clipboard_satisfies(clipboard_text, sentinel):
                                    break
                            # When debugging Copy-button navigation, prefer UI Copy (Enter) before shortcut copy.
                            # In generic mode, shortcut copy can succeed early and mask failures to reach the Copy button.
                            if (not generic_copy) or (not prefer_ui_copy):
                                clipw2 = _attempt_copy_with_fallback(sentinel=sentinel, attempt=attempt)
                                if clipw2:
                                    clipboard_text = clipw2
                                    if _clipboard_satisfies(clipboard_text, sentinel):
                                        break

                    if _clipboard_satisfies(clipboard_text, sentinel):
                        break

                    # Walk further; attempt copy after each focus move.
                    step_cap = int(max_focus_walk)
                    if dir_label in {"right", "left", "up", "down"}:
                        step_cap = min(step_cap, max(1, int(arrow_max_walk)))
                    for step in range(max(1, step_cap)):
                        idx = 4000 + (attempt * 1000) + (step if dir_label == "tab" else 500 + step)
                        _press_move(dir_keys, f"{dir_label}:walk", idx)

                        # Only copy if expected is still visible.
                        if not _observe(f"{dir_label}:confirm", idx):
                            break

                        # First try: activate the UI Copy button (Enter) if it's on-screen.
                        clip = _attempt_enter_copy_button(sentinel=sentinel, attempt=attempt, ctx=f"{dir_label}:{step}")
                        if clip:
                            clipboard_text = clip
                            if _clipboard_satisfies(clipboard_text, sentinel):
                                break

                        # Fallback: classic copy shortcuts.
                        if (not generic_copy) or (not prefer_ui_copy):
                            clip = _attempt_copy_with_fallback(sentinel=sentinel, attempt=attempt)
                            clipboard_text = clip
                            if _clipboard_satisfies(clipboard_text, sentinel):
                                break

                        # If clipboard remained unchanged, focus likely isn't on a selectable message yet.
                        # Keep walking; do not refocus unless foreground is lost.

                    if _clipboard_satisfies(clipboard_text, sentinel):
                        break

                if _clipboard_satisfies(clipboard_text, sentinel):
                    break

                # In generic mode with UI-copy preference, allow a final shortcut-copy attempt if UI copy never succeeded.
                if generic_copy and prefer_ui_copy and (not _clipboard_satisfies(clipboard_text, sentinel)):
                    clip_final = _attempt_copy_with_fallback(sentinel=sentinel, attempt=attempt)
                    clipboard_text = clip_final
                    if _clipboard_satisfies(clipboard_text, sentinel):
                        break

                # Between attempts: nudge scroll a bit to keep the token in view.
                try:
                    self.scroll_chat(direction="down", steps=1, amount=350)
                except Exception:
                    pass
                time.sleep(max(self.delay, 0.25))

            result["clipboard_chars"] = len(clipboard_text or "")
            result["clipboard_preview"] = (clipboard_text or "")[:300]
            if (not generic_copy) and clipboard_text:
                if expected and expected in clipboard_text:
                    result["clipboard_contains_expected"] = True
                else:
                    exp_hex = re.sub(r"[^0-9a-fA-F]", "", expected).lower()
                    clip_hex = re.sub(r"[^0-9a-fA-F]", "", (clipboard_text or "")).lower()
                    result["clipboard_contains_expected"] = bool(exp_hex and len(exp_hex) >= 8 and exp_hex in clip_hex)
            result["ok"] = bool(_clipboard_satisfies(clipboard_text, sentinel))
            if not result["ok"]:
                if (clipboard_text or "") == sentinel:
                    result["error"] = "clipboard_unchanged_after_copy"
                else:
                    result["error"] = "clipboard_missing_expected" if not generic_copy else "clipboard_empty_or_too_short"
            return result
        finally:
            try:
                if prev_gate is not None:
                    self.ctrl.set_window_gate(prev_gate)
                else:
                    self.ctrl.set_window_gate(None)
            except Exception:
                pass

    def _verify_vscode_foreground(self) -> bool:
        try:
            if not self.winman:
                return True
            fg = self.winman.get_foreground()
            if not fg:
                return False
            info = self.winman.get_window_info(fg)
            title = (info.get("title") or "").lower()
            proc = (info.get("process") or "").lower()
            if proc and (proc == "code.exe" or proc.startswith("code")):
                return True
            # Do not use the window class alone for VS Code detection.
            # Many non-VSCode apps (including WebView2 surfaces) also use Chrome_WidgetWin_*.
            return ("visual studio code" in title) or ("code" == title) or ("vscode" in title)
        except Exception:
            return False

    def _verify_copilot_foreground(self) -> bool:
        try:
            if not self.winman:
                return True
            fg = self.winman.get_foreground()
            if not fg:
                return False
            info = self.winman.get_window_info(fg)
            title = (info.get("title") or "").lower()
            proc = (info.get("process") or "").lower()
            # Never treat VS Code as Copilot (VS Code can contain the word "Copilot" in the title).
            try:
                if self._verify_vscode_foreground():
                    return False
            except Exception:
                pass
            # Explicitly avoid misclassifying VS Code as Copilot even if the title contains "copilot".
            # This matters because Ctrl+C is used for clipboard copy and could cancel the terminal.
            if proc and (proc == "code.exe" or proc.startswith("code")):
                return False
            # Stable acceptance: if we previously focused a Copilot window, accept it when it is foreground.
            try:
                if self._copilot_hwnd and int(fg) == int(self._copilot_hwnd):
                    return True
            except Exception:
                pass
            if proc and "copilot" in proc:
                return True
            return ("copilot" in title) or ("microsoft copilot" in title)
        except Exception:
            return False

    def focus_terminal(self):
        """Focus or toggle the integrated terminal using safe fallbacks.
        First try Ctrl+` then try Command Palette commands.
        """
        self.log("VSBridge: Focus terminal")
        if self.dry_run:
            self.log("DRY-RUN focus terminal")
            return True
        try:
            self.ctrl.press_keys(["ctrl", "`"])
            time.sleep(self.delay)
            if self._verify_vscode_foreground():
                return True
        except Exception:
            pass
        # Fallback via Command Palette
        try:
            for cmd in [
                "Terminal: Focus Terminal",
                "Terminal: Toggle Terminal",
                "View: Toggle Terminal",
            ]:
                self.command_palette(cmd)
                time.sleep(self.delay)
                # After a toggle, try a small input to ensure focus
                self.ctrl.type_text("")
                if self._verify_vscode_foreground():
                    return True
        except Exception:
            self.log("Failed to focus terminal via fallbacks")
        return False

    def run_terminal_command(self, command: str) -> bool:
        """Focus the integrated terminal and run a command by typing it.
        Returns True if typed; relies on VS Code terminal to execute.
        """
        self.log(f"VSBridge: Run terminal command: {command[:120]}")
        if self.dry_run:
            self.log("DRY-RUN terminal command")
            return True
        try:
            # Defensive: ensure VS Code is foreground before trying to focus terminal.
            try:
                self.focus_vscode_window()
                time.sleep(self.delay / 2)
            except Exception:
                pass
            focused = self.focus_terminal()
            time.sleep(self.delay)
            # After toggling terminal, VS Code should still be foreground.
            try:
                self.focus_vscode_window()
                time.sleep(self.delay / 2)
            except Exception:
                pass
            if not focused:
                image_path = ""
                try:
                    # Capture the actual foreground window when terminal focus fails
                    if getattr(self, "_ocr", None) and self.winman:
                        fg = self.winman.get_foreground()
                        bbox = None
                        if fg:
                            rect = self.winman.get_window_rect(int(fg)) or {}
                            if rect and rect.get("width") and rect.get("height"):
                                bbox = {
                                    "left": int(rect.get("left", 0)),
                                    "top": int(rect.get("top", 0)),
                                    "width": int(rect.get("width", 0)),
                                    "height": int(rect.get("height", 0)),
                                }
                        root = Path(__file__).resolve().parent.parent
                        tag = "terminal_focus_failed"
                        if bbox:
                            res = self._ocr.capture_bbox_text(bbox=bbox, save_dir=root / "logs" / "ocr", tag=tag)
                        else:
                            res = self._ocr.capture_image(save_dir=root / "logs" / "ocr", tag=tag)
                        if isinstance(res, dict):
                            image_path = str(res.get("image_path") or "")
                except Exception:
                    image_path = ""

                self._log_error_event(
                    "terminal_focus_failed",
                    command_preview=command[:120],
                    expected_field="vscode_integrated_terminal_input",
                    likely_field="vscode_copilot_chat_input",
                    note="Terminal focus failed; skipping typing to avoid wrong-field input",
                    image_path=image_path,
                )
                return False
            # OCR pre-check (non-focus-stealing)
            self._ocr_observe("before_terminal_type")
            # Foreground must be VS Code
            if not self._verify_vscode_foreground():
                # One retry: refocus VS Code and re-check.
                try:
                    self.focus_vscode_window()
                    time.sleep(max(self.delay / 2, 0.25))
                except Exception:
                    pass
                if not self._verify_vscode_foreground():
                    self._log_error_event(
                        "input_aborted_focus_changed",
                        context="vscode_terminal_type_pre",
                        reason="foreground_not_vscode",
                    )
                    return False
            self.ctrl.type_text(command)
            # Pre-enter foreground re-check
            if not self._verify_vscode_foreground():
                self._log_error_event(
                    "input_aborted_focus_changed",
                    context="vscode_terminal_enter_pre",
                    reason="foreground_not_vscode",
                )
                return False
            self.ctrl.press_keys(["enter"])
            self._ocr_observe("after_terminal_enter")
            time.sleep(self.delay)
            return True
        except Exception:
            return False

    def _vscode_chat_input_ready(self) -> bool:
        """Best-effort to ensure VS Code chat input field is focused.
        Strategy: try small sequence: ensure VS Code foreground, send ESC, TAB cycles,
        re-observe OCR and check wrong-field detector. Limit attempts to avoid side-effects.
        Returns True if appears ready; False otherwise.
        """
        try:
            if not self._verify_vscode_foreground():
                return False
            # Initial observe
            self._ocr_observe("chat_input_ready_probe_start")
            if not self._ocr_detect_wrong_field():
                return True
            # Try to close overlays and move focus down with TAB a couple of times
            for i in range(3):
                try:
                    self.ctrl.press_keys(["esc"]) ; time.sleep(self.delay/2)
                    # Some UIs require TAB to land in the input
                    self.ctrl.press_keys(["tab"]) ; time.sleep(self.delay/2)
                except Exception:
                    pass
                self._ocr_observe(f"chat_input_ready_probe_tab_{i}")
                if not self._ocr_detect_wrong_field():
                    return True
            # As a fallback, try clicking near the bottom of the chat panel if we have coordinates
            try:
                bbox = getattr(self, "chat_panel_bbox", None)
                if bbox:
                    x = int(bbox[0] + bbox[2] * 0.5)
                    y = int(bbox[1] + bbox[3] * 0.92)
                    self.ctrl.click_at(x, y)
                    time.sleep(self.delay/2)
                    self._ocr_observe("chat_input_ready_probe_click")
                    if not self._ocr_detect_wrong_field():
                        return True
            except Exception:
                pass
            return False
        except Exception:
            return False

    def read_copilot_chat_text(
        self,
        ocr: Any,
        save_dir: Optional[Path] = None,
        return_meta: bool = False,
    ) -> Any:
        """Focus Copilot chat and attempt OCR capture via provided ocr helper.

        When ``return_meta`` is False (default), returns the extracted text string.
        When ``return_meta`` is True, returns a dict with keys like
        ``ok``, ``text``, ``image_path``, ``elements`` and ``method`` so
        callers such as verification scripts can inspect additional detail.
        """
        try:
            self.focus_copilot_chat_view()
            # Allow configurable settle time for Copilot to render fully
            settle_ms = 600
            try:
                cfg = getattr(ocr, "cfg", {}) or {}
                settle_ms = int(cfg.get("chat_settle_ms", settle_ms))
            except Exception:
                pass
            time.sleep(max(0, settle_ms) / 1000.0)

            # Targeted ROI override: chat_region_percent or targets.vscode_chat
            alt_region = None
            orig_region = None
            try:
                cfg = getattr(ocr, "cfg", {}) or {}
                alt_region = cfg.get("chat_region_percent")
                if not alt_region:
                    alt_region = (cfg.get("targets") or {}).get("vscode_chat")
                if alt_region:
                    orig_region = getattr(ocr, "region_percent", None)
                    setattr(ocr, "region_percent", alt_region)
            except Exception:
                pass

            try:
                res = ocr.capture_chat_text(save_dir=save_dir)
            finally:
                try:
                    if alt_region and orig_region is not None:
                        setattr(ocr, "region_percent", orig_region)
                except Exception:
                    pass

            if not (res or {}).get("ok"):
                err = (res or {}).get("error") if isinstance(res, dict) else None
                self.log(f"capture failed: {err}")
                if return_meta:
                    return {
                        "ok": False,
                        "text": "",
                        "error": str(err or "ocr_failed"),
                        "image_path": str((res or {}).get("image_path") or "") if isinstance(res, dict) else "",
                        "method": "chat",
                    }
                return ""

            text = str(res.get("text") or "") if isinstance(res, dict) else ""
            elems = (res.get("elements") or []) if isinstance(res, dict) else []

            # Emit observation manifest for cleanup daemon
            try:
                root = Path(__file__).resolve().parent.parent
                obs = {
                    "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "source": "vscode_chat",
                    "observed": True,
                    "deleteable": True,
                    "image_path": str(res.get("image_path") or ""),
                    "elements": len(elems),
                    "chars": len(text),
                }
                p = root / "logs" / "ocr" / "observations.jsonl"
                p.parent.mkdir(parents=True, exist_ok=True)
                with p.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(obs) + "\n")
            except Exception:
                pass

            if text:
                self.log(f"OCR captured {len(text)} chars from Copilot chat")
            elif elems:
                self.log(f"Captured {len(elems)} elements from Copilot chat")
            else:
                self.log("Captured no text/elements from Copilot chat")

            if return_meta:
                return {
                    "ok": True,
                    "text": text,
                    "image_path": str(res.get("image_path") or ""),
                    "elements": elems,
                    "method": "chat",
                }
            return text
        except Exception as e:
            self.log(f"OCR exception: {e}")
            if return_meta:
                return {"ok": False, "text": "", "error": f"exception:{e}", "image_path": "", "method": "chat"}
            return ""

    def read_copilot_app_text(
        self,
        ocr: Any,
        save_dir: Optional[Path] = None,
        return_meta: bool = False,
        *,
        focus_first: bool = True,
    ) -> Any:
        """Focus Windows Copilot app and OCR its panel using optional app-specific ROI.

        Honors optional OCR cfg keys:
        - app_settle_ms: wait time before capture (default 800ms)
        - app_region_percent: ROI override for app-only capture
        """
        try:
            if focus_first:
                self.focus_copilot_app()
            # If VS Code is foreground, this is definitely the wrong surface.
            try:
                if self._verify_vscode_foreground():
                    try:
                        from .jsonlog import JsonActionLogger  # type: ignore
                        root = Path(__file__).resolve().parent.parent
                        JsonActionLogger(root / "logs" / "errors" / "events.jsonl").log(
                            "copilot_app_read_wrong_surface",
                            note="OCR read attempted but VS Code was foreground",
                        )
                    except Exception:
                        pass
                    if return_meta:
                        return {"ok": False, "text": "", "error": "vscode_foreground", "image_path": "", "method": "none"}
                    return ""
            except Exception:
                pass

            if not self._verify_copilot_foreground():
                try:
                    from .jsonlog import JsonActionLogger  # type: ignore
                    root = Path(__file__).resolve().parent.parent
                    JsonActionLogger(root / "logs" / "errors" / "events.jsonl").log(
                        "copilot_app_not_foreground_when_read",
                        note="OCR read attempted while Copilot app not foreground",
                    )
                except Exception:
                    pass
                if return_meta:
                    return {"ok": False, "text": "", "error": "copilot_not_foreground", "image_path": "", "method": "none"}
                return ""
            settle_ms = 800
            alt_region = None
            try:
                cfg = getattr(ocr, "cfg", {}) or {}
                settle_ms = int(cfg.get("app_settle_ms", settle_ms))
                alt_region = cfg.get("app_region_percent")
                if not alt_region:
                    alt_region = (cfg.get("targets") or {}).get("copilot_app")
            except Exception:
                pass
            # Optional runtime override (useful for smoke tests).
            try:
                env_settle = os.environ.get("COPILOT_APP_SETTLE_MS")
                if env_settle is not None and str(env_settle).strip() != "":
                    settle_ms = int(str(env_settle).strip())
            except Exception:
                pass
            time.sleep(max(0, settle_ms) / 1000.0)

            def _looks_like_vscode_ui(txt: str) -> bool:
                try:
                    upper = (txt or "").upper()
                    strong_markers = [
                        "OPEN EDITORS",
                        "EXPLORER",
                        "SOURCE CONTROL",
                        "RUN AND DEBUG",
                        "EXTENSIONS",
                        "DEBUG CONSOLE",
                        "COMMAND PALETTE",
                        "PROBLEMS",
                        "OUTPUT",
                    ]
                    hits = [m for m in strong_markers if m in upper]
                    return len(hits) >= 2
                except Exception:
                    return False

            # Prefer capturing *inside the Copilot window rect* when available.
            try:
                if self.winman:
                    hwnd = None
                    # Most reliable: if Copilot is verified foreground, use foreground hwnd.
                    try:
                        hwnd = self.winman.get_foreground()
                    except Exception:
                        hwnd = None

                    # Then prefer the last known Copilot hwnd for stability.
                    if not hwnd:
                        try:
                            if self._copilot_hwnd:
                                hwnd = int(self._copilot_hwnd)
                        except Exception:
                            hwnd = None

                    # Final fallback: search for a Copilot window.
                    if not hwnd:
                        try:
                            if hasattr(self.winman, "find_first_any"):
                                hwnd = self.winman.find_first_any(process_contains="copilot")
                        except Exception:
                            hwnd = None
                    if not hwnd:
                        for title_hint in ["copilot", "microsoft copilot", "copilot (preview)"]:
                            hwnd = self.winman.find_first(title_contains=title_hint)
                            if hwnd:
                                break

                    # Cache hwnd when we have one.
                    try:
                        if hwnd:
                            self._copilot_hwnd = int(hwnd)
                    except Exception:
                        pass
                    rect = self.winman.get_window_rect(hwnd) if hwnd else None
                    if rect and int(rect.get("width", 0)) > 50 and int(rect.get("height", 0)) > 50 and hasattr(ocr, "capture_bbox_text"):
                        # Crop away title bar and the input area; focus on conversation content.
                        crop = None
                        try:
                            cfg = getattr(ocr, "cfg", {}) or {}
                            crop = cfg.get("app_window_crop_percent")
                        except Exception:
                            crop = None
                        if not isinstance(crop, dict):
                            # Default: capture most of the window content, including the lower
                            # conversation area. Excluding too much bottom area can miss the
                            # most recent prompt/reply.
                            crop = {"left": 4, "top": 6, "width": 92, "height": 90}
                        bx_left = int(rect["left"] + (rect["width"] * float(crop.get("left", 5)) / 100.0))
                        bx_top = int(rect["top"] + (rect["height"] * float(crop.get("top", 8)) / 100.0))
                        bx_w = int(max(1, rect["width"] * float(crop.get("width", 90)) / 100.0))
                        bx_h = int(max(1, rect["height"] * float(crop.get("height", 78)) / 100.0))
                        res = ocr.capture_bbox_text({"left": bx_left, "top": bx_top, "width": bx_w, "height": bx_h}, save_dir=save_dir, tag="copilot_app")
                        if res and res.get("ok"):
                            elems = res.get("elements") if isinstance(res, dict) else None
                            try:
                                self._log_error_event("copilot_app_image_bbox_used", elements=int(len(elems) if elems else 0))
                            except Exception:
                                pass
                            # Emit observation manifest for cleanup daemon (bbox path too)
                            try:
                                root = Path(__file__).resolve().parent.parent
                                obs = {
                                    "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                                    "source": "copilot_app",
                                    "observed": True,
                                    "deleteable": True,
                                    "image_path": str(res.get("image_path") or ""),
                                    "elements": len(elems or []),
                                }
                                p = root / "logs" / "ocr" / "observations.jsonl"
                                p.parent.mkdir(parents=True, exist_ok=True)
                                with p.open("a", encoding="utf-8") as f:
                                    f.write(json.dumps(obs) + "\n")
                            except Exception:
                                pass
                            if elems:
                                self.log(f"Captured {len(elems)} elements from Copilot app (bbox)")
                            else:
                                self.log("Captured no elements (app bbox)")
                            if return_meta:
                                return {
                                    "ok": True,
                                    "text": "",
                                    "elements": elems or [],
                                    "image_path": str(res.get("image_path") or ""),
                                    "method": "bbox",
                                }
                            return ""
            except Exception:
                pass

            # If we don't have an explicit Copilot-app region override, do NOT fall back to
            # the default chat ROI (it often points at VS Code and produces misleading results).
            if not alt_region:
                if return_meta:
                    return {
                        "ok": False,
                        "text": "",
                        "error": "no_bbox_and_no_app_region",
                        "image_path": "",
                        "method": "none",
                    }
                return ""

            # Temporarily override region/monitor and capture.
            orig_region = None
            orig_monitor = None
            best = None
            best_chars = -1
            best_monitor = None
            try:
                if alt_region:
                    orig_region = getattr(ocr, "region_percent", None)
                    setattr(ocr, "region_percent", alt_region)
                orig_monitor = getattr(ocr, "monitor_index", None)

                # If we can determine monitor count, scan them; Copilot overlay is often on a different monitor.
                monitor_candidates = None
                try:
                    from mss import mss  # type: ignore
                    with mss() as sct:
                        # sct.monitors[0] is the virtual bounding box; real monitors start at 1.
                        monitor_candidates = list(range(1, max(1, len(sct.monitors))))
                except Exception:
                    monitor_candidates = None

                if not monitor_candidates:
                    res = ocr.capture_chat_text(save_dir=save_dir)
                    best = res
                    best_monitor = getattr(ocr, "monitor_index", None)
                    best_chars = len(((res or {}).get("elements") or []))
                else:
                    for mi in monitor_candidates:
                        try:
                            setattr(ocr, "monitor_index", int(mi))
                        except Exception:
                            pass
                        res = ocr.capture_chat_text(save_dir=save_dir)
                        if not (res or {}).get("ok"):
                            continue
                        elems_here = ((res or {}).get("elements") or [])
                        # If this monitor looks like VSCode UI via text heuristic, skip (best-effort).
                        txt = ""
                        if _looks_like_vscode_ui(txt):
                            continue
                        chars = len(elems_here)
                        if chars > best_chars:
                            best = res
                            best_chars = chars
                            best_monitor = mi
            finally:
                try:
                    if alt_region and orig_region is not None:
                        setattr(ocr, "region_percent", orig_region)
                except Exception:
                    pass
                try:
                    if orig_monitor is not None:
                        setattr(ocr, "monitor_index", orig_monitor)
                except Exception:
                    pass

            res = best or {"ok": False, "text": "", "error": "no_capture"}
            try:
                if best_monitor is not None:
                    self._log_error_event("copilot_app_ocr_monitor_selected", monitor_index=int(best_monitor), chars=int(best_chars))
            except Exception:
                pass
            if not res.get("ok"):
                self.log(f"OCR (app) failed: {res.get('error')}")
                if return_meta:
                    return {"ok": False, "text": "", "error": str(res.get("error") or "ocr_failed"), "image_path": str(res.get("image_path") or ""), "method": "region"}
                return ""
            elems = (res.get("elements") or []) if isinstance(res, dict) else []

            # Heuristic wrong-surface detection: Copilot app capture should not look like VS Code UI.
            try:
                from .jsonlog import JsonActionLogger  # type: ignore
                root = Path(__file__).resolve().parent.parent
                # If many elements or a very large detected panel exists, assume we captured VS Code chrome
                large_panel = any((e.get("bbox", {}).get("width", 0) > 600 or e.get("bbox", {}).get("height", 0) > 400) for e in elems)
                many_elements = len(elems) > 40
                if large_panel or many_elements:
                    try:
                        JsonActionLogger(root / "logs" / "errors" / "events.jsonl").log(
                            "copilot_app_read_wrong_surface",
                            note="Capture appears to contain VS Code UI",
                            elements_count=len(elems),
                        )
                    except Exception:
                        pass
                    if return_meta:
                        return {"ok": False, "text": "", "error": "wrong_surface", "image_path": str(res.get("image_path") or ""), "method": "region"}
                    return ""
            except Exception:
                pass

            # Emit observation manifest for cleanup daemon (image + element count)
            try:
                root = Path(__file__).resolve().parent.parent
                obs = {
                    "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "source": "copilot_app",
                    "observed": True,
                    "deleteable": True,
                    "image_path": str(res.get("image_path") or ""),
                    "elements": len(elems),
                }
                p = root / "logs" / "ocr" / "observations.jsonl"
                p.parent.mkdir(parents=True, exist_ok=True)
                with p.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(obs) + "\n")
            except Exception:
                pass
            if elems:
                self.log(f"Captured {len(elems)} elements from Copilot app")
            else:
                self.log("Captured no elements (app)")
            if return_meta:
                return {"ok": True, "text": "", "image_path": str(res.get("image_path") or ""), "elements": len(elems), "method": "region"}
            return ""
        except Exception as e:
            self.log(f"OCR app exception: {e}")
            if return_meta:
                return {"ok": False, "text": "", "error": f"exception:{e}", "image_path": "", "method": "none"}
            return ""

    def focus_vscode_window(self) -> bool:
        if not self.winman:
            return False
        try:
            if self._verify_vscode_foreground():
                self._record_focus("vscode", True, method="already_foreground")
                return True
        except Exception:
            pass

        def _focus_and_verify(hwnd: int, method: str) -> bool:
            if not hwnd:
                return False
            ok = False
            try:
                ok = bool(self.winman.focus_hwnd(hwnd))
            except Exception:
                ok = False
            try:
                time.sleep(max(self.delay / 2, 0.12))
            except Exception:
                pass
            try:
                if ok and self._verify_vscode_foreground():
                    self.log("Focused VS Code window")
                    self._record_focus("vscode", True, method=method)
                    return True
            except Exception:
                pass
            self._record_focus("vscode", False, method=f"{method}_unverified")
            return False

        # Prefer a process-based match (robust across localized/atypical titles).
        try:
            if hasattr(self.winman, "find_first_any"):
                hwnd = self.winman.find_first_any(process_contains="code")
                if hwnd and _focus_and_verify(hwnd, method="process_match"):
                    return True
        except Exception:
            pass

        # Fallback: common window title patterns for VS Code.
        candidates = ["visual studio code", " - visual studio code", "code"]
        for sub in candidates:
            try:
                hwnd = self.winman.find_first(title_contains=sub)
            except Exception:
                hwnd = None
            if hwnd and _focus_and_verify(hwnd, method="title_match"):
                return True
        self.log("VS Code window not found to focus")
        self._record_focus("vscode", False, method="not_found")
        return False

    def _copilot_app_input_ready(self) -> bool:
        """Ensure Copilot app input field is focused using OCR and minimal actions.
        Attempts ESC to close overlays and clicks near expected input area if known.
        """
        try:
            if not self._verify_copilot_foreground():
                return False
            self._ocr_observe("copilot_app_ready_probe_start")
            # Close overlays (best-effort).
            for i in range(2):
                try:
                    self.ctrl.press_keys(["esc"])
                    time.sleep(self.delay / 2)
                except Exception:
                    pass
                self._ocr_observe(f"copilot_app_ready_probe_esc_{i}")

            # Prefer clicking into the Copilot app window's bottom-center to focus the input.
            # Using VS Code ROI OCR heuristics here is unreliable when Copilot is foreground.
            rect = None
            try:
                if self.winman and hasattr(self.winman, "find_first_any"):
                    hwnd = self.winman.find_first_any(process_contains="copilot")
                    rect = self.winman.get_window_rect(hwnd) if hwnd else None
            except Exception:
                rect = None
            if not rect:
                try:
                    bbox = getattr(self, "copilot_app_bbox", None)
                    if bbox:
                        rect = {"left": bbox[0], "top": bbox[1], "width": bbox[2], "height": bbox[3]}
                except Exception:
                    rect = None

            if rect and int(rect.get("width", 0)) > 50 and int(rect.get("height", 0)) > 50:
                x = int(rect["left"] + rect["width"] * 0.5)
                y = int(rect["top"] + rect["height"] * 0.92)
                self.ctrl.click_at(x, y)
                time.sleep(self.delay / 2)
                self._ocr_observe("copilot_app_ready_probe_click")
                return True

            # Last resort: proceed even without a known rect; foreground gating still protects us.
            return True
        except Exception:
            return False

    def _copilot_app_prepare_for_send(self) -> bool:
        """Best-effort prepare Copilot app so a message can be sent.

        If Copilot is foreground but no conversation is selected (e.g., picker screen),
        attempt to open the most recent conversation from the sidebar, then refocus input.
        Logs an assessment event for diagnostics.
        """
        if self.dry_run:
            return True
        try:
            if not self._verify_copilot_foreground():
                return False
        except Exception:
            return False

        assessment = self._copilot_app_assess_context()
        try:
            self._log_error_event("copilot_app_assess", **assessment)
        except Exception:
            pass

        # Only take disruptive action when it looks like we're on a picker/home surface.
        if not bool(assessment.get("needs_conversation_open")):
            return True

        opened = False
        try:
            opened = bool(self._copilot_app_open_most_recent_conversation())
        except Exception:
            opened = False
        try:
            self._log_error_event("copilot_app_open_recent", ok=bool(opened))
        except Exception:
            pass

        # Always try to refocus the input after navigation.
        try:
            self._copilot_app_input_ready()
        except Exception:
            pass
        return bool(opened)

    def _copilot_app_assess_context(self) -> dict:
        """Return a lightweight assessment of what the Copilot window looks like."""
        info: dict = {
            "is_foreground": False,
            "focused_ctrl": "",
            "focused_name": "",
            "sidebar_listitems": 0,
            "sidebar_candidates": 0,
            "has_conversations_header": False,
            "has_pages_header": False,
            "needs_conversation_open": False,
        }
        try:
            info["is_foreground"] = bool(self._verify_copilot_foreground())
        except Exception:
            info["is_foreground"] = False
        if not info["is_foreground"]:
            return info

        # UIA snapshot: focused control and presence of sidebar-like list items.
        try:
            import uiautomation as auto  # type: ignore

            fc = auto.GetFocusedControl()
            try:
                info["focused_ctrl"] = str(getattr(fc, "ControlTypeName", "") or "")
            except Exception:
                info["focused_ctrl"] = ""
            try:
                info["focused_name"] = str(getattr(fc, "Name", "") or "")
            except Exception:
                info["focused_name"] = ""

            # Estimate if we have a sidebar chat list by counting list items on the left third.
            hwnd = None
            rect = None
            try:
                if self.winman:
                    hwnd = self.winman.get_foreground()
                    rect = self.winman.get_window_rect(hwnd) if hwnd else None
            except Exception:
                hwnd, rect = None, None
            root = None
            try:
                if hwnd:
                    root = auto.ControlFromHandle(int(hwnd))
            except Exception:
                root = None
            if root is None:
                try:
                    root = fc.GetTopLevelControl()
                except Exception:
                    root = None

            if root is not None and rect and int(rect.get("width", 0)) > 100:
                win_left = int(rect.get("left", 0))
                win_w = int(rect.get("width", 0))
                left_cutoff = win_left + int(win_w * 0.45)

                listitems = []
                try:
                    # Depth-limited walk; WinUI trees can be large.
                    for c in root.GetChildren():
                        listitems.extend(c.GetChildren())
                except Exception:
                    listitems = []

                # Fallback: use ControlTypeName scanning if available.
                scanned = 0
                sidebar_hits = 0
                sidebar_candidates = 0
                has_conversations = False
                has_pages = False
                try:
                    walker = auto.WalkControl(root, maxDepth=8)
                    for ctl, _depth in walker:
                        scanned += 1
                        if scanned > 1400:
                            break
                        # Track section headers we care about.
                        try:
                            ctn = str(getattr(ctl, "ControlTypeName", "") or "").lower()
                        except Exception:
                            ctn = ""
                        if ctn in {"textcontrol", "hyperlinkcontrol", "headercontrol", "groupcontrol"}:
                            try:
                                nm0 = str(getattr(ctl, "Name", "") or "").strip().lower()
                            except Exception:
                                nm0 = ""
                            if nm0 == "conversations":
                                has_conversations = True
                            if nm0 == "pages":
                                has_pages = True
                        try:
                            if str(getattr(ctl, "ControlTypeName", "") or "").lower() != "listitemcontrol":
                                continue
                        except Exception:
                            continue
                        nm = ""
                        try:
                            nm = str(getattr(ctl, "Name", "") or "")
                        except Exception:
                            nm = ""
                        if not nm or len(nm.strip()) < 2:
                            continue
                        sidebar_candidates += 1
                        try:
                            br = getattr(ctl, "BoundingRectangle", None)
                            cx = int((br.left + br.right) / 2) if br else 0
                        except Exception:
                            cx = 0
                        if cx and cx < left_cutoff:
                            sidebar_hits += 1
                except Exception:
                    sidebar_hits = 0
                    sidebar_candidates = 0

                info["has_conversations_header"] = bool(has_conversations)
                info["has_pages_header"] = bool(has_pages)

                info["sidebar_listitems"] = int(sidebar_hits)
                info["sidebar_candidates"] = int(sidebar_candidates)
        except Exception:
            pass

        # Heuristic: if we have a sidebar list but focus is on a generic pane (not an Edit)
        # it often means no conversation is open.
        try:
            focused_ctrl_l = str(info.get("focused_ctrl") or "").lower()
            focused_name_l = str(info.get("focused_name") or "").strip().lower()

            # 1) Picker/home surfaces: focus is not an edit field.
            if int(info.get("sidebar_listitems") or 0) >= 2 and "edit" not in focused_ctrl_l:
                info["needs_conversation_open"] = True

            # 2) Pages editor surfaces: focus *is* an edit field, but not the chat input.
            # If we see a Pages header, and focus isn't the known chat input, treat as needing a conversation open.
            if (not info.get("needs_conversation_open")) and bool(info.get("has_pages_header")):
                if ("edit" in focused_ctrl_l) and ("ask anything" not in focused_name_l):
                    info["needs_conversation_open"] = True
        except Exception:
            pass
        return info

    def _copilot_app_open_most_recent_conversation(self) -> bool:
        """Use UI Automation to click the most recent conversation in Copilot's sidebar."""
        if self.dry_run:
            return True
        if not self.winman:
            return False
        try:
            if not self._verify_copilot_foreground():
                return False
        except Exception:
            return False

        hwnd = None
        rect = None
        try:
            hwnd = self.winman.get_foreground()
            rect = self.winman.get_window_rect(hwnd) if hwnd else None
        except Exception:
            hwnd, rect = None, None
        if not hwnd or not rect:
            return False

        try:
            import uiautomation as auto  # type: ignore

            root = auto.ControlFromHandle(int(hwnd))
            win_left = int(rect.get("left", 0))
            win_w = int(rect.get("width", 0))
            left_cutoff = win_left + int(win_w * 0.45)

            items = []
            scanned = 0
            # Prefer items that live under the explicit "Conversations" section header.
            conv_y = None
            pages_y = None
            try:
                walker = auto.WalkControl(root, maxDepth=10)
                for ctl, _depth in walker:
                    scanned += 1
                    if scanned > 2200:
                        break

                    # Capture section header Y positions (sidebar only).
                    try:
                        ctn = str(getattr(ctl, "ControlTypeName", "") or "").lower()
                    except Exception:
                        ctn = ""
                    if ctn in {"textcontrol", "hyperlinkcontrol", "headercontrol", "groupcontrol"}:
                        nm0 = ""
                        try:
                            nm0 = str(getattr(ctl, "Name", "") or "").strip()
                        except Exception:
                            nm0 = ""
                        nm0_l = nm0.lower()
                        if nm0_l in {"conversations", "pages"}:
                            try:
                                br0 = getattr(ctl, "BoundingRectangle", None)
                                cx0 = int((br0.left + br0.right) / 2) if br0 else 0
                                y0 = int((br0.top + br0.bottom) / 2) if br0 else 0
                            except Exception:
                                cx0, y0 = 0, 0
                            if cx0 and cx0 < left_cutoff:
                                if nm0_l == "conversations":
                                    conv_y = y0
                                if nm0_l == "pages":
                                    pages_y = y0

                    try:
                        if str(getattr(ctl, "ControlTypeName", "") or "").lower() != "listitemcontrol":
                            continue
                    except Exception:
                        continue
                    nm = ""
                    try:
                        nm = str(getattr(ctl, "Name", "") or "")
                    except Exception:
                        nm = ""
                    if not nm or len(nm.strip()) < 2:
                        continue
                    nm_l = nm.strip().lower()
                    if nm_l in {"new chat", "new conversation"}:
                        continue
                    try:
                        br = getattr(ctl, "BoundingRectangle", None)
                        cx = int((br.left + br.right) / 2) if br else 0
                        cy = int((br.top + br.bottom) / 2) if br else 0
                    except Exception:
                        cx, cy = 0, 0
                    if cx and cx < left_cutoff:
                        items.append((cy, nm, ctl))
            except Exception:
                items = []

            if not items:
                return False

            # If we found a Conversations header, restrict selection to that section.
            picked_from = "sidebar_any"
            pick_pool = list(items)
            try:
                if conv_y is not None:
                    pick_pool = [t for t in items if int(t[0]) > int(conv_y)]
                    picked_from = "sidebar_conversations"
                    # If Pages header is below Conversations, avoid crossing into Pages list.
                    if pages_y is not None and int(pages_y) > int(conv_y):
                        pick_pool = [t for t in pick_pool if int(t[0]) < int(pages_y)]
                if not pick_pool:
                    pick_pool = list(items)
                    picked_from = "sidebar_any_fallback"
            except Exception:
                pick_pool = list(items)
                picked_from = "sidebar_any_fallback"

            pick_pool.sort(key=lambda t: t[0])
            _cy, _nm, target = pick_pool[0]
            try:
                self._log_error_event(
                    "copilot_app_open_recent_pick",
                    name=str(_nm)[:120],
                    sidebar_items=len(items),
                    picked_from=picked_from,
                    conv_header_y=int(conv_y) if conv_y is not None else None,
                    pages_header_y=int(pages_y) if pages_y is not None else None,
                    pool_size=len(pick_pool),
                )
            except Exception:
                pass
            try:
                target.Click()
            except Exception:
                try:
                    target.Invoke()
                except Exception:
                    return False
            time.sleep(max(self.delay, 0.6))
            return True
        except Exception:
            return False

    def attach_file_to_copilot_app(
        self,
        path: str,
        *,
        tab_count: int = 2,
        down_count: int = 1,
        ocr: Any = None,
        save_dir: Optional[Path] = None,
    ) -> bool:
        try:
            p = Path(path)
            if not p.exists():
                self.log(f"Attach file not found: {path}")
                return False
            if p.suffix.lower() not in {".txt", ".md"}:
                self.log(f"Attach file type not supported by app: {p.suffix}")
                try:
                    from .jsonlog import JsonActionLogger  # type: ignore
                    root = Path(__file__).resolve().parent.parent
                    JsonActionLogger(root / "logs" / "errors" / "events.jsonl").log(
                        "copilot_app_attachment_skipped",
                        file=str(p),
                        reason="unsupported_extension",
                    )
                except Exception:
                    pass
                return False

            if self.dry_run:
                self.log(f"DRY-RUN attach file to Copilot app: {str(p)}")
                return True

            # Provide OCR if caller did not.
            if ocr is None:
                try:
                    ocr = getattr(self, "_ocr", None)
                except Exception:
                    ocr = None
            if save_dir is None:
                try:
                    root = Path(__file__).resolve().parent.parent
                    save_dir = root / "logs" / "ocr"
                except Exception:
                    save_dir = None
            if ocr is None:
                # Best-effort create a CopilotOCR instance for image observations.
                try:
                    from .ocr import CopilotOCR  # type: ignore

                    root = Path(__file__).resolve().parent.parent
                    cfg_path = root / "config" / "ocr.json"
                    cfg = {}
                    try:
                        if cfg_path.exists():
                            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
                    except Exception:
                        cfg = {}
                    ocr = CopilotOCR(cfg, log=lambda m: None, debug_dir=(root / "logs" / "ocr"))
                except Exception:
                    ocr = None

            def _observe_step(step: str) -> None:
                try:
                    if not ocr:
                        return
                    res = self.read_copilot_app_text(ocr, save_dir=save_dir) or {}
                    elems = (res.get("elements") or []) if isinstance(res, dict) else []
                    self._log_error_event(
                        "copilot_app_attach_observe",
                        step=str(step),
                        elements_count=len(elems),
                        image_path=str(res.get("image_path") or ""),
                    )
                except Exception:
                    pass

            def _observe_point(step: str, x: int, y: int) -> tuple[str, str]:
                """OCR a small bbox around the cursor/target point.

                This is the key "observe after navigation" signal for mouse-driven targeting.
                """
                try:
                    if not ocr or not hasattr(ocr, "capture_bbox_text"):
                        return "", ""
                    w = int(os.environ.get("COPILOT_ATTACH_POINT_OCR_W", "300"))
                    h = int(os.environ.get("COPILOT_ATTACH_POINT_OCR_H", "160"))
                    half_w = max(20, int(w // 2))
                    half_h = max(20, int(h // 2))
                    bbox = {
                        "left": int(x) - half_w,
                        "top": int(y) - half_h,
                        "width": int(w),
                        "height": int(h),
                    }
                    res = ocr.capture_bbox_text(bbox, save_dir=save_dir, tag=f"attach_point_{step}", preprocess_mode="soft")
                    elems = (res.get("elements") or []) if isinstance(res, dict) else []
                    img_path = (res.get("image_path") or "") if isinstance(res, dict) else ""
                    self._log_error_event(
                        "copilot_app_attach_observe_point",
                        step=str(step),
                        x=int(x),
                        y=int(y),
                        elements_count=len(elems),
                        image_path=str(img_path),
                    )
                    return "", str(img_path or "")
                except Exception:
                    return "", ""

            def _mouse_pos() -> tuple[int, int]:
                try:
                    if pyautogui is None:
                        return 0, 0
                    pt = pyautogui.position()
                    return int(pt.x), int(pt.y)
                except Exception:
                    return 0, 0

            def _probe_control_at_point(x: int, y: int, win_rect: Optional[dict]) -> dict:
                """UIA probe at a screen point to reduce misclicks.

                Returns dict with fields: ok, name, control_type, plausible_attach.
                """
                res = {
                    "ok": False,
                    "name": "",
                    "control_type": "",
                    "plausible_attach": False,
                    "by": "",
                }
                try:
                    import uiautomation as auto  # type: ignore

                    ctl0 = auto.ControlFromPoint(x, y)
                    # Sometimes ControlFromPoint hits an icon/ImageControl inside the real button.
                    # Walk parents to locate a clickable button-like control.
                    chain = []
                    ctl = ctl0
                    for depth in range(5):
                        try:
                            nm = str(getattr(ctl, "Name", "") or "").strip()
                        except Exception:
                            nm = ""
                        try:
                            ct = str(getattr(ctl, "ControlTypeName", "") or "").strip()
                        except Exception:
                            ct = ""
                        chain.append((depth, ct, nm))
                        try:
                            ctl = ctl.GetParentControl()
                        except Exception:
                            break

                    plausible = False
                    best_name = ""
                    best_ct = ""
                    best_depth = None

                    def _in_input_region() -> bool:
                        if not win_rect:
                            return False
                        try:
                            wl = int(win_rect.get("left", 0))
                            wt = int(win_rect.get("top", 0))
                            ww = int(win_rect.get("width", 0))
                            wh = int(win_rect.get("height", 0))
                            # Keep this tight to the input bar region to avoid message-list hits.
                            y_min = wt + int(wh * 0.86)
                            # NOTE: The '+' ("More options") button lives on the right in this Copilot layout.
                            # Allow most of the width, but still exclude the far-left sidebar.
                            x_min = wl + int(ww * 0.03)
                            x_max = wl + int(ww * 0.97)
                            return (int(y) >= y_min) and (x_min <= int(x) <= x_max)
                        except Exception:
                            return False

                    for depth, ct, nm in chain:
                        nm_l = (nm or "").lower()
                        ct_l = (ct or "").lower()
                        if any(k in nm_l for k in ("attach", "upload", "add file", "add files", "choose file", "more options", "add")):
                            plausible = True
                            best_name, best_ct, best_depth = nm, ct, depth
                            res["by"] = "name"
                            break
                        if (not plausible) and _in_input_region() and ct_l in {"buttoncontrol", "splitbuttoncontrol", "menuitemcontrol"}:
                            # Icon-only attach buttons: accept button-like controls in the input region.
                            plausible = True
                            best_name, best_ct, best_depth = nm, ct, depth
                            res["by"] = "geometry" if depth == 0 else "parent_geometry"
                            break

                    if best_depth is None:
                        # Nothing matched; still report the leaf control.
                        try:
                            best_ct = str(getattr(ctl0, "ControlTypeName", "") or "")
                        except Exception:
                            best_ct = ""
                        try:
                            best_name = str(getattr(ctl0, "Name", "") or "")
                        except Exception:
                            best_name = ""

                    res.update({
                        "ok": True,
                        "name": str(best_name)[:140],
                        "control_type": str(best_ct)[:80],
                        "plausible_attach": bool(plausible),
                        "parent_depth": int(best_depth) if best_depth is not None else None,
                    })
                    return res
                except Exception:
                    return res

            def _move_observe_probe_then_maybe_click(x: int, y: int, *, tag: str, win_rect: Optional[dict], learned: bool) -> bool:
                """Move cursor -> observe -> probe -> click (only if plausible).

                For non-learned sequences, we OCR-observe after every move.
                """
                try:
                    cur_x, cur_y = _mouse_pos()
                except Exception:
                    cur_x, cur_y = 0, 0
                dx = int(x) - int(cur_x)
                dy = int(y) - int(cur_y)
                fx = None
                fy = None
                try:
                    if win_rect and int(win_rect.get("width", 0)) > 0 and int(win_rect.get("height", 0)) > 0:
                        fx = (float(int(x) - int(win_rect.get("left", 0))) / float(int(win_rect.get("width", 1))))
                        fy = (float(int(y) - int(win_rect.get("top", 0))) / float(int(win_rect.get("height", 1))))
                except Exception:
                    fx, fy = None, None
                try:
                    self._log_error_event(
                        "copilot_app_attach_nav_move",
                        tag=str(tag),
                        x=int(x),
                        y=int(y),
                        dx=int(dx),
                        dy=int(dy),
                        fx=fx,
                        fy=fy,
                        learned=bool(learned),
                    )
                except Exception:
                    pass
                # Safety: do not navigate/click unless Copilot (or file dialog) is foreground.
                try:
                    if not bool(self._verify_copilot_foreground()):
                        if not bool(self.focus_copilot_app()):
                            self._log_error_event("copilot_app_attach_nav_skip", tag=str(tag), reason="copilot_not_foreground")
                            return False
                except Exception:
                    self._log_error_event("copilot_app_attach_nav_skip", tag=str(tag), reason="copilot_foreground_verify_failed")
                    return False

                move_ok = False
                try:
                    move_ok = bool(self.ctrl.move_mouse(int(x), int(y)))
                except Exception:
                    move_ok = False
                if not move_ok:
                    try:
                        self._log_error_event("copilot_app_attach_nav_skip", tag=str(tag), reason="move_mouse_blocked")
                    except Exception:
                        pass
                    return False
                time.sleep(max(self.delay / 3, 0.12))

                # If we lost foreground after move, do not proceed.
                try:
                    if not bool(self._verify_copilot_foreground()):
                        if not bool(self.focus_copilot_app()):
                            self._log_error_event("copilot_app_attach_nav_skip", tag=str(tag), reason="lost_foreground_after_move")
                            return False
                        time.sleep(max(self.delay / 3, 0.12))
                except Exception:
                    self._log_error_event("copilot_app_attach_nav_skip", tag=str(tag), reason="foreground_verify_after_move_failed")
                    return False

                if not learned:
                    _observe_step(f"{tag}:after_move")

                # Always capture point-local OCR before any click decision.
                # Even in learned mode we need an OCR "before click" observation.
                point_txt, point_img = _observe_point(f"{tag}:before_click", int(x), int(y))

                probe = _probe_control_at_point(int(x), int(y), win_rect)
                try:
                    self._log_error_event(
                        "copilot_app_attach_point_probe",
                        tag=str(tag),
                        x=int(x),
                        y=int(y),
                        ok=bool(probe.get("ok")),
                        control_type=str(probe.get("control_type") or ""),
                        name=str(probe.get("name") or ""),
                        plausible_attach=bool(probe.get("plausible_attach")),
                        by=str(probe.get("by") or ""),
                    )
                except Exception:
                    pass
                # Decide whether it's safe to click.
                plausible_by_uia = bool(probe.get("plausible_attach"))
                point_l = (point_txt or "").lower()
                plausible_by_point_ocr = any(k in point_l for k in ("attach", "upload", "add file", "add files", "choose", "open"))
                # In current Copilot UI, the '+' button is labeled "More options" and is the gateway to upload.
                if (not plausible_by_point_ocr) and ("more options" in point_l or "+" in (point_txt or "")):
                    plausible_by_point_ocr = True
                if not (plausible_by_uia or plausible_by_point_ocr):
                    try:
                        self._log_error_event(
                            "copilot_app_attach_nav_reject",
                            ok=False,
                            tag=str(tag),
                            reason="not_plausible",
                            point_preview=(point_txt or "")[:120],
                            point_image_path=str(point_img or ""),
                            probe_ok=bool(probe.get("ok")),
                            probe_control_type=str(probe.get("control_type") or "")[:80],
                            probe_name=str(probe.get("name") or "")[:140],
                            probe_by=str(probe.get("by") or "")[:40],
                        )
                    except Exception:
                        pass
                    return False
                try:
                    if plausible_by_point_ocr and (not plausible_by_uia):
                        by = "point_ocr"
                        if ("more options" in point_l) or ("+" in (point_txt or "")):
                            by = "point_ocr_plus"
                        self._log_error_event("copilot_app_attach_click_gate", tag=str(tag), by=by)
                except Exception:
                    pass
                try:
                    try:
                        self._log_error_event(
                            "copilot_app_attach_click",
                            ok=True,
                            tag=str(tag),
                            x=int(x),
                            y=int(y),
                            reason=("uia" if plausible_by_uia else "point_ocr"),
                            point_preview=(point_txt or "")[:120],
                            point_image_path=str(point_img or ""),
                            probe_control_type=str(probe.get("control_type") or "")[:80],
                            probe_name=str(probe.get("name") or "")[:140],
                        )
                    except Exception:
                        pass
                    click_ok = bool(self.ctrl.click_at(int(x), int(y)))
                    if not click_ok:
                        try:
                            self._log_error_event(
                                "copilot_app_attach_click",
                                ok=False,
                                tag=str(tag),
                                x=int(x),
                                y=int(y),
                                reason="click_blocked",
                                point_preview=(point_txt or "")[:120],
                                point_image_path=str(point_img or ""),
                                probe_control_type=str(probe.get("control_type") or "")[:80],
                                probe_name=str(probe.get("name") or "")[:140],
                            )
                        except Exception:
                            pass
                        return False
                    time.sleep(max(self.delay / 2, 0.18))
                    if not learned:
                        _observe_step(f"{tag}:after_click")
                    return True
                except Exception:
                    try:
                        self._log_error_event(
                            "copilot_app_attach_click",
                            ok=False,
                            tag=str(tag),
                            x=int(x),
                            y=int(y),
                            reason="click_exception",
                        )
                    except Exception:
                        pass
                    return False

            def _move_observe_probe_then_click_any(x: int, y: int, *, tag: str, win_rect: Optional[dict], learned: bool) -> bool:
                """Move cursor -> observe -> probe -> click (unconditionally).

                Used for known non-attach UI like the 'More options' button.
                Still enforces the user's "observe after move" discipline and foreground gating.
                """
                try:
                    cur_x, cur_y = _mouse_pos()
                except Exception:
                    cur_x, cur_y = 0, 0
                dx = int(x) - int(cur_x)
                dy = int(y) - int(cur_y)
                fx = None
                fy = None
                try:
                    if win_rect and int(win_rect.get("width", 0)) > 0 and int(win_rect.get("height", 0)) > 0:
                        fx = (float(int(x) - int(win_rect.get("left", 0))) / float(int(win_rect.get("width", 1))))
                        fy = (float(int(y) - int(win_rect.get("top", 0))) / float(int(win_rect.get("height", 1))))
                except Exception:
                    fx, fy = None, None
                try:
                    self._log_error_event(
                        "copilot_app_attach_nav_move",
                        tag=str(tag),
                        x=int(x),
                        y=int(y),
                        dx=int(dx),
                        dy=int(dy),
                        fx=fx,
                        fy=fy,
                        learned=bool(learned),
                    )
                except Exception:
                    pass

                try:
                    if not bool(self._verify_copilot_foreground()):
                        if not bool(self.focus_copilot_app()):
                            self._log_error_event("copilot_app_attach_nav_skip", tag=str(tag), reason="copilot_not_foreground")
                            return False
                except Exception:
                    self._log_error_event("copilot_app_attach_nav_skip", tag=str(tag), reason="copilot_foreground_verify_failed")
                    return False

                move_ok = False
                try:
                    move_ok = bool(self.ctrl.move_mouse(int(x), int(y)))
                except Exception:
                    move_ok = False
                if not move_ok:
                    try:
                        self._log_error_event("copilot_app_attach_nav_skip", tag=str(tag), reason="move_mouse_blocked")
                    except Exception:
                        pass
                    return False
                time.sleep(max(self.delay / 3, 0.12))

                try:
                    if not bool(self._verify_copilot_foreground()):
                        if not bool(self.focus_copilot_app()):
                            self._log_error_event("copilot_app_attach_nav_skip", tag=str(tag), reason="lost_foreground_after_move")
                            return False
                        time.sleep(max(self.delay / 3, 0.12))
                except Exception:
                    self._log_error_event("copilot_app_attach_nav_skip", tag=str(tag), reason="foreground_verify_after_move_failed")
                    return False

                if not learned:
                    _observe_step(f"{tag}:after_move")
                # Always capture point-local OCR before the unconditional click.
                point_txt, point_img = _observe_point(f"{tag}:before_click", int(x), int(y))

                probe = _probe_control_at_point(int(x), int(y), win_rect)
                try:
                    self._log_error_event(
                        "copilot_app_attach_point_probe",
                        tag=str(tag),
                        x=int(x),
                        y=int(y),
                        ok=bool(probe.get("ok")),
                        control_type=str(probe.get("control_type") or ""),
                        name=str(probe.get("name") or ""),
                        plausible_attach=bool(probe.get("plausible_attach")),
                        by=str(probe.get("by") or ""),
                    )
                except Exception:
                    pass

                # If we are about to click unconditionally but UIA says it's not even plausibly attach-like,
                # record it explicitly so tuning can improve and we don't silently repeat mis-aim.
                try:
                    if not bool(probe.get("plausible_attach")):
                        self._log_error_event(
                            "copilot_app_attach_nav_reject",
                            ok=False,
                            tag=str(tag),
                            reason="unconditional_suspect",
                            point_preview=(point_txt or "")[:120],
                            point_image_path=str(point_img or ""),
                            probe_ok=bool(probe.get("ok")),
                            probe_control_type=str(probe.get("control_type") or "")[:80],
                            probe_name=str(probe.get("name") or "")[:140],
                            probe_by=str(probe.get("by") or "")[:40],
                        )
                except Exception:
                    pass

                try:
                    try:
                        self._log_error_event(
                            "copilot_app_attach_click",
                            ok=True,
                            tag=str(tag),
                            x=int(x),
                            y=int(y),
                            reason="unconditional",
                            point_preview=(point_txt or "")[:120],
                            point_image_path=str(point_img or ""),
                            probe_control_type=str(probe.get("control_type") or "")[:80],
                            probe_name=str(probe.get("name") or "")[:140],
                            probe_by=str(probe.get("by") or "")[:40],
                        )
                    except Exception:
                        pass
                    click_ok = bool(self.ctrl.click_at(int(x), int(y)))
                    if not click_ok:
                        try:
                            self._log_error_event(
                                "copilot_app_attach_click",
                                ok=False,
                                tag=str(tag),
                                x=int(x),
                                y=int(y),
                                reason="click_blocked",
                                point_preview=(point_txt or "")[:120],
                                point_image_path=str(point_img or ""),
                                probe_control_type=str(probe.get("control_type") or "")[:80],
                                probe_name=str(probe.get("name") or "")[:140],
                                probe_by=str(probe.get("by") or "")[:40],
                            )
                        except Exception:
                            pass
                        return False
                    time.sleep(max(self.delay / 2, 0.18))
                    if not learned:
                        _observe_step(f"{tag}:after_click")
                    return True
                except Exception:
                    try:
                        self._log_error_event(
                            "copilot_app_attach_click",
                            ok=False,
                            tag=str(tag),
                            x=int(x),
                            y=int(y),
                            reason="click_exception",
                        )
                    except Exception:
                        pass
                    return False

            def _press(keys: List[str], step: str) -> bool:
                _observe_step(step + ":before")
                ok = False
                try:
                    ok = bool(self._press_keys_copilot(keys))
                except Exception:
                    ok = False
                self._log_error_event("copilot_app_attach_key", step=str(step), keys=keys, ok=bool(ok))
                time.sleep(max(self.delay / 2, 0.18))
                _observe_step(step + ":after")
                return ok

            def _type(text: str, step: str) -> bool:
                _observe_step(step + ":before")
                ok = False
                try:
                    ok = bool(self.ctrl.type_text(text))
                except Exception:
                    ok = False
                self._log_error_event("copilot_app_attach_type", step=str(step), chars=len(text or ""), ok=bool(ok))
                time.sleep(max(self.delay / 3, 0.12))
                _observe_step(step + ":after")
                return ok

            # Focus Copilot first.
            if not self.focus_copilot_app():
                self._log_error_event("copilot_app_attachment_failed", file=str(p), reason="copilot_not_foreground")
                return False

            # Keyboard gating: allow Copilot foreground OR the Windows file-open dialog.
            prev_gate = None

            def _is_file_dialog_foreground() -> bool:
                try:
                    if not self.winman:
                        return False
                    fg = self.winman.get_foreground()
                    if not fg:
                        return False
                    info = self.winman.get_window_info(fg) or {}
                    title = (info.get("title") or "").lower()
                    cls = (info.get("class") or "").lower()
                    proc = (info.get("process") or "").lower()
                    if proc == "code.exe" or proc.startswith("code"):
                        return False
                    # Common file dialog class is #32770; process often explorer.exe.
                    if "#32770" in cls:
                        return True
                    # Copilot can present an explorer.exe dialog behind a Copilot focus frame window
                    # (title/class), which may not have 'Open' in the title.
                    if proc == "explorer.exe" and (
                        ("open" in title or "choose" in title or "select" in title)
                        or ("copilotkeypressfocusframe" in cls)
                        or ("copilotkeyfocuswindow" in cls)
                        or ("copilotkeyfocuswindow" in title)
                    ):
                        return True
                    return False
                except Exception:
                    return False

            def _detect_file_picker_controls() -> dict:
                """Detect common file picker UIA controls (File name / Open)."""
                res = {"has_filename": False, "has_open": False, "fn_xy": None, "open_xy": None}
                try:
                    import uiautomation as auto  # type: ignore

                    fc = auto.GetFocusedControl()
                    top = None
                    try:
                        top = fc.GetTopLevelControl() if fc else None
                    except Exception:
                        top = None
                    if top is None:
                        top = auto.GetTopLevelControl()
                    if top is None:
                        return res

                    scanned = 0
                    for ctl, _depth in auto.WalkControl(top, maxDepth=8):
                        scanned += 1
                        if scanned > 2000:
                            break
                        try:
                            ctn = str(getattr(ctl, "ControlTypeName", "") or "").lower()
                        except Exception:
                            continue
                        nm = ""
                        try:
                            nm = str(getattr(ctl, "Name", "") or "").strip()
                        except Exception:
                            nm = ""
                        nm_l = nm.lower()
                        try:
                            br = getattr(ctl, "BoundingRectangle", None)
                            cx = int((br.left + br.right) / 2) if br else 0
                            cy = int((br.top + br.bottom) / 2) if br else 0
                        except Exception:
                            cx, cy = 0, 0

                        if (not res["has_filename"]) and ctn == "editcontrol" and nm_l:
                            if ("file name" in nm_l) or ("filename" in nm_l):
                                res["has_filename"] = True
                                if cx and cy:
                                    res["fn_xy"] = (cx, cy)
                        if (not res["has_open"]) and ctn in {"buttoncontrol", "splitbuttoncontrol"} and nm_l:
                            if nm_l in {"open", "select"} or nm_l.startswith("open"):
                                res["has_open"] = True
                                if cx and cy:
                                    res["open_xy"] = (cx, cy)
                        if res["has_filename"] and res["has_open"]:
                            break
                except Exception:
                    return res
                return res

            def _wait_file_picker(timeout_s: float = 6.0) -> dict:
                t0 = time.time()
                last = {}
                while (time.time() - t0) < float(timeout_s):
                    if _is_file_dialog_foreground():
                        # Even with a classic dialog, UIA controls are often detectable.
                        last = _detect_file_picker_controls()
                        last["dialog_foreground"] = True
                        try:
                            if self.winman:
                                fg = self.winman.get_foreground()
                                info = self.winman.get_window_info(fg) if fg else {}
                                last["fg_title"] = (info.get("title") or "")
                                last["fg_class"] = (info.get("class") or "")
                                last["fg_process"] = (info.get("process") or "")
                        except Exception:
                            pass
                        return last
                    last = _detect_file_picker_controls()
                    if bool(last.get("has_filename")):
                        last["dialog_foreground"] = False
                        try:
                            if self.winman:
                                fg = self.winman.get_foreground()
                                info = self.winman.get_window_info(fg) if fg else {}
                                last["fg_title"] = (info.get("title") or "")
                                last["fg_class"] = (info.get("class") or "")
                                last["fg_process"] = (info.get("process") or "")
                        except Exception:
                            pass
                        return last
                    time.sleep(0.18)
                last = _detect_file_picker_controls()
                last["timeout"] = True
                try:
                    if self.winman:
                        fg = self.winman.get_foreground()
                        info = self.winman.get_window_info(fg) if fg else {}
                        last["fg_title"] = (info.get("title") or "")
                        last["fg_class"] = (info.get("class") or "")
                        last["fg_process"] = (info.get("process") or "")
                except Exception:
                    pass
                return last

            def _wait_file_dialog(timeout_s: float = 6.0) -> bool:
                # Backward-compatible helper used in a couple spots.
                st = _wait_file_picker(timeout_s)
                return bool(st.get("has_filename") or st.get("dialog_foreground"))

            def _open_more_options_menu_then_upload(win_rect: Optional[dict]) -> bool:
                """Try: click input-bar 'More options' -> choose 'Upload/Add file' -> wait file picker.

                If the caller has already clicked '+' / More options and the flyout is expected to be open,
                pass already_open=True and anchor_xy to skip re-clicking and just pick Upload.
                """
                return _open_more_options_menu_then_upload_impl(win_rect, already_open=False, anchor_xy=None)

            def _open_more_options_menu_then_upload_impl(
                win_rect: Optional[dict], *, already_open: bool, anchor_xy: Optional[tuple[int, int]]
            ) -> bool:
                if self.dry_run:
                    return True
                if not self.winman:
                    return False
                try:
                    if not bool(self._verify_copilot_foreground()):
                        if not bool(self.focus_copilot_app()):
                            return False
                except Exception:
                    return False

                try:
                    import uiautomation as auto  # type: ignore

                    hwnd = self.winman.get_foreground()
                    root = auto.ControlFromHandle(int(hwnd)) if hwnd else auto.GetRootControl()

                    bx, by = 0, 0
                    if already_open and anchor_xy and len(anchor_xy) == 2:
                        try:
                            bx, by = int(anchor_xy[0]), int(anchor_xy[1])
                            self._log_error_event(
                                "copilot_app_more_options_found",
                                ok=True,
                                x=int(bx),
                                y=int(by),
                                name="(anchor_only)",
                                note="already_open",
                            )
                        except Exception:
                            bx, by = 0, 0
                    if not (bx and by):
                        target_btn = None
                        try:
                            fc = auto.GetFocusedControl()
                            fc_name = str(getattr(fc, "Name", "") or "").strip().lower()
                            fc_type = str(getattr(fc, "ControlTypeName", "") or "").strip().lower()
                            if fc and ("more options" in fc_name) and ("button" in fc_type):
                                target_btn = fc
                        except Exception:
                            target_btn = None

                        if target_btn is None:
                            candidates = []
                            scanned = 0
                            for ctl, _depth in auto.WalkControl(root, maxDepth=10):
                                scanned += 1
                                if scanned > 2600:
                                    break
                                try:
                                    ct = str(getattr(ctl, "ControlTypeName", "") or "").strip().lower()
                                    nm = str(getattr(ctl, "Name", "") or "").strip()
                                except Exception:
                                    continue
                                if ct not in {"buttoncontrol", "splitbuttoncontrol"}:
                                    continue
                                nm_l = nm.lower()
                                if "more options" not in nm_l:
                                    continue
                                try:
                                    br = getattr(ctl, "BoundingRectangle", None)
                                    cx = int((br.left + br.right) / 2) if br else 0
                                    cy = int((br.top + br.bottom) / 2) if br else 0
                                except Exception:
                                    cx, cy = 0, 0
                                if not cx or not cy:
                                    continue
                                if win_rect:
                                    try:
                                        wt = int(win_rect.get("top", 0))
                                        wh = int(win_rect.get("height", 0))
                                        y_min = wt + int(wh * 0.80)
                                        if cy < y_min:
                                            continue
                                    except Exception:
                                        pass
                                # Prefer the rightmost/bottom-most 'More options' in the input area.
                                score = int(cy / 10) + int(cx / 50)
                                candidates.append((score, ctl, cx, cy, nm))
                            if not candidates:
                                self._log_error_event("copilot_app_more_options_found", ok=False, reason="not_found")
                                return False
                            candidates.sort(key=lambda t: t[0], reverse=True)
                            _score, target_btn, cx, cy, nm = candidates[0]
                            self._log_error_event("copilot_app_more_options_found", ok=True, x=int(cx), y=int(cy), name=str(nm)[:120])

                        try:
                            br = getattr(target_btn, "BoundingRectangle", None)
                            bx = int((br.left + br.right) / 2) if br else 0
                            by = int((br.top + br.bottom) / 2) if br else 0
                        except Exception:
                            bx, by = 0, 0
                        if not bx or not by:
                            return False

                        if not already_open:
                            # Click 'More options' WITHOUT attach-only gating.
                            try:
                                did = _move_observe_probe_then_click_any(int(bx), int(by), tag="more_options", win_rect=win_rect, learned=False)
                                if not did:
                                    try:
                                        target_btn.Click()
                                    except Exception:
                                        target_btn.Invoke()
                            except Exception:
                                try:
                                    target_btn.Invoke()
                                except Exception:
                                    return False

                            time.sleep(max(self.delay, 0.35))
                            _observe_step("more_options:after_open")
                            try:
                                _observe_point("more_options:menu_hint", int(bx), int(by) + 140)
                            except Exception:
                                pass
                        else:
                            # Caller already clicked '+', so just observe where we expect the flyout.
                            _observe_step("more_options:assumed_open")
                            try:
                                _observe_point("more_options:menu_hint", int(bx), int(by) + 140)
                            except Exception:
                                pass

                    # Locate the flyout/menu subtree by probing around the More options button.
                    # WinUI flyouts often open upward, so we probe multiple directions.
                    menu_roots = []
                    upload_xy: Optional[tuple[int, int]] = None
                    try:
                        probe_offsets = [
                            (0, 60),
                            (0, 100),
                            (0, 140),
                            (0, -60),
                            (0, -120),
                            (-140, -60),
                            (-200, -120),
                            (-240, 60),
                            (-240, -60),
                        ]
                        for dx, dy in probe_offsets:
                            px = int(bx) + int(dx)
                            py = int(by) + int(dy)
                            if px <= 0 or py <= 0:
                                continue
                            if win_rect:
                                try:
                                    wl = int(win_rect.get("left", 0))
                                    wt = int(win_rect.get("top", 0))
                                    ww = int(win_rect.get("width", 0))
                                    wh = int(win_rect.get("height", 0))
                                    if not (wl <= px <= (wl + ww) and wt <= py <= (wt + wh)):
                                        continue
                                except Exception:
                                    pass
                            ctl = auto.ControlFromPoint(int(px), int(py))
                            if not ctl:
                                continue
                            try:
                                leaf_ct = str(getattr(ctl, "ControlTypeName", "") or "")
                                leaf_name = str(getattr(ctl, "Name", "") or "")
                                self._log_error_event(
                                    "copilot_app_more_options_menu_probe",
                                    dx=int(dx),
                                    dy=int(dy),
                                    x=int(px),
                                    y=int(py),
                                    leaf_ct=leaf_ct[:60],
                                    leaf_name=leaf_name[:140],
                                )
                                # If UIA exposes an explicit 'Upload'/attach-like menu item near the probe,
                                # remember its center so we can click it directly before heavier heuristics.
                                try:
                                    nm_l_probe = leaf_name.lower()
                                    if any(k in nm_l_probe for k in ("upload", "add file", "add files", "attach")):
                                        br_probe = getattr(ctl, "BoundingRectangle", None)
                                        if br_probe:
                                            mx = int((br_probe.left + br_probe.right) / 2)
                                            my = int((br_probe.top + br_probe.bottom) / 2)
                                            upload_xy = (mx, my)
                                except Exception:
                                    pass
                            except Exception:
                                pass
                            c = ctl
                            for _ in range(7):
                                try:
                                    ct = str(getattr(c, "ControlTypeName", "") or "").strip().lower()
                                except Exception:
                                    ct = ""
                                if ct in {"menucontrol", "panecontrol", "windowcontrol", "listcontrol", "popupcontrol"}:
                                    menu_roots.append(c)
                                    break
                                try:
                                    c = c.GetParentControl()
                                except Exception:
                                    break
                    except Exception:
                        menu_roots = []

                    # Deduplicate roots (by RuntimeId when available).
                    uniq_roots = []
                    seen = set()
                    for r in menu_roots:
                        try:
                            rid = tuple(getattr(r, "GetRuntimeId", lambda: [])() or [])
                        except Exception:
                            rid = ()
                        key = rid if rid else id(r)
                        if key in seen:
                            continue
                        seen.add(key)
                        uniq_roots.append(r)

                    if not uniq_roots:
                        self._log_error_event("copilot_app_more_options_menu_pick", ok=False, reason="menu_root_not_found")
                        return False

                    def _click_conversation_starter_upload(win_rect_cs: Optional[dict]) -> bool:
                        """If the foreground surface is a 'Conversation Starter Options' window,

                        attempt to click an Upload/Attach entry inside it and wait for a real
                        file picker. This handles Copilot builds where More options opens an
                        intermediate Copilot surface instead of a classic file dialog.
                        """
                        if self.dry_run:
                            return False
                        if not self.winman:
                            return False
                        try:
                            fg = self.winman.get_foreground()
                            info = self.winman.get_window_info(fg) if fg else {}
                        except Exception:
                            info = {}
                        title_cs = str((info.get("title") or "")).lower()
                        proc_cs = str((info.get("process") or "")).lower()
                        if "conversation starter" not in title_cs:
                            return False
                        if "copilot" not in proc_cs:
                            return False
                        try:
                            self._log_error_event(
                                "copilot_app_conversation_starter_detected",
                                ok=True,
                                title=str(info.get("title") or "")[:160],
                                process=str(info.get("process") or "")[:80],
                            )
                        except Exception:
                            pass
                        try:
                            import uiautomation as auto  # type: ignore

                            hwnd_cs = self.winman.get_foreground()
                            top = auto.ControlFromHandle(int(hwnd_cs)) if hwnd_cs else auto.GetTopLevelControl()
                            if not top:
                                return False

                            # Limit search to the conversation-starter window bounds when known.
                            wl = int(win_rect_cs.get("left", 0)) if win_rect_cs else 0
                            wt = int(win_rect_cs.get("top", 0)) if win_rect_cs else 0
                            ww = int(win_rect_cs.get("width", 0)) if win_rect_cs else 0
                            wh = int(win_rect_cs.get("height", 0)) if win_rect_cs else 0
                            win_area = int(ww) * int(wh) if ww and wh else 0

                            best = None
                            best_score = 0
                            scanned = 0
                            for ctl, _depth in auto.WalkControl(top, maxDepth=9):
                                scanned += 1
                                if scanned > 2600:
                                    break
                                try:
                                    ct = str(getattr(ctl, "ControlTypeName", "") or "").strip().lower()
                                    nm = str(getattr(ctl, "Name", "") or "").strip()
                                except Exception:
                                    continue
                                if ct not in {"buttoncontrol", "splitbuttoncontrol", "menuitemcontrol", "listitemcontrol"}:
                                    continue
                                nm_l = nm.lower()
                                if not nm_l:
                                    continue
                                if not any(k in nm_l for k in ("upload", "add file", "add files", "attach", "choose file", "choose files")):
                                    continue
                                try:
                                    br = getattr(ctl, "BoundingRectangle", None)
                                    if not br:
                                        continue
                                    cx = int((br.left + br.right) / 2)
                                    cy = int((br.top + br.bottom) / 2)
                                    w = int(br.right - br.left)
                                    h = int(br.bottom - br.top)
                                except Exception:
                                    continue
                                if win_area:
                                    if cx < wl or cx > (wl + ww) or cy < wt or cy > (wt + wh):
                                        continue
                                # Avoid huge panes and tiny glyphs.
                                if w <= 8 or h <= 8 or w > 640 or h > 280:
                                    continue
                                score = 0
                                if "upload" in nm_l:
                                    score += 500
                                if "add file" in nm_l or "add files" in nm_l:
                                    score += 420
                                if "file" in nm_l or "files" in nm_l:
                                    score += 260
                                if "attach" in nm_l:
                                    score += 200
                                if "choose" in nm_l or "browse" in nm_l or "select" in nm_l:
                                    score += 120
                                score += int(cy / 30)
                                if score > best_score:
                                    best_score = int(score)
                                    best = (int(cx), int(cy), nm, ct)

                            if not best or best_score < 240:
                                try:
                                    self._log_error_event(
                                        "copilot_app_conversation_starter_pick",
                                        ok=False,
                                        reason="no_upload_candidate",
                                        scanned=int(scanned),
                                    )
                                except Exception:
                                    pass
                                return False

                            mx, my, nm_best, ct_best = best
                            try:
                                self._log_error_event(
                                    "copilot_app_conversation_starter_pick",
                                    ok=True,
                                    x=int(mx),
                                    y=int(my),
                                    name=str(nm_best)[:120],
                                    control_type=str(ct_best)[:60],
                                    score=int(best_score),
                                )
                            except Exception:
                                pass

                            did = _move_observe_probe_then_click_any(
                                int(mx),
                                int(my),
                                tag="conversation_starter_upload",
                                win_rect=win_rect_cs,
                                learned=False,
                            )
                            if not did:
                                return False
                            st_cs = _wait_file_picker(6.0)
                            if bool(st_cs.get("has_filename") or st_cs.get("dialog_foreground")):
                                self._log_error_event("copilot_app_attach_opened", method="conversation_starter_upload", **st_cs)
                                return True
                            try:
                                self._log_error_event(
                                    "copilot_app_attachment_failed",
                                    file=str(p),
                                    reason="conversation_starter_no_file_picker",
                                    **st_cs,
                                )
                            except Exception:
                                pass
                            return False
                        except Exception:
                            return False

                    # Fast-path: if we already saw an explicit 'Upload' / attach-like item via UIA
                    # during the probe phase, prefer clicking it directly instead of relying on
                    # secondary heuristics that might select unrelated menu entries.
                    if upload_xy and win_rect:
                        try:
                            mx, my = int(upload_xy[0]), int(upload_xy[1])
                            did_click = bool(
                                _move_observe_probe_then_maybe_click(
                                    mx,
                                    my,
                                    tag="more_options_upload_direct",
                                    win_rect=win_rect,
                                    learned=False,
                                )
                            )
                        except Exception:
                            did_click = False
                        if did_click:
                            st = _wait_file_picker(6.0)
                            if bool(st.get("has_filename") or st.get("dialog_foreground")):
                                self._log_error_event("copilot_app_attach_opened", method="more_options_menu_direct", **st)
                                return True

                    def _ocr_scan_more_options_labels(anchor_x: int, anchor_y: int) -> list[str]:
                        """OCR-scan likely flyout regions near the More options anchor.

                        Copilot flyout UIs are often icon+text; even when UIA names are empty,
                        OCR often reveals labels like 'Upload' / 'Add files'.
                        """
                        try:
                            if not ocr or not hasattr(ocr, "capture_bbox_text") or not save_dir:
                                return []
                            # Probe regions around the anchor; flyout commonly opens upward.
                            probes = [
                                (0, -220, 420, 260),
                                (0, -320, 520, 320),
                                (-260, -240, 520, 300),
                                (-340, -200, 640, 300),
                                (0, 120, 520, 220),
                            ]
                            labels: list[str] = []
                            images: list[str] = []
                            for i, (dx, dy, w, h) in enumerate(probes):
                                cx = int(anchor_x) + int(dx)
                                cy = int(anchor_y) + int(dy)
                                bbox = {
                                    "left": int(cx - int(w // 2)),
                                    "top": int(cy - int(h // 2)),
                                    "width": int(w),
                                    "height": int(h),
                                }
                                try:
                                    res = ocr.capture_bbox_text(
                                        bbox,
                                        save_dir=save_dir,
                                        tag=f"more_options_menu_ocr_{'open' if (not already_open) else 'assumed'}_{i}",
                                        preprocess_mode="soft",
                                    )
                                except Exception:
                                    res = None
                                elems_menu = (res.get("elements") or []) if isinstance(res, dict) else []
                                img = (res.get("image_path") or "") if isinstance(res, dict) else ""
                                if img:
                                    images.append(str(img))
                                # We don't have text labels without OCR; expose element bbox info for downstream inspection.
                                for e in elems_menu:
                                    b = e.get("bbox") or {}
                                    lbl = f"bbox:{b.get('left',0)},{b.get('top',0)},{b.get('width',0)},{b.get('height',0)}"
                                    if lbl not in labels:
                                        labels.append(lbl)
                            try:
                                self._log_error_event(
                                    "copilot_app_more_options_menu_ocr",
                                    ok=True,
                                    already_open=bool(already_open),
                                    anchor_x=int(anchor_x),
                                    anchor_y=int(anchor_y),
                                    label_count=int(len(labels)),
                                    labels=labels[:20],
                                    image_paths=images[:10],
                                )
                            except Exception:
                                pass
                            return labels
                        except Exception:
                            try:
                                self._log_error_event(
                                    "copilot_app_more_options_menu_ocr",
                                    ok=False,
                                    already_open=bool(already_open),
                                    anchor_x=int(anchor_x),
                                    anchor_y=int(anchor_y),
                                    reason="exception",
                                )
                            except Exception:
                                pass
                            return []

                    # Read flyout labels via OCR to learn the actual option text shown.
                    observed_labels: list[str] = []
                    try:
                        if bx and by:
                            observed_labels = _ocr_scan_more_options_labels(int(bx), int(by))
                    except Exception:
                        observed_labels = []

                    # Base keywords (stable across builds).
                    keywords = [
                        "upload",
                        "upload file",
                        "add file",
                        "add files",
                        "attach",
                        "choose file",
                        "choose files",
                        "browse",
                        "select file",
                        "select files",
                    ]
                    # If OCR revealed any likely file-related entries, add them as extra matching hints.
                    # This helps when UIA names are slightly different (e.g., 'Upload files').
                    try:
                        for lbl in (observed_labels or [])[:12]:
                            l = (lbl or "").strip().lower()
                            if not l:
                                continue
                            if any(k in l for k in ("upload", "file", "attach", "browse", "select")):
                                if l not in keywords:
                                    keywords.append(l)
                    except Exception:
                        pass
                    menu_candidates = []
                    for search_root in uniq_roots:
                        scanned = 0
                        for ctl, _depth in auto.WalkControl(search_root, maxDepth=10):
                            scanned += 1
                            if scanned > 2200:
                                break
                            try:
                                ct = str(getattr(ctl, "ControlTypeName", "") or "").strip().lower()
                                nm = str(getattr(ctl, "Name", "") or "").strip()
                            except Exception:
                                continue
                            if ct not in {"menuitemcontrol", "buttoncontrol", "listitemcontrol", "textcontrol"}:
                                continue
                            nm_l = nm.lower()
                            if not nm_l:
                                continue
                            if not any(k in nm_l for k in keywords):
                                continue
                            try:
                                br = getattr(ctl, "BoundingRectangle", None)
                                mx = int((br.left + br.right) / 2) if br else 0
                                my = int((br.top + br.bottom) / 2) if br else 0
                            except Exception:
                                mx, my = 0, 0
                            if not mx or not my:
                                continue
                            score = 0
                            if "upload" in nm_l:
                                score += 300
                            if "add file" in nm_l or "add files" in nm_l:
                                score += 250
                            if "attach" in nm_l:
                                score += 180
                            score += int(my / 30)
                            menu_candidates.append((score, ctl, mx, my, nm))

                    if not menu_candidates:
                        # Many Copilot builds expose flyout items as icon-only controls with empty names.
                        # Fallback (NO BLIND-PICKS): OCR-evaluate candidate items within the smallest flyout root,
                        # then click only if the OCR indicates an Upload/Add file action.
                        try:
                            wl = int(win_rect.get("left", 0)) if win_rect else 0
                            wt = int(win_rect.get("top", 0)) if win_rect else 0
                            ww = int(win_rect.get("width", 0)) if win_rect else 0
                            wh = int(win_rect.get("height", 0)) if win_rect else 0
                            win_area = int(ww) * int(wh) if ww and wh else 0
                        except Exception:
                            wl = wt = ww = wh = 0
                            win_area = 0

                        flyouts = []
                        for r in uniq_roots:
                            try:
                                br = getattr(r, "BoundingRectangle", None)
                                if not br:
                                    continue
                                rw = int(br.right - br.left)
                                rh = int(br.bottom - br.top)
                                if rw <= 0 or rh <= 0:
                                    continue
                                area = int(rw) * int(rh)
                                # Exclude roots that are basically the whole app window.
                                if win_area and area > int(win_area * 0.60):
                                    continue
                                # Must lie within Copilot window.
                                if ww and wh:
                                    if br.left < wl or br.right > (wl + ww) or br.top < wt or br.bottom > (wt + wh):
                                        continue
                                flyouts.append((area, r, int(br.left), int(br.top), int(br.right), int(br.bottom)))
                            except Exception:
                                continue

                        flyouts.sort(key=lambda t: t[0])
                        picked = None
                        if flyouts:
                            _area, root0, l0, t0, r0, b0 = flyouts[0]
                            # Scan for button-like children and OCR-evaluate each candidate.
                            cands = []
                            scanned = 0
                            for ctl, _depth in auto.WalkControl(root0, maxDepth=8):
                                scanned += 1
                                if scanned > 1800:
                                    break
                                try:
                                    ct = str(getattr(ctl, "ControlTypeName", "") or "").strip().lower()
                                except Exception:
                                    continue
                                if ct not in {"buttoncontrol", "menuitemcontrol", "listitemcontrol"}:
                                    continue
                                try:
                                    br = getattr(ctl, "BoundingRectangle", None)
                                    if not br:
                                        continue
                                    cx = int((br.left + br.right) / 2)
                                    cy = int((br.top + br.bottom) / 2)
                                    w = int(br.right - br.left)
                                    h = int(br.bottom - br.top)
                                except Exception:
                                    continue
                                if not (l0 <= cx <= r0 and t0 <= cy <= b0):
                                    continue
                                # Size sanity: avoid huge panes.
                                if w > 520 or h > 220 or w < 14 or h < 14:
                                    continue
                                try:
                                    nm = str(getattr(ctl, "Name", "") or "").strip()
                                except Exception:
                                    nm = ""
                                # Keep candidates ordered top-to-bottom for evaluation.
                                cands.append((cy, cx, nm, ct, br))

                            if cands:
                                cands.sort(key=lambda t: (t[0], t[1]))  # cy asc, cx asc
                                best = None
                                best_score = 0
                                eval_count = 0
                                for idx, (cy, cx, nm, ct, br) in enumerate(cands[:10]):
                                    eval_count += 1
                                    ocr_txt = ""
                                    img_path = ""
                                    score = 0
                                    try:
                                        # Expand bbox slightly so OCR can catch label text next to icon.
                                        pad = 18
                                        bbox = {
                                            "left": int(br.left) - pad,
                                            "top": int(br.top) - pad,
                                            "width": int((br.right - br.left) + pad * 2),
                                            "height": int((br.bottom - br.top) + pad * 2),
                                        }
                                        if ocr and hasattr(ocr, "capture_bbox_text"):
                                            res = ocr.capture_bbox_text(
                                                bbox,
                                                save_dir=save_dir,
                                                tag=f"more_options_item_{idx}",
                                                preprocess_mode="soft",
                                            )
                                            if isinstance(res, dict):
                                                # Use a small amount of localized OCR text to recognize
                                                # file-related actions (e.g. "Upload" / "Add file").
                                                ocr_txt = str(
                                                    res.get("text")
                                                    or res.get("full_text")
                                                    or ""
                                                )
                                                img_path = str(res.get("image_path") or "")
                                    except Exception:
                                        ocr_txt = ""
                                        img_path = ""
                                    txt_l = (ocr_txt or "").lower()
                                    # Strong signals.
                                    if "upload" in txt_l:
                                        score += 500
                                    if "add file" in txt_l or "add files" in txt_l:
                                        score += 420
                                    if "file" in txt_l or "files" in txt_l:
                                        score += 240
                                    if "attach" in txt_l:
                                        score += 180
                                    if "browse" in txt_l or "select" in txt_l:
                                        score += 120
                                    # Weak tie-breakers.
                                    if nm:
                                        nm_l = (nm or "").lower()
                                        if "upload" in nm_l:
                                            score += 120
                                        if "file" in nm_l:
                                            score += 60

                                    try:
                                        self._log_error_event(
                                            "copilot_app_more_options_menu_item_eval",
                                            ok=True,
                                            idx=int(idx),
                                            x=int(cx),
                                            y=int(cy),
                                            control_type=str(ct)[:60],
                                            uia_name=str(nm)[:120],
                                            score=int(score),
                                            ocr_preview=(ocr_txt or "")[:160],
                                            image_path=str(img_path or ""),
                                        )
                                    except Exception:
                                        pass

                                    if score > best_score:
                                        best_score = int(score)
                                        best = (int(cx), int(cy), nm, ct)

                                # Only click if OCR indicates we found an upload/file action.
                                # No guess: if we can't identify it, bail out and let the keyboard inference run.
                                if best and int(best_score) >= 240:
                                    mx, my, nm, ct = best
                                    picked = (mx, my, nm, ct)
                                    try:
                                        self._log_error_event(
                                            "copilot_app_more_options_menu_pick",
                                            ok=True,
                                            reason="icon_only_ocr_pick",
                                            roots=len(uniq_roots),
                                            name=str(nm)[:120],
                                            x=int(mx),
                                            y=int(my),
                                            control_type=str(ct)[:60],
                                            score=int(best_score),
                                            evaluated=int(eval_count),
                                        )
                                    except Exception:
                                        pass

                        if picked is not None:
                            mx, my, nm, ct = picked
                            _move_observe_probe_then_click_any(int(mx), int(my), tag="more_options_icon_item", win_rect=win_rect, learned=False)
                            st = _wait_file_picker(6.0)
                            if bool(st.get("has_filename") or st.get("dialog_foreground")):
                                self._log_error_event("copilot_app_attach_opened", method="more_options_menu_icon_fallback", **st)
                                return True

                        # Keyboard fallback for icon-only flyouts: try type-to-select and arrow navigation.
                        # This stays within the user's "observe before click" constraint (no mouse movement).
                        def _infer_flyout_accels(labels: list[str]) -> list[str]:
                            """Infer likely type-to-select accelerators from observed flyout labels."""
                            try:
                                scored: list[tuple[int, str]] = []
                                for raw in (labels or [])[:20]:
                                    s = (raw or "").strip()
                                    if not s:
                                        continue
                                    l = s.lower()
                                    # Prefer explicit upload actions.
                                    score = 0
                                    if "upload" in l:
                                        score += 300
                                    if "add file" in l or "add files" in l:
                                        score += 260
                                    if "file" in l or "files" in l:
                                        score += 200
                                    if "attach" in l:
                                        score += 160
                                    if "browse" in l or "select" in l:
                                        score += 120
                                    if score <= 0:
                                        continue

                                    # Find first alphabetic character to use as type-to-select.
                                    accel = ""
                                    for ch in l:
                                        if "a" <= ch <= "z":
                                            accel = ch
                                            break
                                    if not accel:
                                        continue
                                    scored.append((score, accel))

                                scored.sort(key=lambda t: t[0], reverse=True)
                                # Deduplicate while keeping order.
                                out: list[str] = []
                                for _score, a in scored:
                                    if a not in out:
                                        out.append(a)
                                return out[:5]
                            except Exception:
                                return []

                        inferred_accels = _infer_flyout_accels(observed_labels)
                        try:
                            if inferred_accels:
                                self._log_error_event(
                                    "copilot_app_more_options_menu_infer",
                                    ok=True,
                                    strategy="type_to_select",
                                    inferred_accels=inferred_accels,
                                    labels=(observed_labels or [])[:10],
                                )
                        except Exception:
                            pass

                        def _kb_try(label: str, fn) -> bool:
                            try:
                                self._log_error_event("copilot_app_more_options_menu_keyboard", ok=True, attempt=str(label)[:80])
                            except Exception:
                                pass
                            try:
                                fn()
                            except Exception:
                                return False
                            time.sleep(max(self.delay / 2, 0.12))
                            st = _wait_file_picker(3.2)
                            if bool(st.get("has_filename") or st.get("dialog_foreground")):
                                self._log_error_event("copilot_app_attach_opened", method=f"more_options_menu_keyboard_{label}", **st)
                                return True
                            return False

                        def _press_nav(k: str, step: str) -> None:
                            """Navigation keys: can skip heavy OCR when sequence is learned."""
                            try:
                                if learned_seq:
                                    ok = bool(self._press_keys_copilot([k]))
                                    try:
                                        self._log_error_event("copilot_app_attach_key", step=str(step), keys=[k], ok=bool(ok), observed=False)
                                    except Exception:
                                        pass
                                    time.sleep(max(self.delay / 4, 0.08))
                                    return
                            except Exception:
                                pass
                            _press([k], step)

                        def _press_action(k: str, step: str) -> None:
                            """Action keys (Enter/Space): always OCR-observe before/after."""
                            _press([k], step)

                        def _type_action(s: str, step: str) -> None:
                            """Text input: always OCR-observe before/after."""
                            _type(str(s), step)

                        # Inference-driven accelerators first (based on OCR-observed labels).
                        for a in (inferred_accels or []):
                            if _kb_try(
                                f"type_{a}_enter_inferred",
                                lambda a=a: (_type_action(str(a), f"flyout_type_{a}"), _press_action("enter", "flyout_enter")),
                            ):
                                return True

                        # Then try common accelerators.
                        if _kb_try("type_u_enter", lambda: (_type_action("u", "flyout_type_u"), _press_action("enter", "flyout_enter"))):
                            return True
                        if _kb_try("type_a_enter", lambda: (_type_action("a", "flyout_type_a"), _press_action("enter", "flyout_enter"))):
                            return True

                        # Try navigating to the first few items.
                        if _kb_try("down_enter", lambda: (_press_nav("down", "flyout_down"), _press_action("enter", "flyout_enter"))):
                            return True
                        if _kb_try(
                            "enter_only",
                            lambda: _press_action("enter", "flyout_enter"),
                        ):
                            return True
                        if _kb_try(
                            "tab_enter",
                            lambda: (_press_nav("tab", "flyout_tab"), _press_action("enter", "flyout_enter")),
                        ):
                            return True
                        if _kb_try(
                            "tab_space",
                            lambda: (_press_nav("tab", "flyout_tab"), _press_action("space", "flyout_space")),
                        ):
                            return True
                        for i in range(2, 7):
                            if _kb_try(
                                f"down{i}_enter",
                                lambda i=i: ([ _press_nav("down", f"flyout_down_{j+1}") for j in range(i) ], _press_action("enter", "flyout_enter")),
                            ):
                                return True

                        # Some flyouts are horizontal icon toolbars: try left/right navigation.
                        for i in range(1, 6):
                            if _kb_try(
                                f"right{i}_enter",
                                lambda i=i: ([ _press_nav("right", f"flyout_right_{j+1}") for j in range(i) ], _press_action("enter", "flyout_enter")),
                            ):
                                return True
                        for i in range(1, 6):
                            if _kb_try(
                                f"left{i}_enter",
                                lambda i=i: ([ _press_nav("left", f"flyout_left_{j+1}") for j in range(i) ], _press_action("enter", "flyout_enter")),
                            ):
                                return True
                        for i in range(1, 6):
                            if _kb_try(
                                f"right{i}_space",
                                lambda i=i: ([ _press_nav("right", f"flyout_right_{j+1}") for j in range(i) ], _press_action("space", "flyout_space")),
                            ):
                                return True

                        # Close the flyout if still open.
                        try:
                            self.ctrl.press_keys(["esc"])
                        except Exception:
                            pass
                        self._log_error_event(
                            "copilot_app_more_options_menu_pick",
                            ok=False,
                            reason="no_candidates",
                            roots=len(uniq_roots),
                        )
                        return False

                    menu_candidates.sort(key=lambda t: t[0], reverse=True)
                    _score, pick, mx, my, nm = menu_candidates[0]
                    self._log_error_event("copilot_app_more_options_menu_pick", ok=True, name=str(nm)[:120], x=int(mx), y=int(my))

                    # Click the chosen menu item.
                    did_click = False
                    try:
                        did_click = bool(
                            _move_observe_probe_then_maybe_click(int(mx), int(my), tag="more_options_upload", win_rect=win_rect, learned=False)
                        )
                    except Exception:
                        did_click = False
                    if not did_click:
                        # If we already matched by name, force-click at the point.
                        _move_observe_probe_then_click_any(int(mx), int(my), tag="more_options_upload_force", win_rect=win_rect, learned=False)

                    st = _wait_file_picker(6.0)
                    if bool(st.get("has_filename") or st.get("dialog_foreground")):
                        self._log_error_event("copilot_app_attach_opened", method="more_options_menu", **st)
                        return True

                    # Newer Copilot layouts may open a "Conversation Starter Options"
                    # surface from More options, which then contains an Upload entry
                    # that *itself* triggers the real file picker. Detect that surface
                    # and try one more structured click inside it before failing.
                    try:
                        fg_title_cs = str(st.get("fg_title") or "").lower()
                    except Exception:
                        fg_title_cs = ""
                    if fg_title_cs and "conversation starter" in fg_title_cs:
                        try:
                            if _click_conversation_starter_upload(win_rect):
                                return True
                        except Exception:
                            pass

                    self._log_error_event("copilot_app_attachment_failed", file=str(p), reason="more_options_no_file_picker", **st)
                    return False
                except Exception as e:
                    try:
                        self._log_error_event("copilot_app_attachment_failed", file=str(p), reason="more_options_exception", error=str(e))
                    except Exception:
                        pass
                    return False

            try:
                prev_gate = getattr(self.ctrl, "_window_gate", None)
                self.ctrl.set_window_gate(lambda: bool(self._verify_copilot_foreground() or _is_file_dialog_foreground()))
            except Exception:
                prev_gate = None

            try:
                root = Path(__file__).resolve().parent.parent
                try:
                    from .jsonlog import JsonActionLogger  # type: ignore
                    JsonActionLogger(root / "logs" / "errors" / "events.jsonl").log(
                        "copilot_app_attachment_attempted",
                        file=str(p),
                        note="keyboard_only_sequence",
                    )
                except Exception:
                    pass

                # Ensure we're in a predictable UI state.
                try:
                    self.ctrl.press_keys(["esc"])
                    time.sleep(max(self.delay / 2, 0.15))
                except Exception:
                    pass

                # Preferred: click the attach button (mouse-driven) then proceed if a file picker appears.
                learned_seq = str(os.environ.get("COPILOT_ATTACH_LEARNED", "0")).strip().lower() in {"1", "true", "yes"}
                observe_each_move = str(os.environ.get("COPILOT_ATTACH_OBSERVE_EACH_MOVE", "1")).strip().lower() in {"1", "true", "yes"}
                learned_for_nav = bool(learned_seq) or (not bool(observe_each_move))

                # Strict cursor-move policy:
                # - Only move/click when we have a specific, evidence-backed target (UIA/OCR)
                # - Disable exploratory mouse fallbacks (near-input geometry clicks, hotspot sweeps)
                # - Allow keyboard-only fallbacks (no cursor movement)
                strict_targets = str(os.environ.get("COPILOT_ATTACH_STRICT_TARGETS", "1")).strip().lower() in {"1", "true", "yes"}
                try:
                    self._log_error_event(
                        "copilot_app_attach_strict_targets",
                        ok=True,
                        enabled=bool(strict_targets),
                        note="When enabled: skip exploratory mouse fallbacks; only click validated targets",
                    )
                except Exception:
                    pass

                # Track whether we've successfully triggered a picker-opening action.
                clicked = False

                # Remember where we clicked the '+' / More options button (used to probe the flyout).
                last_more_options_xy: Optional[tuple[int, int]] = None

                # First: this Copilot layout uses a '+' button on the right, usually named "More options".
                # Clicking it is the most reliable way to open the upload picker.
                def _click_input_more_options(win_rect: Optional[dict]) -> bool:
                    if self.dry_run:
                        return True
                    if not self.winman or not win_rect:
                        return False
                    try:
                        import uiautomation as auto  # type: ignore

                        hwnd = self.winman.get_foreground()
                        root = auto.ControlFromHandle(int(hwnd)) if hwnd else auto.GetRootControl()

                        wl = int(win_rect.get("left", 0))
                        wt = int(win_rect.get("top", 0))
                        ww = int(win_rect.get("width", 0))
                        wh = int(win_rect.get("height", 0))
                        y_min = wt + int(wh * 0.84)
                        x_min = wl + int(ww * 0.55)
                        x_max = wl + int(ww * 0.98)

                        candidates = []
                        scanned = 0
                        for ctl, _depth in auto.WalkControl(root, maxDepth=10):
                            scanned += 1
                            if scanned > 2600:
                                break
                            try:
                                ct = str(getattr(ctl, "ControlTypeName", "") or "").strip().lower()
                                nm = str(getattr(ctl, "Name", "") or "").strip()
                            except Exception:
                                continue
                            if ct not in {"buttoncontrol", "splitbuttoncontrol"}:
                                continue
                            nm_l = nm.lower()
                            if nm_l and ("more options" not in nm_l) and ("attach" not in nm_l) and ("upload" not in nm_l) and ("add file" not in nm_l):
                                continue
                            try:
                                br = getattr(ctl, "BoundingRectangle", None)
                                cx = int((br.left + br.right) / 2) if br else 0
                                cy = int((br.top + br.bottom) / 2) if br else 0
                            except Exception:
                                cx, cy = 0, 0
                            if not cx or not cy:
                                continue
                            if cy < y_min or cx < x_min or cx > x_max:
                                continue
                            score = 0
                            if "more options" in nm_l:
                                score += 500
                            if "upload" in nm_l or "attach" in nm_l or "add file" in nm_l:
                                score += 350
                            score += int(cx / 20)
                            score += int(cy / 30)
                            candidates.append((score, cx, cy, nm))
                        if not candidates:
                            self._log_error_event("copilot_app_attach_plus_scan", ok=False, reason="no_candidates")
                            return False
                        candidates.sort(key=lambda t: t[0], reverse=True)
                        _score, cx, cy, nm = candidates[0]
                        try:
                            nonlocal last_more_options_xy
                            last_more_options_xy = (int(cx), int(cy))
                        except Exception:
                            pass
                        self._log_error_event("copilot_app_attach_plus_scan", ok=True, x=int(cx), y=int(cy), name=str(nm)[:120])
                        return bool(
                            _move_observe_probe_then_click_any(
                                int(cx),
                                int(cy),
                                tag="input_plus_more_options",
                                win_rect=win_rect,
                                learned=bool(learned_for_nav),
                            )
                        )
                    except Exception as e:
                        try:
                            self._log_error_event("copilot_app_attach_plus_scan", ok=False, reason="exception", error=str(e))
                        except Exception:
                            pass
                        return False

                try:
                    if self.winman:
                        hwnd0 = self.winman.get_foreground()
                        r0 = self.winman.get_window_rect(hwnd0) if hwnd0 else None
                    else:
                        r0 = None
                except Exception:
                    r0 = None

                try:
                    if (not clicked) and _click_input_more_options(r0):
                        # After '+' click, Copilot typically opens a flyout menu (not the file picker directly).
                        # If no picker is visible yet, explicitly select Upload from the flyout.
                        st = _wait_file_picker(2.0)
                        if bool(st.get("has_filename") or st.get("dialog_foreground")):
                            self._log_error_event("copilot_app_attach_opened", method="input_plus_more_options", **st)
                            clicked = True
                        else:
                            self._log_error_event(
                                "copilot_app_attach_opened",
                                method="input_plus_more_options",
                                ok=False,
                                note="no_file_picker_after_plus_click_attempt_upload_select",
                                **st,
                            )
                            try:
                                if _open_more_options_menu_then_upload_impl(r0, already_open=True, anchor_xy=last_more_options_xy):
                                    clicked = True
                            except Exception:
                                pass
                except Exception:
                    pass

                if (not clicked) and (not strict_targets):
                    try:
                        clicked = bool(self._copilot_app_click_attach_button(observe_fn=_observe_step if (not learned_for_nav) else None))
                    except Exception:
                        clicked = False
                    if clicked:
                        st = _wait_file_picker(3.5)
                        if bool(st.get("has_filename") or st.get("dialog_foreground")):
                            self._log_error_event("copilot_app_attach_opened", method="mouse_click", **st)
                        else:
                            self._log_error_event("copilot_app_attach_opened", method="mouse_click", ok=False, note="no_file_picker", **st)
                            clicked = False

                # Alternate: locate the input field and click the nearest left-side button (+/attach).
                if (not clicked) and (not strict_targets):
                    try:
                        if self.winman:
                            hwnd0 = self.winman.get_foreground()
                            r0 = self.winman.get_window_rect(hwnd0) if hwnd0 else None
                        else:
                            r0 = None
                    except Exception:
                        r0 = None

                    def _click_attach_near_input(win_rect: Optional[dict]) -> bool:
                        if self.dry_run:
                            return True
                        if not self.winman:
                            return False
                        if not win_rect:
                            return False
                        try:
                            import uiautomation as auto  # type: ignore

                            hwnd = self.winman.get_foreground()
                            root = auto.ControlFromHandle(int(hwnd)) if hwnd else auto.GetRootControl()

                            # Find the lowest "Ask anything"-like input.
                            input_edit = None
                            input_br = None
                            scanned = 0
                            best_y = -1
                            for ctl, _depth in auto.WalkControl(root, maxDepth=10):
                                scanned += 1
                                if scanned > 2600:
                                    break
                                try:
                                    ct = str(getattr(ctl, "ControlTypeName", "") or "").strip().lower()
                                    nm = str(getattr(ctl, "Name", "") or "").strip()
                                except Exception:
                                    continue
                                if ct != "editcontrol":
                                    continue
                                nm_l = nm.lower()
                                # Prefer the known placeholder, but accept other edit controls very near bottom.
                                if nm_l and ("ask" not in nm_l):
                                    continue
                                try:
                                    br = getattr(ctl, "BoundingRectangle", None)
                                    if not br:
                                        continue
                                    cy = int((br.top + br.bottom) / 2)
                                except Exception:
                                    continue
                                # Keep within bottom portion of Copilot.
                                try:
                                    wt = int(win_rect.get("top", 0))
                                    wh = int(win_rect.get("height", 0))
                                    if cy < (wt + int(wh * 0.70)):
                                        continue
                                except Exception:
                                    pass
                                if cy > best_y:
                                    best_y = cy
                                    input_edit = ctl
                                    input_br = br

                            if input_edit is None or input_br is None:
                                self._log_error_event("copilot_app_attach_near_input", ok=False, reason="input_edit_not_found")
                                return False

                            edit_left = int(input_br.left)
                            edit_right = int(input_br.right)
                            edit_top = int(input_br.top)
                            edit_bottom = int(input_br.bottom)
                            # Candidate region is the input-row to the RIGHT of the input field (where '+' lives).
                            y_min = edit_top - 12
                            y_max = edit_bottom + 12
                            win_left = int(win_rect.get("left", 0))
                            win_w = int(win_rect.get("width", 0))
                            x_min = min(win_left + win_w - 2, max(win_left, edit_right + 2))
                            x_max = win_left + win_w - 2

                            btn_candidates = []
                            scanned = 0
                            for ctl, _depth in auto.WalkControl(root, maxDepth=10):
                                scanned += 1
                                if scanned > 3000:
                                    break
                                try:
                                    ct = str(getattr(ctl, "ControlTypeName", "") or "").strip().lower()
                                except Exception:
                                    continue
                                if ct not in {"buttoncontrol", "splitbuttoncontrol"}:
                                    continue
                                try:
                                    nm = str(getattr(ctl, "Name", "") or "").strip()
                                except Exception:
                                    nm = ""
                                nm_l = nm.lower()
                                if nm_l and any(k in nm_l for k in ("microphone", "mic", "voice", "dictat", "send", "submit")):
                                    continue
                                try:
                                    br = getattr(ctl, "BoundingRectangle", None)
                                    if not br:
                                        continue
                                    cx = int((br.left + br.right) / 2)
                                    cy = int((br.top + br.bottom) / 2)
                                except Exception:
                                    continue
                                # Only accept points inside the input-row band and to the right of the edit.
                                if cy < y_min or cy > y_max:
                                    continue
                                if cx < x_min or cx > x_max:
                                    continue
                                # Score: prefer the right-side '+' / More options.
                                score = 0
                                score += 500 - min(500, abs(edit_right - cx))
                                score += int(cy / 25)
                                if nm_l and ("more options" in nm_l or nm_l.strip() in {"+", "plus"}):
                                    score += 500
                                if nm_l and any(k in nm_l for k in ("attach", "upload", "add file", "add files")):
                                    score += 250
                                btn_candidates.append((score, cx, cy, nm))

                            if not btn_candidates:
                                # Some Copilot builds render the +/attach as a non-Button control.
                                # Fall back to a few safe geometry clicks near the input row on the right.
                                self._log_error_event("copilot_app_attach_near_input", ok=False, reason="no_button_candidates")
                                mid_y = int((edit_top + edit_bottom) / 2)
                                try_points = [
                                    (edit_right + 12, mid_y, "right_of_edit"),
                                    (edit_right + 42, mid_y, "right_of_edit_2"),
                                    (max(win_left + 10, int(win_left + win_w - 70)), mid_y, "far_right"),
                                ]
                                for tx, ty, tname in try_points:
                                    if tx < (win_left + 2) or tx > (win_left + win_w - 2):
                                        continue
                                    if ty < y_min or ty > y_max:
                                        continue
                                    self._log_error_event(
                                        "copilot_app_attach_near_input_point_try",
                                        x=int(tx),
                                        y=int(ty),
                                        tag=str(tname),
                                    )
                                    _move_observe_probe_then_click_any(
                                        int(tx),
                                        int(ty),
                                        tag=f"attach_near_input_{tname}",
                                        win_rect=win_rect,
                                        learned=False,
                                    )
                                    st = _wait_file_picker(1.8)
                                    if bool(st.get("has_filename") or st.get("dialog_foreground")):
                                        self._log_error_event("copilot_app_attach_opened", method=f"near_input_point_{tname}", **st)
                                        return True
                                return False
                            btn_candidates.sort(key=lambda t: t[0], reverse=True)
                            _score, cx, cy, nm = btn_candidates[0]
                            self._log_error_event("copilot_app_attach_near_input", ok=True, x=int(cx), y=int(cy), name=str(nm)[:120])
                            # Click unconditionally (we already constrained geometry tightly).
                            return bool(
                                _move_observe_probe_then_click_any(
                                    int(cx),
                                    int(cy),
                                    tag="attach_near_input",
                                    win_rect=win_rect,
                                    learned=False,
                                )
                            )
                        except Exception as e:
                            try:
                                self._log_error_event("copilot_app_attach_near_input", ok=False, reason="exception", error=str(e))
                            except Exception:
                                pass
                            return False

                    try:
                        if _click_attach_near_input(r0):
                            st = _wait_file_picker(3.5)
                            if bool(st.get("has_filename") or st.get("dialog_foreground")):
                                self._log_error_event("copilot_app_attach_opened", method="near_input_click", **st)
                                clicked = True
                            else:
                                self._log_error_event("copilot_app_attach_opened", method="near_input_click", ok=False, note="no_file_picker", **st)
                    except Exception:
                        pass

                # Alternate: Copilot layouts where uploads live under 'More options'.
                if (not clicked) and (not strict_targets):
                    try:
                        if self.winman:
                            hwnd0 = self.winman.get_foreground()
                            r0 = self.winman.get_window_rect(hwnd0) if hwnd0 else None
                        else:
                            r0 = None
                    except Exception:
                        r0 = None
                    try:
                        if _open_more_options_menu_then_upload(r0):
                            clicked = True
                    except Exception:
                        pass

                # Mouse fallback: click a bottom-left hotspot where the attach/plus button often lives.
                if (not clicked) and (not strict_targets):
                    try:
                        if self.winman:
                            hwnd0 = self.winman.get_foreground()
                            r0 = self.winman.get_window_rect(hwnd0) if hwnd0 else None
                        else:
                            r0 = None
                        if r0 and int(r0.get("width", 0)) > 50 and int(r0.get("height", 0)) > 50:
                            # Emit a one-shot math reminder for this attach attempt.
                            try:
                                cur_x, cur_y = _mouse_pos()
                                self._log_error_event(
                                    "copilot_app_attach_math_reminder",
                                    note="Targets are computed as x=left+width*fx, y=top+height*fy; deltas dx=x-cur_x, dy=y-cur_y",
                                    cursor_x=int(cur_x),
                                    cursor_y=int(cur_y),
                                    win_left=int(r0.get("left", 0)),
                                    win_top=int(r0.get("top", 0)),
                                    win_width=int(r0.get("width", 0)),
                                    win_height=int(r0.get("height", 0)),
                                    learned=bool(learned_for_nav),
                                )
                            except Exception:
                                pass
                            # Try a few conservative hotspots near the input bar's left side.
                            spots = [
                                (0.03, 0.985, "mouse_hotspot_ultra_low_far_left"),
                                (0.05, 0.985, "mouse_hotspot_ultra_low_left"),
                                (0.07, 0.975, "mouse_hotspot_very_low_left"),
                                (0.12, 0.975, "mouse_hotspot_very_low_mid"),
                                (0.07, 0.96, "mouse_hotspot_low_left"),
                                (0.14, 0.96, "mouse_hotspot_low_mid"),
                                (0.04, 0.92, "mouse_hotspot_far_left"),
                                (0.07, 0.92, "mouse_hotspot_left"),
                                (0.10, 0.90, "mouse_hotspot_left_mid"),
                                (0.07, 0.88, "mouse_hotspot_left_upper"),
                                (0.14, 0.92, "mouse_hotspot_center_left"),
                                (0.20, 0.92, "mouse_hotspot_center"),
                                # Right-side '+' / More options button in this Copilot layout.
                                (0.92, 0.90, "mouse_hotspot_plus_right"),
                                (0.95, 0.90, "mouse_hotspot_plus_far_right"),
                                (0.90, 0.92, "mouse_hotspot_right_upper"),
                            ]
                            for xf, yf, tag in spots:
                                x0 = int(r0["left"] + int(r0["width"] * float(xf)))
                                y0 = int(r0["top"] + int(r0["height"] * float(yf)))
                                try:
                                    self._log_error_event(
                                        "copilot_app_attach_hotspot_try",
                                        x=int(x0),
                                        y=int(y0),
                                        tag=str(tag),
                                        fx=float(xf),
                                        fy=float(yf),
                                    )
                                except Exception:
                                    pass
                                # Navigation attempt: move -> observe -> probe -> click only if plausible.
                                did_click = _move_observe_probe_then_maybe_click(
                                    int(x0),
                                    int(y0),
                                    tag=str(tag),
                                    win_rect=r0,
                                    learned=bool(learned_for_nav),
                                )
                                if not did_click:
                                    continue
                                time.sleep(max(self.delay / 2, 0.25))
                                st2 = _wait_file_picker(2.3)
                                if bool(st2.get("has_filename") or st2.get("dialog_foreground")):
                                    clicked = True
                                    self._log_error_event("copilot_app_attach_opened", method=str(tag), **st2)
                                    break
                    except Exception:
                        pass

                # Fallback: keyboard sequence (tab to attach target, Enter, Down, Enter).
                if not clicked:
                    def _focused_attach_anchor() -> tuple[bool, str, Optional[tuple[int, int]]]:
                        """Return (found, kind, anchor_xy) for a plausible attach gateway.

                        Strict targets: this is used to open the '+'/More options flyout without mouse scanning.
                        """
                        ct, nm = self._copilot_app_focused_name()
                        ct_l = (ct or "").strip().lower()
                        nm_l = (nm or "").strip().lower()

                        # Named controls are the strongest signal.
                        # When possible, also return the control's center so downstream
                        # logic can treat this as an anchor for a flyout/menu.
                        if any(k in nm_l for k in ("upload", "attach", "add file", "add files", "choose file", "choose files")):
                            try:
                                import uiautomation as auto  # type: ignore

                                fc = auto.GetFocusedControl()
                                br = getattr(fc, "BoundingRectangle", None)
                                if br:
                                    cx = int((br.left + br.right) / 2)
                                    cy = int((br.top + br.bottom) / 2)
                                    return True, "named", (cx, cy)
                            except Exception:
                                pass
                            return True, "named", None
                        if "more options" in nm_l or nm_l.strip() in {"+", "plus"}:
                            # More options is the known gateway to Upload.
                            try:
                                import uiautomation as auto  # type: ignore

                                fc = auto.GetFocusedControl()
                                br = getattr(fc, "BoundingRectangle", None)
                                if br:
                                    return True, "more_options_named", (int((br.left + br.right) / 2), int((br.top + br.bottom) / 2))
                            except Exception:
                                pass
                            return True, "more_options_named", None

                        # Icon-only buttons: accept if focused is a Button in the bottom input row.
                        try:
                            import uiautomation as auto  # type: ignore

                            if ct_l != "buttoncontrol":
                                return False, "", None
                            if not self.winman:
                                return False, "", None
                            hwnd0 = self.winman.get_foreground()
                            r0 = self.winman.get_window_rect(hwnd0) if hwnd0 else None
                            if not r0:
                                return False, "", None
                            fc = auto.GetFocusedControl()
                            br = getattr(fc, "BoundingRectangle", None)
                            if not br:
                                return False, "", None
                            cx = int((br.left + br.right) / 2)
                            cy = int((br.top + br.bottom) / 2)
                            win_left = int(r0.get("left", 0))
                            win_top = int(r0.get("top", 0))
                            win_w = int(r0.get("width", 0))
                            win_h = int(r0.get("height", 0))
                            y_min = win_top + int(win_h * 0.86)
                            if cy < y_min:
                                return False, "", None

                            # Copilot layouts vary: some have attach on left, others have '+' (More options) on right.
                            left_min = win_left + int(win_w * 0.03)
                            left_max = win_left + int(win_w * 0.45)
                            right_min = win_left + int(win_w * 0.55)
                            right_max = win_left + int(win_w * 0.98)

                            if left_min <= cx <= left_max:
                                return True, "icon_button_left", (cx, cy)
                            if right_min <= cx <= right_max:
                                return True, "icon_button_right", (cx, cy)
                            return False, "", None
                        except Exception:
                            return False, "", None

                    found = False
                    found_kind = ""
                    found_anchor: Optional[tuple[int, int]] = None
                    for i in range(max(1, int(tab_count))):
                        if not _press(["tab"], f"tab_{i+1}"):
                            self._log_error_event("copilot_app_attachment_failed", file=str(p), reason="tab_failed", i=i+1)
                            return False
                        try:
                            ok_focus, kind, anchor_xy = _focused_attach_anchor()
                            if ok_focus:
                                found = True
                                found_kind = str(kind or "")
                                found_anchor = anchor_xy
                                break
                        except Exception:
                            pass

                    if not found:
                        self._log_error_event("copilot_app_attachment_failed", file=str(p), reason="attach_target_not_found")
                        return False

                    try:
                        self._log_error_event(
                            "copilot_app_attach_tab_target",
                            ok=True,
                            kind=str(found_kind)[:60],
                            anchor_x=int(found_anchor[0]) if found_anchor else None,
                            anchor_y=int(found_anchor[1]) if found_anchor else None,
                        )
                    except Exception:
                        pass

                    # Activate the focused control (Enter, then Space fallback).
                    if not _press(["enter"], "activate_attach"):
                        if not _press(["space"], "activate_attach_space"):
                            self._log_error_event("copilot_app_attachment_failed", file=str(p), reason="enter_open_menu_failed")
                            return False

                    # If this opened a picker directly, we're done.
                    st0 = _wait_file_picker(2.2)
                    if bool(st0.get("has_filename") or st0.get("dialog_foreground")):
                        self._log_error_event("copilot_app_attach_opened", method="keyboard_activate", **st0)
                        clicked = True
                    else:
                        # If we likely opened the '+' / More options flyout, choose Upload using evidence.
                        try:
                            if self.winman:
                                hwnd0 = self.winman.get_foreground()
                                r0 = self.winman.get_window_rect(hwnd0) if hwnd0 else None
                            else:
                                r0 = None
                        except Exception:
                            r0 = None
                        if found_anchor and _open_more_options_menu_then_upload_impl(r0, already_open=True, anchor_xy=found_anchor):
                            clicked = True
                        elif not strict_targets:
                            # Non-strict legacy: allow Down/Enter positional selection.
                            for i in range(max(1, int(down_count))):
                                if not _press(["down"], f"menu_down_{i+1}"):
                                    self._log_error_event("copilot_app_attachment_failed", file=str(p), reason="down_select_failed", i=i+1)
                                    return False
                            if not _press(["enter"], "choose_menu_item"):
                                self._log_error_event("copilot_app_attachment_failed", file=str(p), reason="enter_choose_failed")
                                return False
                        else:
                            self._log_error_event(
                                "copilot_app_attachment_failed",
                                file=str(p),
                                reason="keyboard_activate_no_picker",
                                kind=str(found_kind)[:60],
                            )
                            return False

                # File picker must now be present; otherwise we are not actually attaching.
                st = _wait_file_picker(6.0)
                if not bool(st.get("has_filename") or st.get("dialog_foreground")):
                    try:
                        fg = self.winman.get_foreground() if self.winman else None
                        info = self.winman.get_window_info(fg) if (self.winman and fg) else {}
                    except Exception:
                        info = {}
                    self._log_error_event(
                        "copilot_app_attachment_failed",
                        file=str(p),
                        reason="file_picker_not_detected",
                        fg_title=(info.get("title") or ""),
                        fg_class=(info.get("class") or ""),
                        fg_process=(info.get("process") or ""),
                        **st,
                    )
                    return False

                # File dialog should now be foreground.
                time.sleep(max(self.delay, 0.7))
                try:
                    if self.winman:
                        fg = self.winman.get_foreground()
                        info = self.winman.get_window_info(fg) if fg else {}
                        self._log_error_event(
                            "copilot_app_attach_dialog_foreground",
                            title=(info.get("title") or ""),
                            cls=(info.get("class") or ""),
                            process=(info.get("process") or ""),
                        )
                except Exception:
                    pass

                # Enter path robustly using mouse + clipboard paste.
                # Strategy:
                # - Prefer clicking "File name" edit (if detected) and pasting the full path, then Enter.
                # - Otherwise fall back to address bar + filename paste.
                prev_clip: Optional[str] = None
                try:
                    if self.winman:
                        prev_clip = str(self.winman.get_clipboard_text(timeout_s=0.25) or "")
                except Exception:
                    prev_clip = None

                def _sanitize_windows_path(s: str) -> str:
                    try:
                        t = str(s or "").strip()
                        if (t.startswith('"') and t.endswith('"')) or (t.startswith("'") and t.endswith("'")):
                            t = t[1:-1].strip()
                        # If a trailing ':' was accidentally appended (common copy/paste typo), remove it.
                        if len(t) > 2 and t.endswith(":") and not (len(t) == 2 and t[1] == ":"):
                            t = t[:-1]
                        # Fix doubled drive-colon typos like 'C::\Users\...'
                        if len(t) >= 3 and t[1:3] == "::":
                            t = t[0] + t[2:]
                        # Ensure 'C:\\' not 'C:Users'
                        if len(t) >= 3 and t[1] == ":" and t[2] not in {"\\", "/"}:
                            t = t[:2] + "\\" + t[2:]
                        # Normalize slashes for local drive paths.
                        if len(t) >= 3 and t[1] == ":":
                            t = t.replace("/", "\\")
                        return t
                    except Exception:
                        return str(s or "")

                def _clipboard_set_verify(text: str, tag: str) -> bool:
                    if not self.winman:
                        return False
                    target = str(text or "")
                    target_s = target.strip()
                    for attempt in range(3):
                        ok = False
                        try:
                            ok = bool(self.winman.set_clipboard_text(target, timeout_s=0.8))
                        except Exception:
                            ok = False
                        try:
                            got = str(self.winman.get_clipboard_text(timeout_s=0.35) or "")
                        except Exception:
                            got = ""
                        if ok and got.strip() == target_s:
                            return True
                        time.sleep(0.06 + 0.05 * attempt)
                    try:
                        self._log_error_event(
                            "copilot_app_clipboard_set_failed",
                            ok=False,
                            tag=str(tag)[:80],
                            target_preview=target[:120],
                            got_preview=(got or "")[:120],
                        )
                    except Exception:
                        pass
                    return False

                full_path_raw = str(p.resolve())
                dir_path_raw = str(p.resolve().parent)
                full_path = _sanitize_windows_path(full_path_raw)
                dir_path = _sanitize_windows_path(dir_path_raw)
                file_name = str(p.name)

                try:
                    if full_path != full_path_raw or dir_path != dir_path_raw:
                        self._log_error_event(
                            "copilot_app_attach_path_sanitized",
                            ok=True,
                            full_before=full_path_raw[:160],
                            full_after=full_path[:160],
                            dir_before=dir_path_raw[:160],
                            dir_after=dir_path[:160],
                        )
                except Exception:
                    pass

                pasted = False
                try:
                    if self.winman and hasattr(self.winman, "set_clipboard_text"):
                        # If we detected a File name field, click it and paste full path.
                        try:
                            fn_xy = st.get("fn_xy")
                        except Exception:
                            fn_xy = None
                        if fn_xy and isinstance(fn_xy, (list, tuple)) and len(fn_xy) == 2:
                            try:
                                click_ok = bool(self.ctrl.click_at(int(fn_xy[0]), int(fn_xy[1])))
                                try:
                                    self._log_error_event(
                                        "copilot_app_dialog_click",
                                        ok=bool(click_ok),
                                        target="file_name",
                                        x=int(fn_xy[0]),
                                        y=int(fn_xy[1]),
                                    )
                                except Exception:
                                    pass
                                time.sleep(max(self.delay / 3, 0.12))
                            except Exception:
                                pass
                            # Ensure the input field is actually focused before paste.
                            # Alt+N is the canonical accelerator for the File name field in common dialogs.
                            try:
                                _press(["alt", "n"], "dialog_focus_filename_after_click")
                            except Exception:
                                pass
                            try:
                                _clipboard_set_verify(full_path, "dialog_full_path")
                            except Exception:
                                pass
                            _press(["ctrl", "a"], "dialog_filename_select_all")
                            if not _press(["ctrl", "v"], "dialog_filename_paste"):
                                raise RuntimeError("paste_fullpath_failed")
                            if not _press(["enter"], "dialog_confirm_open"):
                                raise RuntimeError("confirm_open_failed")
                            pasted = True

                        if pasted:
                            pass
                        else:
                            # Address bar: directory
                            if not _press(["ctrl", "l"], "dialog_focus_address"):
                                _press(["alt", "d"], "dialog_focus_address_alt")
                            try:
                                _clipboard_set_verify(dir_path, "dialog_dir_path")
                            except Exception:
                                pass
                            _press(["ctrl", "a"], "dialog_address_select_all")
                            if not _press(["ctrl", "v"], "dialog_address_paste"):
                                raise RuntimeError("paste_address_failed")
                            _press(["enter"], "dialog_address_enter")
                            time.sleep(max(self.delay, 0.5))

                            # File name: filename only
                            ok_focus_name = _press(["alt", "n"], "dialog_focus_filename")
                            if not ok_focus_name:
                                for i in range(4):
                                    _press(["tab"], f"dialog_tab_to_filename_{i+1}")
                            try:
                                _clipboard_set_verify(file_name, "dialog_file_name")
                            except Exception:
                                pass
                            _press(["ctrl", "a"], "dialog_filename_select_all")
                            if not _press(["ctrl", "v"], "dialog_filename_paste"):
                                raise RuntimeError("paste_filename_failed")
                            if not _press(["enter"], "dialog_confirm_open"):
                                raise RuntimeError("confirm_open_failed")
                            pasted = True
                except Exception:
                    pasted = False

                if not pasted:
                    # Final fallback: type the full file path into the filename field and confirm.
                    if not _type(full_path, "type_file_path"):
                        self._log_error_event("copilot_app_attachment_failed", file=str(p), reason="type_path_failed")
                        return False
                    if not _press(["enter"], "confirm_file_path"):
                        self._log_error_event("copilot_app_attachment_failed", file=str(p), reason="enter_confirm_path_failed")
                        return False

                # Best-effort restore previous clipboard content.
                try:
                    if self.winman and (prev_clip is not None):
                        self.winman.set_clipboard_text(prev_clip, timeout_s=0.6)
                except Exception:
                    pass

                # Return to Copilot and settle on the input field (next step is: type message -> Enter).
                time.sleep(max(self.delay, 0.8))
                self.focus_copilot_app()
                time.sleep(max(self.delay / 2, 0.2))
                try:
                    self._copilot_app_input_ready()
                except Exception:
                    pass
                _observe_step("attach_done")

                self._log_error_event("copilot_app_attachment_sent", file=str(p))
                return True
            finally:
                try:
                    if prev_gate is not None:
                        self.ctrl.set_window_gate(prev_gate)
                    else:
                        self.ctrl.set_window_gate(None)
                except Exception:
                    pass
        except Exception:
            return False

    def wait_for_copilot_app_reply(self, ocr: Any, *,
                                  expect_substring: Optional[str] = None,
                                  timeout_s: float = 45.0,
                                  interval_s: float = 2.0,
                                  react_every: int = 3,
                                  save_dir: Optional[Path] = None) -> str:
        """Wait long enough for Copilot app to respond, OCR-observe, and react.

        Reaction strategy (best-effort, low risk):
        - Refocus Copilot app
        - Nudge scroll to bottom (PageDown)
        - Re-press Enter once in case the send keystroke was dropped
        """
        t0 = time.time()
        last_text = ""
        len_at_last_react = 0
        stale_reacts = 0
        ticks = 0
        resent = 0

        def _contains_expected(text: str, expected: Optional[str]) -> bool:
            if not expected:
                return False
            raw = text or ""
            exp = str(expected)
            if exp and exp in raw:
                return True
            exp_hex = re.sub(r"[^0-9a-fA-F]", "", exp).lower()
            if not exp_hex or len(exp_hex) < 8:
                return False
            raw_hex = re.sub(r"[^0-9a-fA-F]", "", raw).lower()
            return exp_hex in raw_hex

        while (time.time() - t0) < float(timeout_s):
            ticks += 1
            cur = ""
            try:
                cur = self.read_copilot_app_text(ocr, save_dir=save_dir, focus_first=False) or ""
            except Exception:
                cur = ""

            if cur:
                last_text = cur
                if _contains_expected(cur, expect_substring):
                    try:
                        self._log_error_event("copilot_app_reply_detected", expect=expect_substring, chars=len(cur))
                    except Exception:
                        pass
                    return cur

            # React periodically: focus + scroll + (optionally) resend Enter once.
            if react_every > 0 and (ticks % max(1, int(react_every)) == 0):
                try:
                    self._log_error_event("copilot_app_react", tick=ticks, resent=resent)
                except Exception:
                    pass
                try:
                    # Only refocus if we actually lost foreground; otherwise don't thrash.
                    if not self._verify_copilot_foreground():
                        self.focus_copilot_app()
                        time.sleep(max(self.delay, 0.4))
                except Exception:
                    pass

                # Optional keepalive: if Copilot is foreground but progress appears stale,
                # briefly focus VS Code then return to Copilot. This can help VS Code agent mode
                # continue when it is waiting for user response (user-observed taskbar red state).
                try:
                    keepalive = str(os.environ.get("COPILOT_APP_VSCODE_KEEPALIVE", "1")).strip().lower() in {"1", "true", "yes"}
                    stale_limit = int(os.environ.get("COPILOT_APP_VSCODE_KEEPALIVE_STALE_REACTS", "3"))
                    if keepalive:
                        # Staleness heuristic: if observed text length hasn't changed since last react cycle.
                        cur_len = int(len(last_text or ""))
                        if cur_len > 0 and cur_len == int(len_at_last_react):
                            stale_reacts += 1
                        else:
                            stale_reacts = 0
                            len_at_last_react = cur_len
                        if stale_reacts >= max(1, stale_limit):
                            stale_reacts = 0
                            try:
                                self._log_error_event("copilot_app_keepalive_vscode")
                            except Exception:
                                pass
                            try:
                                self.focus_vscode_window()
                            except Exception:
                                pass
                            time.sleep(max(self.delay, 0.35))
                            try:
                                self.focus_copilot_app()
                            except Exception:
                                pass
                            time.sleep(max(self.delay, 0.35))
                except Exception:
                    pass
                try:
                    # Try to jump to newest content first.
                    self._press_keys_copilot(["end"])
                    time.sleep(max(self.delay / 2, 0.2))
                    self._press_keys_copilot(["pagedown"])
                    time.sleep(max(self.delay / 2, 0.2))
                except Exception:
                    pass
                if resent < 1:
                    resent += 1
                    try:
                        self._press_keys_copilot(["enter"])
                    except Exception:
                        pass

            time.sleep(max(0.2, float(interval_s)))

        return last_text
