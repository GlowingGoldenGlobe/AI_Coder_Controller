from __future__ import annotations
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
    out_path = out_dir / f"verify_reply_{ts}.json"
    out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return out_path


def main():
    ap = argparse.ArgumentParser(description="Verify that Copilot reply contains an expected phrase or token (image-based fallback).")
    ap.add_argument("--phrase", type=str, default="", help="Phrase to search for in OCR text.")
    ap.add_argument("--token", type=str, default="", help="Short token to verify via image-template matching when text search fails (preferred for concise tokens).")
    ap.add_argument("--frames", type=int, default=1, help="Number of chat frames to capture for token/image matching.")
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

    def _detect_token_in_image(path: str, token: str, threshold: float = 0.55) -> bool:
        if not path or not token:
            return False
        try:
            if Image is None or cv2 is None:
                return False
            # render token to template image
            # size scales with token length
            tpl_w = max(64, 18 * len(token))
            tpl_h = 96
            tpl_img = Image.new("L", (tpl_w, tpl_h), color=255)
            draw = ImageDraw.Draw(tpl_img)
            try:
                font = ImageFont.load_default()
            except Exception:
                font = None
            # center text
            draw.text((6, 10), token, fill=0, font=font)
            import numpy as np
            tpl_np = np.array(tpl_img)
            img = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
            if img is None:
                return False
            # Preprocess image: equalize and blur to reduce UI noise
            try:
                img_proc = cv2.equalizeHist(img)
            except Exception:
                img_proc = img
            img_proc = cv2.medianBlur(img_proc, 3)

            # Multi-scale matching: try resized templates to handle font/scale differences
            scales = [0.7, 0.85, 1.0, 1.2, 1.4]
            best = 0.0
            for s in scales:
                try:
                    new_w = max(8, int(tpl_np.shape[1] * s))
                    new_h = max(8, int(tpl_np.shape[0] * s))
                    tpl_img_resized = Image.fromarray(tpl_np).resize((new_w, new_h), resample=Image.LANCZOS)
                    tpl = np.array(tpl_img_resized)
                    if tpl.shape[0] >= img_proc.shape[0] or tpl.shape[1] >= img_proc.shape[1]:
                        continue
                    res = cv2.matchTemplate(img_proc, tpl, cv2.TM_CCOEFF_NORMED)
                    minv, maxv, minloc, maxloc = cv2.minMaxLoc(res)
                    best = max(best, float(maxv))
                    if maxv >= threshold:
                        return True
                except Exception:
                    continue
            # last resort: try lower threshold on best match
            return best >= (threshold - 0.1)
        except Exception:
            return False

    phrase = (args.phrase or "").strip()
    token = (args.token or "").strip()
    frames = int(getattr(args, "frames", 1) or 1)

    # Primary: check for a user-specified phrase in the text.
    has_phrase = False
    if phrase:
        has_phrase = phrase.lower() in (text or "").lower()

    has_token = False
    if token:
        # check token in text first
        has_token = token.lower() in (text or "").lower()
        # If not found, attempt multi-frame image/template matching
        if not has_token:
            for i in range(frames):
                # if latest capture provided an image, try matching it
                if image_for_check and _detect_token_in_image(image_for_check, token, threshold=0.55):
                    has_token = True
                    break
                # capture another frame from chat view
                try:
                    time.sleep(0.45)
                    chat_meta = vs.read_copilot_chat_text(ocr, save_dir=ocr_debug, return_meta=True)
                    if isinstance(chat_meta, dict):
                        image_for_check = chat_meta.get("image_path") or image_for_check
                        text = (chat_meta.get("text") or text)
                    else:
                        text = chat_meta or text
                except Exception:
                    pass
                if image_for_check and _detect_token_in_image(image_for_check, token, threshold=0.55):
                    has_token = True
                    break

    has_four = False
    if not phrase and not token:
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
        "token": token,
        "contains_phrase": has_phrase,
        "contains_token": has_token,
        "contains_4": has_four,
        "source": source,
    }
    outp = write_report(root, report)
    print("Verify reply report:", outp)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    # Success if phrase found, token matched, or (legacy) '4' detected.
    ok = bool(has_phrase or has_token or (not phrase and has_four))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
