from __future__ import annotations
import json
import time
from pathlib import Path
from typing import List, Dict, Any

import numpy as np
from mss import mss

try:
    from PIL import Image, ImageOps, ImageFilter
except Exception:
    Image = None  # type: ignore
    ImageOps = None  # type: ignore
    ImageFilter = None  # type: ignore

try:
    import pytesseract
except Exception:
    pytesseract = None  # type: ignore

from src.windows import WindowsManager


def _preprocess(img: Image.Image) -> Image.Image:
    g = ImageOps.grayscale(img)
    try:
        g = ImageOps.autocontrast(g)
    except Exception:
        pass
    try:
        if ImageFilter:
            g = g.filter(ImageFilter.SHARPEN)
    except Exception:
        pass
    try:
        arr = np.array(g)
        thresh = arr.mean() * 0.9
        b = (arr > thresh).astype(np.uint8) * 255
        return Image.fromarray(b)
    except Exception:
        return g


def ocr_window_region(rect: Dict[str, int], pad: int = 8) -> Dict[str, Any]:
    # Capture a window region as an image and return the image path. Do not run OCR here.
    if Image is None:
        return {"ok": False, "error": "Pillow missing", "text": ""}
    # Shrink to avoid title bar and borders
    left = rect["left"] + pad
    top = rect["top"] + 32  # approximate title bar height
    width = max(1, rect["width"] - pad * 2)
    height = max(1, rect["height"] - (pad + 32))
    bbox = {"left": left, "top": top, "width": width, "height": height}
    try:
        with mss() as sct:
            shot = sct.grab(bbox)
        arr = np.array(shot)[:, :, :3]
        img = Image.fromarray(arr)
        # Save debug image for downstream image-based heuristics
        out_dir = Path("logs") / "ocr"
        out_dir.mkdir(parents=True, exist_ok=True)
        ts = int(time.time() * 1000)
        p = out_dir / f"powershell_{ts}.png"
        img.save(p)
        return {"ok": True, "text": "", "image_path": str(p)}
    except Exception as e:
        return {"ok": False, "error": str(e), "text": ""}


def score_idle_text(t: str) -> float:
    # Heuristic: idle PowerShell shows prompt lines with PS and path, little recent output
    t_low = t.lower()
    score = 0.0
    if ">" in t or "ps " in t_low or "ps c:" in t_low:
        score += 1.0
    if "ai_coder_controller" in t_low:
        score += 1.0
    # Penalize signs of our commit loop activity (shouldn't actually write to console, but guard anyway)
    for kw in ("commit iteration", "start mode=", "launch mode="):
        if kw in t_low:
            score -= 2.0
    # Fewer characters likely means idle prompt
    score += max(0.0, 2.0 - min(2.0, len(t) / 200.0))
    return score


def _estimate_text_lines_from_image(image_path: str) -> int:
    try:
        if Image is None:
            return 0
        img = Image.open(image_path).convert("L")
        arr = np.array(img)
        # Normalize and threshold to detect dark text rows
        thresh = max(10, int(arr.mean() * 0.7))
        mask = arr < thresh
        # Count rows that contain some dark pixels (likely text)
        rows = (mask.sum(axis=1) > (mask.shape[1] * 0.01)).astype(int)
        return int(rows.sum())
    except Exception:
        return 0


def score_idle_image_lines(lines: int, title: str = "") -> float:
    # Fewer text lines implies likely idle prompt
    score = 0.0
    if lines <= 3:
        score += 1.5
    elif lines <= 8:
        score += 1.0
    else:
        score += max(0.0, 2.0 - min(2.0, lines / 50.0))
    if title and "ai_coder_controller" in title.lower():
        score += 1.0
    return score


def main():
    root = Path(__file__).resolve().parents[1]
    win = WindowsManager()
    windows = win.list_windows()
    candidates: List[Dict[str, Any]] = []
    for w in windows:
        title = (w.get("title") or "")
        cls = (w.get("class") or "")
        if ("powershell" in title.lower()) or (cls.lower() == "consolewindowclass"):
            candidates.append(w)
    results: List[Dict[str, Any]] = []
    for w in candidates:
        hwnd = int(w.get("hwnd", "0"))
        ok_f = win.focus_hwnd(hwnd)
        time.sleep(0.4)
        rect = win.get_window_rect(hwnd) or {}
        if not rect:
            results.append({"hwnd": hwnd, "title": w.get("title"), "class": w.get("class"), "focused": ok_f, "error": "no_rect"})
            continue
        o = ocr_window_region(rect)
        if not o.get("ok"):
            results.append({"hwnd": hwnd, "title": w.get("title"), "class": w.get("class"), "focused": ok_f, "error": o.get("error")})
            continue
        image_path = o.get("image_path") or ""
        lines = _estimate_text_lines_from_image(image_path)
        idle_score = score_idle_image_lines(lines, w.get("title") or "")
        results.append({
            "hwnd": hwnd,
            "title": w.get("title"),
            "class": w.get("class"),
            "focused": ok_f,
            "rect": rect,
            "chars": 0,
            "preview": image_path,
            "text_lines": int(lines),
            "idle_score": round(idle_score, 3),
        })
    # Choose the best idle candidate (highest score)
    to_close = None
    if results:
        results_sorted = sorted(results, key=lambda r: r.get("idle_score", 0.0), reverse=True)
        to_close = results_sorted[0] if results_sorted[0].get("idle_score", 0.0) >= 1.0 else None
    closed = False
    if to_close:
        hwnd = int(to_close.get("hwnd", 0))
        # Guard: if only one candidate, still allow close if score is good
        closed = bool(win.close_hwnd(hwnd))
    report = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "candidates": results,
        "selected": to_close or {},
        "closed": bool(closed),
    }
    out_dir = root / "logs" / "tests"
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    out_path = out_dir / f"close_idle_powershell_{ts}.json"
    out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print("Close idle PowerShell report:", out_path)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
