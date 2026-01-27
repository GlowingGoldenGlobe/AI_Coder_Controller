from __future__ import annotations
"""Legacy 2+2 reply verifier.

This script was originally used by the "commit+verify 2+2" flow to
check for a visual "4" in Copilot's reply. The current, preferred
path for commit+verify tests is Scripts/verify_reply.py together with
Scripts/commit_and_verify_2plus2.ps1, which use a short token and/or
phrase instead of a bare digit.

The module is kept for historical logs and ad-hoc experiments but is
no longer referenced by the main workflows. New work should target
verify_reply.py rather than extending this file.
"""

import json
import re
import time
from pathlib import Path
import argparse

from src.control import Controller, SafetyLimits
from src.vsbridge import VSBridge
from src.windows import WindowsManager
from src.ocr import CopilotOCR
try:
    import pytesseract  # type: ignore
except Exception:
    pytesseract = None
try:
    import cv2
except Exception:
    cv2 = None
try:
    from PIL import Image, ImageDraw, ImageFont
except Exception:
    Image = ImageDraw = ImageFont = None
import re


def write_report(root: Path, report: dict) -> Path:
    out_dir = root / "logs" / "tests"
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    out_path = out_dir / f"verify_reply_2plus2_{ts}.json"
    out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return out_path


def main():
    ap = argparse.ArgumentParser(description="Verify that Copilot reply contains an expected phrase (or '4' by default).")
    ap.add_argument("--phrase", type=str, default="", help="Phrase to search for in OCR text; if empty, fall back to detecting '4'.")
    args = ap.parse_args()
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

    # Prefer VS Code chat verification first
    vs.focus_vscode_window()
    time.sleep(0.35)
    vs.focus_copilot_chat_view()
    chat_settle_ms = int((ocr_cfg or {}).get("chat_settle_ms", 1000))
    time.sleep(max(0.8, chat_settle_ms / 1000.0))
    chat_meta = vs.read_copilot_chat_text(ocr, save_dir=ocr_debug, return_meta=True)
    # chat_meta may be a dict with image_path/elements; prefer any text if present
    text = ""
    image_for_check = None
    source = "chat"
    if isinstance(chat_meta, dict):
        text = (chat_meta.get("text") or "")
        image_for_check = chat_meta.get("image_path") or None
    else:
        text = chat_meta or ""

    # Fallback: attempt Copilot app if chat text is too short
    if len(text) < 8:
        vs.focus_copilot_app()
        app_settle_ms = int((ocr_cfg or {}).get("app_settle_ms", 1000))
        time.sleep(max(1.0, app_settle_ms / 1000.0))
        app_meta = vs.read_copilot_app_text(ocr, save_dir=ocr_debug, return_meta=True)
        if isinstance(app_meta, dict):
            app_text = (app_meta.get("text") or "")
            if app_text:
                text = app_text
                source = "fallback_app"
            if not image_for_check:
                image_for_check = app_meta.get("image_path") or None
        else:
            if app_meta:
                text = app_meta or ""
                source = "fallback_app"

    def _detect_digit_in_image(path: str, digit: str = "4") -> bool:
        if not path:
            return False
        try:
            if pytesseract is not None:
                img = Image.open(path) if Image is not None else None
                if img is not None:
                    txt = pytesseract.image_to_string(img) or ""
                    return bool(re.search(r"(^|\b)" + re.escape(digit) + r"(\b|$)", txt))
        except Exception:
            pass
        # Fallback: basic template match using OpenCV + PIL-generated digit template
        try:
            if cv2 is None or Image is None:
                return False
            import numpy as np
            img = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
            if img is None:
                return False
            # generate template
            tpl_w, tpl_h = 64, 96
            tpl_img = Image.new("L", (tpl_w, tpl_h), color=255)
            draw = ImageDraw.Draw(tpl_img)
            try:
                font = ImageFont.load_default()
            except Exception:
                font = None
            draw.text((8, 8), digit, fill=0, font=font)
            tpl = np.array(tpl_img)
            res = cv2.matchTemplate(img, tpl, cv2.TM_CCOEFF_NORMED)
            minv, maxv, minloc, maxloc = cv2.minMaxLoc(res)
            return maxv > 0.6
        except Exception:
            return False

    phrase = (args.phrase or "").strip()

    # Primary: check for a user-specified phrase in the text.
    has_phrase = False
    if phrase:
        # Case-insensitive substring match for robustness.
        has_phrase = phrase.lower() in (text or "").lower()

    has_four = False
    if not phrase:
        has_four = bool(re.search(r"(^|\b)4(\b|$)", text))
        # If not found in text, try image-based detection (non-OCR fallback)
        if not has_four and image_for_check:
            try:
                has_four = _detect_digit_in_image(image_for_check, "4")
            except Exception:
                has_four = False
    report = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "chars": len(text),
        "preview": text[:200],
        "phrase": phrase,
        "contains_phrase": has_phrase,
        "contains_4": has_four,
        "source": source,
    }
    outp = write_report(root, report)
    print("Verify reply report:", outp)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    # Success if phrase found (when provided) or, for legacy runs, if '4' detected.
    ok = bool(has_phrase or (not phrase and has_four))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
