import time
from pathlib import Path
from typing import Optional

try:
    import cv2  # type: ignore
    import numpy as np  # type: ignore
    from mss import mss  # type: ignore
except Exception:
    cv2 = None
    np = None
    mss = None


class ScreenCapture:
    def __init__(self, out_path: Path, fps: int = 20, monitor_index: int = 1):
        self.out_path = out_path
        self.fps = fps
        self.monitor_index = monitor_index
        self._sct = None
        self._writer = None
        self._last_frame_t = 0.0

    def start(self) -> bool:
        if cv2 is None or mss is None:
            return False
        try:
            self._sct = mss()
            monitors = self._sct.monitors
            mon = monitors[self.monitor_index] if self.monitor_index < len(monitors) else monitors[0]
            width = mon["width"]
            height = mon["height"]
            fourcc = cv2.VideoWriter_fourcc(*"mp4v")
            self._writer = cv2.VideoWriter(str(self.out_path), fourcc, self.fps, (width, height))
            self._last_frame_t = 0.0
            return True
        except Exception:
            self._sct = None
            self._writer = None
            return False

    def grab_frame(self) -> bool:
        if self._sct is None or self._writer is None or cv2 is None or np is None:
            time.sleep(1.0 / max(self.fps, 1))
            return False
        now = time.time()
        if now - self._last_frame_t < (1.0 / max(self.fps, 1)):
            return False
        self._last_frame_t = now
        try:
            mon = self._sct.monitors[self.monitor_index] if self.monitor_index < len(self._sct.monitors) else self._sct.monitors[0]
            img = self._sct.grab(mon)
            frame = np.array(img)
            frame = cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)
            self._writer.write(frame)
            return True
        except Exception:
            return False

    def stop(self) -> None:
        try:
            if self._writer is not None:
                self._writer.release()
            self._writer = None
            self._sct = None
        except Exception:
            self._writer = None
            self._sct = None


class SegmentedScreenCapture:
    """Write rolling MP4 segments to a directory, rotating every segment_seconds.

    Safe for external cleaners: the actively written segment has a very recent mtime,
    so age-based deletion will skip it.
    """
    def __init__(self, out_dir: Path, fps: int = 20, monitor_index: int = 1, segment_seconds: int = 60):
        self.out_dir = out_dir
        self.fps = fps
        self.monitor_index = monitor_index
        self.segment_seconds = max(5, int(segment_seconds))
        self._sct = None
        self._writer = None
        self._last_frame_t = 0.0
        self._seg_start_t = 0.0
        self._size = None  # type: Optional[tuple]

    def _open_writer(self):
        import cv2  # type: ignore
        self.out_dir.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        name = f"segment_{ts}.mp4"
        path = self.out_dir / name
        w, h = self._size
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        self._writer = cv2.VideoWriter(str(path), fourcc, self.fps, (w, h))
        self._seg_start_t = time.time()
        return path

    def start(self) -> bool:
        if cv2 is None or mss is None:
            return False
        try:
            self._sct = mss()
            monitors = self._sct.monitors
            mon = monitors[self.monitor_index] if self.monitor_index < len(monitors) else monitors[0]
            width = mon["width"]
            height = mon["height"]
            self._size = (width, height)
            self._open_writer()
            self._last_frame_t = 0.0
            return True
        except Exception:
            self._sct = None
            self._writer = None
            return False

    def _rotate_if_needed(self):
        if not self._writer:
            return
        if time.time() - self._seg_start_t >= self.segment_seconds:
            try:
                self._writer.release()
            except Exception:
                pass
            self._writer = None
            self._open_writer()

    def grab_frame(self) -> bool:
        if self._sct is None or self._writer is None or cv2 is None or np is None:
            time.sleep(1.0 / max(self.fps, 1))
            return False
        now = time.time()
        if now - self._last_frame_t < (1.0 / max(self.fps, 1)):
            return False
        self._last_frame_t = now
        try:
            mon = self._sct.monitors[self.monitor_index] if self.monitor_index < len(self._sct.monitors) else self._sct.monitors[0]
            img = self._sct.grab(mon)
            frame = np.array(img)
            frame = cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)
            self._writer.write(frame)
            self._rotate_if_needed()
            return True
        except Exception:
            return False

    def stop(self) -> None:
        try:
            if self._writer is not None:
                self._writer.release()
            self._writer = None
            self._sct = None
        except Exception:
            self._writer = None
            self._sct = None
