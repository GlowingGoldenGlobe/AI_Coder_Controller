from __future__ import annotations
import json
import subprocess
import shlex
import time
from pathlib import Path
from typing import Any, Dict, List

from src.windows import WindowsManager

# Optional OCR import for idle scoring
try:
    import numpy as np
    from mss import mss
    from PIL import Image, ImageOps, ImageFilter
    import pytesseract
except Exception:
    np = None  # type: ignore
    mss = None  # type: ignore
    Image = None  # type: ignore
    ImageOps = None  # type: ignore
    ImageFilter = None  # type: ignore
    pytesseract = None  # type: ignore


def _preprocess(img):
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


def ocr_idle_score(win: WindowsManager, hwnd: int) -> Dict[str, Any]:
    if any(x is None for x in (np, mss, Image, ImageOps, ImageFilter, pytesseract)):
        return {"ok": False, "score": 0.0, "reason": "ocr_deps_missing"}
    rect = win.get_window_rect(hwnd)
    if not rect:
        return {"ok": False, "score": 0.0, "reason": "no_rect"}
    left = rect["left"] + 8
    top = rect["top"] + 32
    width = max(1, rect["width"] - 16)
    height = max(1, rect["height"] - 40)
    bbox = {"left": left, "top": top, "width": width, "height": height}
    try:
        with mss() as sct:
            shot = sct.grab(bbox)
        arr = np.array(shot)[:, :, :3][:, :, ::-1]
        img = Image.fromarray(arr)
        proc = _preprocess(img)
        text = pytesseract.image_to_string(proc, config="--psm 6 -l eng").strip()
    except Exception as e:
        return {"ok": False, "score": 0.0, "reason": str(e)}
    t = text.lower()
    score = 0.0
    if ">" in text or "ps " in t or "ps c:" in t:
        score += 1.0
    if "ai_coder_controller" in t:
        score += 1.0
    score += max(0.0, 2.0 - min(2.0, len(text) / 200.0))
    return {"ok": True, "score": round(score, 3), "chars": len(text), "preview": text[:160]}


def get_commit_loop_processes() -> List[Dict[str, Any]]:
    try:
        cmd = (
            "Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'powershell.exe' -and $_.CommandLine -match 'copilot_commit.ps1' } "
            "| Select-Object ProcessId, Name, CommandLine | ConvertTo-Json -Depth 2"
        )
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", cmd],
            capture_output=True,
            text=True,
            check=False,
        )
        out = proc.stdout.strip()
        if not out:
            return []
        data = json.loads(out)
        if isinstance(data, list):
            return data
        return [data]
    except Exception:
        return []


def main():
    root = Path(__file__).resolve().parents[1]
    win = WindowsManager()
    wins = win.list_windows()
    fg = win.get_foreground()
    fg_info = win.get_window_info(fg) if fg else {}

    # Inventory and categorization
    found_vscode = any("visual studio code" in (w.get("title","")).lower() for w in wins)
    found_copilot_app = any("copilot" in (w.get("title","" )).lower() for w in wins)
    powershells = [w for w in wins if ("powershell" in (w.get("title") or "").lower()) or ((w.get("class") or "").lower() == "consolewindowclass")]

    # Score idle PowerShells via OCR if possible
    ps_details: List[Dict[str, Any]] = []
    for w in powershells:
        hwnd = int(w.get("hwnd", "0"))
        score = ocr_idle_score(win, hwnd)
        ps_details.append({"hwnd": hwnd, "title": w.get("title"), "class": w.get("class"), **score})

    loops = get_commit_loop_processes()

    summary = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "foreground": fg_info,
        "counts": {
            "windows": len(wins),
            "powershell": len(powershells),
            "commit_loops": len(loops),
        },
        "presence": {
            "vscode": bool(found_vscode),
            "copilot_app": bool(found_copilot_app),
        },
        "actions_needed": [],
    }

    if not found_vscode:
        summary["actions_needed"].append("Open/Focus VS Code")
    if not found_copilot_app:
        summary["actions_needed"].append("Open/Focus Copilot app")
    if len(loops) > 1:
        summary["actions_needed"].append("Close duplicate commit loop windows")

    report = {
        "windows": wins,
        "foreground": fg_info,
        "powershell_ocr": ps_details,
        "commit_loop_processes": loops,
        "summary": summary,
    }

    out_dir = root / "logs" / "tests"
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    out_path = out_dir / f"assess_windows_{ts}.json"
    out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print("Assessment report:", out_path)
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
