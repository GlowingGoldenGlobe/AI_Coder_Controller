#!/usr/bin/env python3
"""Extract a rectangular template from a captured image and save it to the
assets/ui_templates folder for use with template-matching.

Usage:
  python Scripts/extract_template.py --image path/to/capture.png --left 10 --top 20 --width 32 --height 32 --name copy_icon
"""
import argparse
from pathlib import Path
import os

try:
    import cv2
except Exception:
    cv2 = None

try:
    from PIL import Image
except Exception:
    Image = None


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--image", required=True)
    p.add_argument("--left", type=int, required=True)
    p.add_argument("--top", type=int, required=True)
    p.add_argument("--width", type=int, required=True)
    p.add_argument("--height", type=int, required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--outdir", default="assets/ui_templates")
    args = p.parse_args()
    imgp = Path(args.image)
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    if cv2 is not None:
        img = cv2.imread(str(imgp))
        if img is None:
            raise SystemExit("failed to read image")
        l, t, w, h = args.left, args.top, args.width, args.height
        crop = img[t:t+h, l:l+w]
        outp = outdir / f"{args.name}.png"
        cv2.imwrite(str(outp), crop)
        print("wrote", outp)
        return
    if Image is not None:
        img = Image.open(imgp).convert("RGBA")
        l, t, w, h = args.left, args.top, args.width, args.height
        crop = img.crop((l, t, l + w, t + h))
        outp = outdir / f"{args.name}.png"
        crop.save(outp)
        print("wrote", outp)
        return
    raise SystemExit("no image library available (cv2 or Pillow required)")


if __name__ == "__main__":
    main()
