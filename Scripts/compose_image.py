from __future__ import annotations

import argparse
from pathlib import Path
from src.image_compose import compose_card


def main():
    ap = argparse.ArgumentParser(description="Compose a simple image card with title/subtitle/bullets")
    ap.add_argument("out", type=Path, help="Output PNG path")
    ap.add_argument("--title", type=str, default="AI Coder Controller")
    ap.add_argument("--subtitle", type=str, default="")
    ap.add_argument("--bullet", action="append", default=[], help="Add a bullet line (repeatable)")
    ap.add_argument("--overlay", type=Path, default=None)
    ap.add_argument("--size", type=str, default="1200x628", help="WxH, e.g., 1200x628")
    args = ap.parse_args()

    try:
        w, h = [int(x) for x in args.size.lower().split("x", 1)]
    except Exception:
        w, h = 1200, 628

    compose_card(
        args.out,
        width=w,
        height=h,
        title=args.title,
        subtitle=args.subtitle,
        bullets=args.bullet,
        overlay=args.overlay,
    )
    print(f"Wrote {args.out}")


if __name__ == "__main__":
    main()
