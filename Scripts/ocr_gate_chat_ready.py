from __future__ import annotations
import json
import sys
import time
from pathlib import Path

from src.control import Controller, SafetyLimits
from src.vsbridge import VSBridge
from src.windows import WindowsManager
from src.ocr import CopilotOCR
from ocr_guard import InputGuard, OCREngine
import os

_cv2 = None
try:
    import cv2  # type: ignore
    _cv2 = cv2
except Exception:
    _cv2 = None


PALETTE_HINTS = ["open view:", "view:", "command palette", "focus on chat view", "copilot: open chat", "git:"]
CHAT_READY_HINTS = ["ask copilot", "type your message", "send a message", "reply"]
NAV_RULES = [
    "Intent fidelity: only act toward the stated target; don't open anything else.",
    "Palette hygiene: if command palette/search overlays are present, press ESC and re-observe.",
    "Foreground gating: only act when VS Code is focused in agent mode.",
    "Readiness gate: send only when chat input is visually ready (template/heuristic).",
    "Ambiguity abstain: if unsure, take no action and observe again.",
    "No external apps: never launch or interact with browsers; close if foreground.",
    "Idempotence: prefer safe, repeatable actions and verify after acting."
]


def looks_like_palette(text: str) -> bool:
    t = (text or "").lower()
    return any(h in t for h in PALETTE_HINTS)


def looks_chat_ready(text: str) -> bool:
    t = (text or "").lower()
    if any(h in t for h in CHAT_READY_HINTS):
        return True
    alpha = sum(c.isalpha() for c in t)
    return (alpha > 120) and (not looks_like_palette(t))


def template_ready(image_path: str, template_path: str, threshold: float = 0.85) -> bool:
    if not _cv2:
        return False
    if not (image_path and os.path.exists(image_path) and os.path.exists(template_path)):
        return False
    try:
        img = _cv2.imread(image_path, _cv2.IMREAD_GRAYSCALE)
        tpl = _cv2.imread(template_path, _cv2.IMREAD_GRAYSCALE)
        if img is None or tpl is None:
            return False
        res = _cv2.matchTemplate(img, tpl, _cv2.TM_CCOEFF_NORMED)
        _, max_val, _, _ = _cv2.minMaxLoc(res)
        return bool(max_val >= threshold)
    except Exception:
        return False


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    rules_path = root / "config" / "policy_rules.json"
    ocr_cfg_path = root / "config" / "ocr.json"

    try:
        rules = json.loads(rules_path.read_text(encoding="utf-8"))
    except Exception:
        rules = {}
    vs_cfg = rules.get("vsbridge", {}) or {}

    limits = SafetyLimits(max_clicks_per_min=60, max_keys_per_min=180)
    ctrl = Controller(mouse_speed=0.25, limits=limits, mouse_control_seconds=6, mouse_release_seconds=3)
    win = WindowsManager()
    log = lambda m: None
    vs = VSBridge(ctrl, log, winman=win, delay_ms=int(vs_cfg.get("delay_ms", 300)), dry_run=True)

    try:
        ocr_cfg = json.loads(ocr_cfg_path.read_text(encoding="utf-8"))
    except Exception:
        ocr_cfg = {"enabled": True}
    ocr_debug = root / "logs" / "ocr"
    ocr = CopilotOCR(ocr_cfg, log=lambda m: None, debug_dir=ocr_debug)

    # Ensure VS Code is front and chat view focused
    vs.focus_vscode_window()
    time.sleep(0.35)
    vs.focus_copilot_chat_view()
    settle_ms = int((ocr_cfg or {}).get("chat_settle_ms", 1000))
    time.sleep(max(0, settle_ms) / 1000.0)

    # Prefer VS Code chat target ROI if present
    alt_region = (ocr_cfg.get("targets") or {}).get("vscode_chat") if isinstance(ocr_cfg, dict) else None
    orig_region = getattr(ocr, "region_percent", None)
    try:
        if alt_region:
            setattr(ocr, "region_percent", alt_region)
        # Require pre-check via element/image capture
        guard = InputGuard(OCREngine(), root / "logs" / "events.jsonl")
        guard.require_observe("chat_pre_observe")
        res = ocr.capture_chat_text(save_dir=ocr_debug)
        elems = (res.get("elements") or []) if isinstance(res, dict) else []
        img_path = str(res.get("image_path") or "")
        template_path = str(root / "config" / "chat_input_template.png")
        # Heuristics: ready if template matches or a bottom input-like element present
        ready = template_ready(img_path, template_path) if img_path else False
        try:
            if elems and not ready:
                from PIL import Image
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
                im = Image.open(img_path)
                w_img, h_img = im.size
                for e in elems:
                    b = e.get("bbox") or {}
                    if (b.get("height") or 0) < 80 and (b.get("top") or 0) > (0.65 * h_img):
                        ready = True
                        break
        except Exception:
            pass
        # Detect palette by finding large overlay elements
        palette = False
        try:
            if elems:
                from PIL import Image
                im = Image.open(img_path)
                w_img, h_img = im.size
                area = float(w_img * h_img)
                for e in elems:
                    b = e.get("bbox") or {}
                    a = float((b.get("width") or 0) * (b.get("height") or 0))
                    if a > 0.3 * area:
                        palette = True
                        break
        except Exception:
            pass

        # RULES at the exact moment of observation
        # Foreground must be VS Code and no palette; readiness required to allow send.
        can_send = False
        reason = ""
        fg = win.get_foreground()
        fg_title = ""
        if fg:
            info = win.get_window_info(fg)
            fg_title = (info.get("title") or "").lower()
        if "visual studio code" not in fg_title:
            reason = "refocus_vscode_before_any_action"
        elif palette:
            reason = "palette_open_close_then_observe"
        elif not ready:
            reason = "not_ready_observe_again"
        else:
            can_send = True
            reason = "ready_and_focused"

        # Log final readiness decision
        guard._log("assess_chat_ready", {"can_send": can_send, "reason": reason, "palette": palette, "ready": ready, "fg_title": fg_title})
        # Exit 0 only if rules permit sending; otherwise 1
        return 0 if can_send else 1
    finally:
        try:
            if alt_region and orig_region is not None:
                setattr(ocr, "region_percent", orig_region)
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
