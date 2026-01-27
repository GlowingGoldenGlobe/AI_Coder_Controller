from __future__ import annotations
import argparse
import json
import sys
import time
from pathlib import Path

from src.control import Controller, SafetyLimits
from src.vsbridge import VSBridge
from src.windows import WindowsManager
from src.ocr import CopilotOCR
import os

_cv2 = None
try:
    import cv2  # type: ignore
    _cv2 = cv2
except Exception:
    _cv2 = None

PALETTE_HINTS = ["open view:", "view:", "command palette", "focus on chat view", "copilot: open chat"]
BROWSER_HINTS = ["edge", "chrome", "github"]
CHAT_READY_HINTS = ["ask copilot", "type your message", "send a message"]

# Common-sense navigation rules, consulted on every observed image.
# These are intentionally general, not exact-match heuristics.
NAV_RULES = [
    "Intent fidelity: only act toward the stated target; don't open anything else.",
    "Palette hygiene: if command palette/search overlays are present, press ESC and re-observe.",
    "Foreground gating: only act when VS Code is focused in agent mode.",
    "Readiness gate: send only when chat input is visually ready (template/heuristic).",
    "Ambiguity abstain: if unsure, take no action and observe again.",
    "No external apps: never launch or interact with browsers; close if foreground.",
    "Idempotence: prefer safe, repeatable actions and verify after acting.",
    "Improve: after the run, assess error events and failed commands; record a lesson and update guards to avoid repeating the error."
]


def looks_like_palette(text: str) -> bool:
    t = text.lower()
    return any(h in t for h in PALETTE_HINTS)


def looks_like_browser_window(title: str) -> bool:
    t = title.lower()
    return any(h in t for h in BROWSER_HINTS) and ("copilot" not in t)


def looks_chat_ready(text: str) -> bool:
    t = (text or "").lower()
    if any(h in t for h in CHAT_READY_HINTS):
        return True
    # heuristic: enough words and no palette hints
    words = sum(c.isalpha() for c in t)
    return (words > 120) and (not looks_like_palette(t))


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
        _min_val, max_val, _min_loc, _max_loc = _cv2.minMaxLoc(res)
        return bool(max_val >= threshold)
    except Exception:
        return False


