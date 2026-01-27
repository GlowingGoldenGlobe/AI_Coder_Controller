import argparse
import time
import os
import sys
from typing import Optional, Tuple

import numpy as np

# Try dxcam first; fallback to mss
_dxcam = None
try:
    import dxcam  # type: ignore
    _dxcam = dxcam
except Exception:
    _dxcam = None

_mss = None
try:
    import mss  # type: ignore
    import mss.tools  # noqa: F401
    _mss = mss
except Exception:
    _mss = None

import cv2  # type: ignore


def parse_region(s: Optional[str]) -> Optional[Tuple[int, int, int, int]]:
    if not s:
        return None
    parts = s.split(",")
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("--region must be 'x,y,w,h'")
    try:
        x, y, w, h = map(int, parts)
        if w <= 0 or h <= 0:
            raise ValueError
        return x, y, w, h
    except Exception as e:
        raise argparse.ArgumentTypeError("--region must be four positive integers x,y,w,h") from e


def open_writer(path: str, fps: int, size: Tuple[int, int]) -> cv2.VideoWriter:
    fourcc = cv2.VideoWriter_fourcc(*"mp4v") if path.lower().endswith(".mp4") else cv2.VideoWriter_fourcc(*"avc1")
    writer = cv2.VideoWriter(path, fourcc, fps, size)
    if not writer.isOpened():
        # Fallback to mjpg in avi if mp4 fails
        alt_path = os.path.splitext(path)[0] + ".avi"
        fourcc2 = cv2.VideoWriter_fourcc(*"MJPG")
        writer = cv2.VideoWriter(alt_path, fourcc2, fps, size)
        if not writer.isOpened():
            raise RuntimeError("Failed to open VideoWriter for both MP4 and AVI")
        print(f"VideoWriter fell back to AVI: {alt_path}")
    return writer


def ensure_dirs(path: str) -> None:
    d = os.path.dirname(os.path.abspath(path))
    if d and not os.path.exists(d):
        os.makedirs(d, exist_ok=True)


