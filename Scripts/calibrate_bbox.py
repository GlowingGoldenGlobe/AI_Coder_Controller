from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, Tuple

from src.orchestrator.bbox import BBox, clamp_bbox_to_monitor, roi_to_absolute_bbox


def _try_imports():
    try:
        import numpy as np  # type: ignore
    except Exception:
        np = None

    try:
        import cv2  # type: ignore
    except Exception:
        cv2 = None

    try:
        from mss import mss  # type: ignore
    except Exception:
        mss = None

    return np, cv2, mss


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"Failed to read JSON: {path} ({exc})")


def _write_json(path: Path, obj: Dict[str, Any]) -> None:
    path.write_text(json.dumps(obj, indent=2, sort_keys=False) + "\n", encoding="utf-8")


def _select_roi(cv2: Any, img_bgr: Any) -> Tuple[int, int, int, int]:
    """Return (x,y,w,h) in image coords; (0,0,0,0) if cancelled."""

    win = "Select ROI (ENTER to confirm, ESC to cancel)"
    try:
        roi = cv2.selectROI(win, img_bgr, showCrosshair=True, fromCenter=False)
        cv2.destroyWindow(win)
    except Exception:
        # Some OpenCV builds may require destroyAllWindows.
        try:
            cv2.destroyAllWindows()
        except Exception:
            pass
        raise

    x, y, w, h = [int(v) for v in roi]
    return x, y, w, h


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Interactive bbox calibrator for capture_screenshot.bbox (uses mss + OpenCV selectROI). "
            "Outputs absolute screen coordinates; optionally writes into a pipeline JSON config."
        )
    )
    parser.add_argument("--monitor-index", type=int, default=1, help="mss monitor index (default: 1)")
    parser.add_argument("--config", type=str, default="", help="Pipeline JSON to update (optional)")
    parser.add_argument(
        "--section",
        type=str,
        default="capture_screenshot",
        help="Config section to write bbox into (default: capture_screenshot)",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="If set (and --config provided), write bbox into config file",
    )
    parser.add_argument(
        "--clamp",
        action="store_true",
        help="Clamp the bbox to the selected monitor bounds (best-effort)",
    )
    args = parser.parse_args(argv)

    np, cv2, mss = _try_imports()
    missing = [
        name
        for (name, mod) in (
            ("numpy", np),
            ("opencv-python", cv2),
            ("mss", mss),
        )
        if mod is None
    ]
    if missing:
        print(
            "Missing dependencies: "
            + ", ".join(missing)
            + "\nInstall: python -m pip install -r requirements.txt (and ensure OpenCV is available)",
            file=sys.stderr,
        )
        return 2

    monitor_index = int(args.monitor_index)
    try:
        with mss() as sct:
            mon = sct.monitors[monitor_index] if monitor_index < len(sct.monitors) else sct.monitors[0]
            shot = sct.grab(mon)
    except Exception as exc:
        print(f"Failed to capture monitor {monitor_index}: {exc}", file=sys.stderr)
        return 2

    # mss returns BGRA
    img_bgr = np.array(shot)[:, :, :3]

    print(
        "ROI selection window will open. Drag to select the region, then press ENTER to confirm (ESC cancels)."
    )

    try:
        roi = _select_roi(cv2, img_bgr)
    except Exception as exc:
        print(f"ROI selection failed: {exc}", file=sys.stderr)
        return 2

    if roi[2] <= 0 or roi[3] <= 0:
        print("Cancelled (empty ROI).", file=sys.stderr)
        return 1

    try:
        bbox = roi_to_absolute_bbox(roi, mon)
        if args.clamp:
            bbox = clamp_bbox_to_monitor(bbox, mon)
    except Exception as exc:
        print(f"Invalid ROI: {exc}", file=sys.stderr)
        return 2

    bbox_dict = bbox.as_dict()

    print("\nAbsolute bbox (paste into capture_screenshot.bbox):")
    print(json.dumps(bbox_dict, indent=2))

    if args.config:
        cfg_path = Path(args.config)
        if not cfg_path.exists():
            print(f"Config not found: {cfg_path}", file=sys.stderr)
            return 2

        cfg = _read_json(cfg_path)
        section = str(args.section)
        sec_obj = cfg.get(section)
        if not isinstance(sec_obj, dict):
            sec_obj = {}
        sec_obj["bbox"] = bbox_dict
        cfg[section] = sec_obj

        if args.write:
            _write_json(cfg_path, cfg)
            print(f"\nWrote bbox into {cfg_path} under [{section}].")
        else:
            print("\nPreview (not written; pass --write to persist):")
            print(json.dumps(cfg, indent=2))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
