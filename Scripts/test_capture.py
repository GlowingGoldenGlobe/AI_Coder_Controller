#!/usr/bin/env python3
"""Simple capture smoke test for CopilotOCR.

Writes a sample capture to logs/ and exits 0 on success, non-zero on failure.
"""
import sys
import json
from pathlib import Path
from src.ocr import CopilotOCR


def main():
    out = Path("logs/test_capture")
    out.mkdir(parents=True, exist_ok=True)
    cfg = {"enabled": True, "save_debug_images": True}
    ocr = CopilotOCR(cfg=cfg, log=print, debug_dir=out)
    res = ocr.capture_chat_text(save_dir=out)
    # normalize path for JSON
    rp = res.get("image_path")
    if hasattr(rp, "as_posix"):
        res["image_path"] = str(rp)
    Path(out / "last_report.json").write_text(json.dumps(res, indent=2), encoding="utf-8")
    elements = res.get("elements") or []
    if not res.get("image_path"):
        print("FAIL: no image saved")
        return 2
    if not elements:
        print("WARN: image captured but no elements detected")
        return 1
    print("OK: captured image and detected", len(elements), "elements")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
from pathlib import Path
from src.ocr import CopilotOCR

ocr = CopilotOCR({}, log=print, debug_dir=Path('logs/ocr_test'))
res = ocr.capture_chat_text(save_dir=Path('logs/ocr_test'))
print('RESULT:', res)