def main() -> int:
    p = argparse.ArgumentParser(description="Live screen capture (dxcam with mss fallback)")
    p.add_argument("--seconds", type=float, default=0, help="Auto-stop after N seconds (0 = manual)")
    p.add_argument("--fps", type=int, default=15, help="Target capture FPS")
    p.add_argument("--out", type=str, default="", help="Optional output video path (.mp4 or .avi)")
    p.add_argument("--preview", action="store_true", help="Show live preview window")
    p.add_argument("--scale", type=float, default=1.0, help="Scale factor (e.g., 0.5)")
    p.add_argument("--region", type=parse_region, default=None, help="x,y,w,h capture region")
    p.add_argument("--monitor", type=int, default=0, help="Monitor index (dxcam) or '1' relative (mss)")
    p.add_argument("--backend", type=str, default="auto", choices=["auto", "dxcam", "mss"], help="Force capture backend or auto-detect")
    p.add_argument("--mark-assessed", action="store_true", help="Create a sidecar marker (e.g., .assessed) for output on completion")
    p.add_argument("--marker-ext", type=str, default=".assessed", help="Marker extension to append (default .assessed)")
    args = p.parse_args()

    if _dxcam is None and _mss is None:
        print("Neither dxcam nor mss is available. Install with: pip install dxcam mss opencv-python", file=sys.stderr)
        return 2

    # Backend selection
    use_dxcam = _dxcam is not None and (args.backend in ("auto", "dxcam"))
    force_mss = args.backend == "mss"

    # Setup capture
    camera = None
    sct = None
    mon_rect = None

    if use_dxcam and not force_mss:
        try:
            camera = _dxcam.create(output_idx=args.monitor)
            camera.start(target_fps=args.fps, video_mode=True)
        except Exception as e:
            print(f"dxcam failed ({e!r}); falling back to mss", file=sys.stderr)
            use_dxcam = False

    if not use_dxcam or force_mss:
        if _mss is None:
            print("mss not available for fallback.", file=sys.stderr)
            return 2
        sct = _mss.mss()
        if args.region is None:
            # Full virtual screen in mss is monitor 0; individual monitors start at 1
            mon = sct.monitors[min(max(args.monitor + 1, 1), len(sct.monitors) - 1)]
            mon_rect = {"left": mon["left"], "top": mon["top"], "width": mon["width"], "height": mon["height"]}
        else:
            x, y, w, h = args.region
            mon_rect = {"left": x, "top": y, "width": w, "height": h}

    # Grab one frame to determine size
    consecutive_none = 0
    def grab_frame():
        if use_dxcam:
            frame = camera.grab()
            if frame is None:
                return None
            # dxcam returns BGRA
            if frame.shape[2] == 4:
                frame = cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)
            # Optional region crop
            if args.region is not None:
                x, y, w, h = args.region
                frame = frame[y:y+h, x:x+w]
            return frame
        else:
            img = sct.grab(mon_rect)
            frame = np.array(img)  # BGRA
            frame = cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)
            return frame

    first = None
    t0 = time.time()
    # Wait briefly if dxcam needs a moment to initialize
    for _ in range(50):
        first = grab_frame()
        if first is not None:
            break
        time.sleep(0.02)

    # If dxcam failed to provide a frame, fallback to mss
    if first is None and use_dxcam and _mss is not None and args.backend == "auto":
        print("dxcam produced no frames; switching to mss backend", file=sys.stderr)
        try:
            camera.stop()
        except Exception:
            pass
        use_dxcam = False
        sct = _mss.mss()
        if args.region is None:
            mon = sct.monitors[min(max(args.monitor + 1, 1), len(sct.monitors) - 1)]
            mon_rect = {"left": mon["left"], "top": mon["top"], "width": mon["width"], "height": mon["height"]}
        else:
            x, y, w, h = args.region
            mon_rect = {"left": x, "top": y, "width": w, "height": h}
        # try again
        for _ in range(20):
            first = grab_frame()
            if first is not None:
                break
            time.sleep(0.02)

    if first is None:
        print("Failed to capture initial frame.", file=sys.stderr)
        if use_dxcam and camera:
            camera.stop()
        if sct:
            sct.close()
        return 3

    if args.scale != 1.0:
        first = cv2.resize(first, None, fx=args.scale, fy=args.scale, interpolation=cv2.INTER_AREA)

    h, w = first.shape[:2]
    writer = None
    if args.out:
        ensure_dirs(args.out)
        writer = open_writer(args.out, args.fps, (w, h))

    window_name = "Monitor Live"
    if args.preview:
        cv2.namedWindow(window_name, cv2.WINDOW_NORMAL)
        cv2.resizeWindow(window_name, max(320, int(w * 0.6)), max(240, int(h * 0.6)))

    interval = 1.0 / max(args.fps, 1)
    next_tick = time.perf_counter()
    end_at = (time.time() + args.seconds) if args.seconds > 0 else None

    try:
        while True:
            frame = grab_frame()
            if frame is None:
                consecutive_none += 1
                # If dxcam is unstable, fallback on the fly in auto mode
                if use_dxcam and consecutive_none > max(10, args.fps) and _mss is not None and args.backend == "auto":
                    print("dxcam unstable; switching to mss backend", file=sys.stderr)
                    try:
                        camera.stop()
                    except Exception:
                        pass
                    use_dxcam = False
                    sct = _mss.mss()
                    if args.region is None:
                        mon = sct.monitors[min(max(args.monitor + 1, 1), len(sct.monitors) - 1)]
                        mon_rect = {"left": mon["left"], "top": mon["top"], "width": mon["width"], "height": mon["height"]}
                    else:
                        x, y, w, h = args.region
                        mon_rect = {"left": x, "top": y, "width": w, "height": h}
                    # reset None counter and retry immediately
                    consecutive_none = 0
                    continue
                else:
                    continue
            consecutive_none = 0
            if args.scale != 1.0:
                frame = cv2.resize(frame, (w, h), interpolation=cv2.INTER_AREA)
            if writer is not None:
                writer.write(frame)
            if args.preview:
                cv2.imshow(window_name, frame)
                if cv2.waitKey(1) == 27:  # ESC
                    break
            if end_at is not None and time.time() >= end_at:
                break
            # fps pacing
            next_tick += interval
            sleep = next_tick - time.perf_counter()
            if sleep > 0:
                time.sleep(sleep)
            else:
                next_tick = time.perf_counter()
    finally:
        if writer is not None:
            writer.release()
        if args.preview:
            cv2.destroyAllWindows()
        if use_dxcam and camera:
            try:
                camera.stop()
            except Exception:
                pass
        if sct:
            try:
                sct.close()
            except Exception:
                pass
    if args.out:
        print(f"Saved video: {args.out}")
        if args.mark_assessed:
            try:
                marker_path = args.out + (args.marker_ext or ".assessed")
                with open(marker_path, "w", encoding="utf-8") as f:
                    f.write("assessed\n")
                print(f"Marked assessed: {marker_path}")
            except Exception as e:
                print(f"Failed to write marker: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
