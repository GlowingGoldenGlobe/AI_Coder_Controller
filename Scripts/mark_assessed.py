from __future__ import annotations
import argparse
import os
from pathlib import Path
from typing import Iterable

SUPPORTED_EXT = {".png", ".jpg", ".jpeg", ".mp4", ".gif"}

def mark_path(p: Path, marker_ext: str = ".assessed") -> bool:
    try:
        if not p.exists() or not p.is_file():
            return False
        if p.suffix.lower() not in SUPPORTED_EXT:
            return False
        marker = Path(str(p) + marker_ext)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("assessed\n", encoding="utf-8")
        return True
    except Exception:
        return False

def iter_media(root: Path) -> Iterable[Path]:
    for r, _d, files in os.walk(root):
        for f in files:
            p = Path(r) / f
            if p.suffix.lower() in SUPPORTED_EXT:
                yield p


def main():
    ap = argparse.ArgumentParser(description="Mark media files as assessed via sidecar markers")
    ap.add_argument("paths", nargs="*", help="Files or directories to mark. If a directory, recurses.")
    ap.add_argument("--marker-ext", default=".assessed", help="Marker extension to append (default .assessed)")
    args = ap.parse_args()

    if not args.paths:
        print("No paths provided. Example: -- C:/.../logs/screens")
        return

    total = 0
    marked = 0
    for s in args.paths:
        p = Path(s)
        if p.is_dir():
            for f in iter_media(p):
                total += 1
                if mark_path(f, args.marker_ext):
                    marked += 1
        elif p.is_file():
            total += 1
            if mark_path(p, args.marker_ext):
                marked += 1
    print(f"Marked {marked}/{total} files with marker '{args.marker_ext}'.")

if __name__ == "__main__":
    main()