def evaluate_navigation_rules(context: dict) -> dict:
    """Evaluate general navigation rules against current observation.

    Returns a decision dict influencing actions:
      - must_close_palette: bool
      - must_refocus_vscode: bool
      - can_send: bool
      - reason: str
    """
    agent_mode = context.get("agent_mode") or "vscode"
    fg_title = (context.get("foreground_title") or "").lower()
    palette = bool(context.get("palette"))
    ready = bool(context.get("ready"))

    # Rule: No external apps
    if looks_like_browser_window(fg_title):
        return {
            "must_close_palette": False,
            "must_refocus_vscode": False,
            "can_send": False,
            "reason": "foreground_is_browser_close_first",
        }

    # Rule: Foreground gating (agent mode: vscode)
    if agent_mode == "vscode" and ("visual studio code" not in fg_title):
        return {
            "must_close_palette": False,
            "must_refocus_vscode": True,
            "can_send": False,
            "reason": "refocus_vscode_before_any_action",
        }

    # Rule: Palette hygiene
    if palette:
        return {
            "must_close_palette": True,
            "must_refocus_vscode": False,
            "can_send": False,
            "reason": "palette_open_close_then_observe",
        }

    # Rule: Readiness + ambiguity abstain
    if not ready:
        return {
            "must_close_palette": False,
            "must_refocus_vscode": False,
            "can_send": False,
            "reason": "not_ready_observe_again",
        }

    # Passed gates; sending is allowed
    return {
        "must_close_palette": False,
        "must_refocus_vscode": False,
        "can_send": True,
        "reason": "ready_and_focused",
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="OCR-driven observe→react navigator for VS Code Copilot chat")
    ap.add_argument("--ticks", type=int, default=30)
    ap.add_argument("--interval-ms", type=int, default=400)
    ap.add_argument("--send", type=str, default="", help="Optional message to send once chat is ready")
    ap.add_argument("--agent-mode", type=str, default="vscode", choices=["vscode"], help="Agent mode that constrains navigation")
    args = ap.parse_args()

    root = Path(__file__).resolve().parent.parent
    rules_path = root / "config" / "policy_rules.json"
    ocr_cfg_path = root / "config" / "ocr.json"
    templates_cfg_path = root / "config" / "templates.json"

    try:
        rules = json.loads(rules_path.read_text(encoding="utf-8"))
    except Exception:
        rules = {}
    vs_cfg = rules.get("vsbridge", {}) or {}
    meas_cfg = rules.get("measurement", {}) or {}
    meas_threshold = float(meas_cfg.get("threshold", 0.85))
    meas_retry = int(meas_cfg.get("retry_attempts", 2))
    meas_backoff_ms = int(meas_cfg.get("backoff_ms", 400))

    # Optional templates configuration (for curated chat-input templates).
    templates_cfg = {}
    try:
        if templates_cfg_path.exists():
            templates_cfg = json.loads(templates_cfg_path.read_text(encoding="utf-8")) or {}
    except Exception:
        templates_cfg = {}

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
    log = lambda m: print(m)
    vs = VSBridge(ctrl, log, winman=win, delay_ms=int(vs_cfg.get("delay_ms", 300)), dry_run=bool(vs_cfg.get("dry_run", False)))

    try:
        ocr_cfg = json.loads(ocr_cfg_path.read_text(encoding="utf-8"))
    except Exception:
        ocr_cfg = {"enabled": True}
    ocr_debug = root / "logs" / "ocr"
    ocr = CopilotOCR(ocr_cfg, log=log, debug_dir=ocr_debug)

    out = root / "logs" / "tests" / f"ocr_nav_{time.strftime('%Y%m%d_%H%M%S')}.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)

    sent = False
    # Track repeated palette classifications so we can back off if we keep
    # thinking "palette" while the view also looks chat-ready.
    palette_cooldown = 0
    errors_path = root / "logs" / "errors" / "events.jsonl"
    errors_path.parent.mkdir(parents=True, exist_ok=True)
    template_path = str(root / "config" / "chat_input_template.png")
    # Fallback list of curated templates, if provided via config/templates.json.
    chat_templates = []
    try:
        chat_templates = list((templates_cfg.get("chat_input", {}) or {}).get("templates", []) or [])
    except Exception:
        chat_templates = []
    for i in range(max(1, int(args.ticks))):
        # Close disallowed browser windows if foreground
        try:
            fg = win.get_foreground()
            if fg:
                info = win.get_window_info(fg)
                title = info.get("title") or ""
                if looks_like_browser_window(title):
                    win.close_window(fg)
                    with open(out, "a", encoding="utf-8") as f:
                        f.write(json.dumps({"ts": time.time(), "tick": i, "action": "close_foreground", "title": title}) + "\n")
                    # also log structured error event
                    try:
                        with open(errors_path, "a", encoding="utf-8") as ef:
                            ef.write(json.dumps({
                                "ts": time.strftime('%Y-%m-%d %H:%M:%S'),
                                "source": "ocr_observe_react_nav.py",
                                "type": "browser_foreground_closed",
                                "message": "Closed disallowed foreground browser",
                                "data": {"title": title}
                            }) + "\n")
                    except Exception:
                        pass
                    time.sleep(0.3)
        except Exception:
            pass

        # Focus VS Code window
        vs.focus_vscode_window()
        time.sleep(0.25)
        # Capture image frame, OCR text, and detect UI elements
        res = ocr.capture_chat_text(save_dir=ocr_debug)
        img_path = str(res.get("image_path") or "") if isinstance(res, dict) else ""
        elems = res.get("elements") if isinstance(res, dict) else None
        text = str(res.get("text") or "") if isinstance(res, dict) else ""

        # Heuristics combining text and geometry. We separate text and geometry
        # signals, then combine them conservatively so that a large chat panel
        # is not mistaken for a palette overlay.
        palette = False
        ready = False
        palette_text = False
        ready_text = False
        palette_geom = False
        ready_geom = False

        # Text-based hints first
        if text:
            palette_text = looks_like_palette(text)
            ready_text = looks_chat_ready(text)

        # Geometry-based hints as a fallback/refinement
        try:
            if img_path and elems:
                from PIL import Image

                im = Image.open(img_path)
                w_img, h_img = im.size
                img_area = float(w_img * h_img)
                for e in elems:
                    b = e.get("bbox") or {}
                    area = float((b.get("width") or 0) * (b.get("height") or 0))
                    top = float(b.get("top") or 0)
                    height = float(b.get("height") or 0)
                    # large overlay near the top suggests palette/command overlay
                    if area > 0.35 * img_area and top < 0.4 * h_img:
                        palette_geom = True
                    # input-like narrow box near bottom indicates readiness
                    if height < 80 and top > (0.65 * h_img):
                        ready_geom = True
        except Exception:
            pass

        # Template-based readiness: optionally require at least one curated
        # chat-input match when an image is available. This is conservative
        # and only strengthens an existing "ready" signal; it does not force
        # readiness when other signals are weak.
        if img_path:
            try:
                # Prefer explicit config/chat_input_template.png when present.
                tpl_paths = []
                if template_path and os.path.exists(template_path):
                    tpl_paths.append(template_path)
                # Add any curated templates from config/templates.json.
                for rel in chat_templates:
                    p = root / rel
                    if p.exists():
                        tpl_paths.append(str(p))
                matched = False
                for tpath in tpl_paths:
                    if template_ready(img_path, tpath, threshold=meas_threshold):
                        matched = True
                        break
                if matched:
                    ready_geom = True
            except Exception:
                pass

        # Combine signals:
        # - Palette from text is strong.
        # - Palette from geometry only counts when text does NOT already say
        #   the chat is ready; this avoids treating a normal chat panel as an overlay.
        palette = bool(palette_text or (palette_geom and not ready_text))
        ready = bool(ready_text or ready_geom)

        # Also allow template matching for chat input readiness when not already ready
        if not ready and img_path:
            ready = template_ready(img_path, template_path)

        # If we repeatedly think "palette" while also believing the chat is
        # ready, treat it as a likely false positive and back off.
        cooldown_reason = None
        if palette and ready and not palette_text:
            palette_cooldown += 1
            if palette_cooldown >= 4:
                palette = False
                cooldown_reason = "palette_cooldown_override"
        else:
            palette_cooldown = 0

        # Read and enforce RULES exactly at image observation
        fg = win.get_foreground()
        fg_title = ""
        try:
            if fg:
                info = win.get_window_info(fg)
                fg_title = info.get("title") or ""
        except Exception:
            fg_title = ""

        decision = evaluate_navigation_rules({
            "agent_mode": args.agent_mode,
            "foreground_title": fg_title,
            "palette": palette,
            "ready": ready,
        })

        # React to image per RULES
        action = None
        if decision.get("must_refocus_vscode"):
            vs.focus_vscode_window()
            action = "refocus_vscode"
        elif decision.get("must_close_palette"):
            ctrl.press_keys(["esc"])  # close palette
            action = "press_esc_palette"
        elif not decision.get("can_send"):
            # Ambiguity or not-ready: nudge safely (ESC) and observe again
            ctrl.press_keys(["esc"])
            action = "press_esc_nudge"
        else:
            if args.send and not sent:
                vs.compose_message_vscode_chat(args.send)
                sent = True
                action = "send_message"

        with open(out, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": time.time(),
                "tick": i,
                "palette": palette,
                "ready": ready,
                "action": action,
                "image_path": img_path,
                "elements_count": len(elems) if elems is not None else 0,
                "fg_title": fg_title,
                "palette_cooldown": palette_cooldown,
                "cooldown_reason": cooldown_reason,
                "rules_version": 1,
                "rules_reason": decision.get("reason"),
                "rules": NAV_RULES
            }) + "\n")

        time.sleep(max(0.05, args.interval_ms / 1000.0))

    print(f"OCR nav log: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
