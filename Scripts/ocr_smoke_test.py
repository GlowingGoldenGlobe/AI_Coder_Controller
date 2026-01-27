import json
from pathlib import Path

from src.ocr import CopilotOCR


def main():
    root = Path(__file__).resolve().parents[1]
    cfg_path = root / "config" / "ocr.json"
    debug_dir = root / "logs" / "ocr"
    cfg = {}
    if cfg_path.exists():
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))

    ocr = CopilotOCR(cfg, log=print, debug_dir=debug_dir)
    print("Focusing Copilot chat first is recommended, then capturing...\n")
    res = ocr.capture_chat_text(save_dir=debug_dir)
    if not res.get("ok"):
        print("[OCR ERROR]", res.get("error"))
        if res.get("image_path"):
            print("Debug image:", res["image_path"])
        print("\nIf on Windows, install Tesseract: https://github.com/UB-Mannheim/tesseract/wiki")
        return 1

    text = res.get("text", "")
    img = res.get("image_path")
    print("Captured text (first 600 chars):\n")
    print(text[:600])
    if img:
        print("\nDebug image saved:", img)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
