from __future__ import annotations
import time
from pathlib import Path
from src.capture import SegmentedScreenCapture


def main():
    root = Path(__file__).resolve().parent.parent
    out_dir = root / "recordings" / "segments"
    cap = SegmentedScreenCapture(out_dir, fps=5, monitor_index=1, segment_seconds=5)
    if not cap.start():
        print("Failed to start segmented capture (cv2/mss missing?)")
        return
    t0 = time.time()
    while time.time() - t0 < 12:
        cap.grab_frame()
        time.sleep(0.05)
    cap.stop()
    # List created segments
    if out_dir.exists():
        files = sorted([p.name for p in out_dir.glob("segment_*.mp4")])
        print("Segments:", files)
    else:
        print("Segments dir not found")


if __name__ == "__main__":
    main()
