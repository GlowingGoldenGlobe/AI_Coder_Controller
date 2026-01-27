from __future__ import annotations
import sys
from pathlib import Path
import shutil
import time


def find_latest_png(folder: Path) -> Path | None:
    if not folder.exists():
        return None
    candidates = sorted(folder.glob("*.png"), key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0] if candidates else None


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    ocr_dir = root / "logs" / "ocr"
    cfg_dir = root / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    template_path = cfg_dir / "chat_input_template.png"

    latest = find_latest_png(ocr_dir)
    if not latest:
        print(f"No PNGs found in {ocr_dir}. Run Scripts/ocr_commit_test.py first.")
        return 2

    shutil.copy2(latest, template_path)
    ts = time.strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{ts}] Template saved: {template_path} (from {latest.name})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
