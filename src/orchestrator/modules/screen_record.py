from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, MutableMapping, Optional

from ..errors import ModuleError
from ..interfaces import Module, RunContext


@dataclass
class ScreenRecordModule(Module):
    """Records the primary monitor to an MP4 via existing src.capture.ScreenCapture.

    Safety:
    - In dry_run mode, does not start recording or write files.
    - In live mode, only performs screen capture (no input automation).

    Config keys (ctx.config):
    - root: repo root path (defaults to cwd)
    - out_path: explicit mp4 output path (optional)
    - fps: frames per second (default 10)
    - monitor_index: mss monitor index (default 1)
    """

    name: str = "capture_record"

    _capture: Any = None
    _started: bool = False
    _out_path: Optional[Path] = None

    def init(self, ctx: RunContext) -> None:
        self._capture = None
        self._started = False
        self._out_path = None

        if ctx.dry_run:
            return

        if sys.platform != "win32":
            # mss/cv2 can work elsewhere, but this project is Windows-first.
            raise ModuleError(self.name, code="unsupported_platform", message=f"platform={sys.platform}")

        root = Path(str(ctx.config.get("root", "."))).resolve()
        out_path_cfg = ctx.config.get("out_path")
        if out_path_cfg:
            out_path = Path(str(out_path_cfg))
            if not out_path.is_absolute():
                out_path = root / out_path
        else:
            out_dir = root / "recordings" / "segments"
            out_dir.mkdir(parents=True, exist_ok=True)
            ts = time.strftime("%Y%m%d_%H%M%S")
            out_path = out_dir / f"orchestrator_capture_{ts}.mp4"

        fps = int(ctx.config.get("fps", 10))
        monitor_index = int(ctx.config.get("monitor_index", 1))

        # Import here so that just importing this module stays lightweight.
        from src.capture import ScreenCapture  # type: ignore

        cap = ScreenCapture(out_path=out_path, fps=fps, monitor_index=monitor_index)
        started = cap.start()
        if not started:
            raise ModuleError(
                self.name,
                code="capture_start_failed",
                message="ScreenCapture.start() returned False (missing deps or capture init failure)",
                details={"out_path": str(out_path), "fps": fps, "monitor_index": monitor_index},
            )

        self._capture = cap
        self._started = True
        self._out_path = out_path

    def run_once(self, data: MutableMapping[str, Any], ctx: RunContext) -> Dict[str, Any]:
        if ctx.dry_run:
            return {
                "status": "ok",
                "payload": {"capture": {"mode": "dry_run", "recording": False}},
                "meta": {},
            }

        if not self._started or self._capture is None:
            raise ModuleError(self.name, code="not_initialized", message="capture not started")

        wrote = bool(self._capture.grab_frame())
        return {
            "status": "ok",
            "payload": {"capture": {"mode": "live", "recording": True, "wrote_frame": wrote, "out_path": str(self._out_path)}},
            "meta": {},
        }

    def shutdown(self, ctx: RunContext) -> None:
        try:
            if self._capture is not None:
                self._capture.stop()
        finally:
            self._capture = None
            self._started = False
