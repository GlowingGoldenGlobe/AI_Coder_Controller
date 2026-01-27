from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, MutableMapping, Optional

from ..interfaces import Module, RunContext


def _try_imports() -> tuple[Any, Any, Any]:
    try:
        import numpy as np  # type: ignore
    except Exception:
        np = None

    try:
        from mss import mss  # type: ignore
    except Exception:
        mss = None

    try:
        from PIL import Image  # type: ignore
    except Exception:
        Image = None

    return np, mss, Image


def _stamp() -> int:
    try:
        return int(time.time_ns())
    except Exception:
        return int(time.time() * 1000)


@dataclass
class ScreenshotCaptureModule(Module):
    """Captures a screenshot (full monitor or bbox) to a PNG.

    Config section: ctx.config["capture_screenshot"]
      - enabled: bool (default True)
      - monitor_index: int (default 1)
      - bbox: {left, top, width, height} absolute screen coords (optional)
      - out_dir: output directory (default logs/screens)
      - allow_in_dry_run: bool (default True)

    Output payload keys:
      - screenshot: {image_path, bbox, ok}
    """

    name: str = "capture_screenshot"

    def init(self, ctx: RunContext) -> None:
        return None

    def run_once(self, data: MutableMapping[str, Any], ctx: RunContext) -> Dict[str, Any]:
        cfg = dict(ctx.config.get("capture_screenshot") or {})
        if not bool(cfg.get("enabled", True)):
            return {"status": "skip", "payload": {}, "meta": {"reason": "disabled"}}

        allow_in_dry_run = bool(cfg.get("allow_in_dry_run", True))
        if ctx.dry_run and not allow_in_dry_run:
            return {"status": "skip", "payload": {}, "meta": {"reason": "dry_run_capture_disabled"}}

        np, mss, Image = _try_imports()
        if np is None or mss is None or Image is None:
            return {
                "status": "skip",
                "payload": {},
                "meta": {"reason": "missing_deps", "need": ["numpy", "mss", "pillow"]},
            }

        out_dir = Path(str(cfg.get("out_dir") or "logs/screens"))
        out_dir.mkdir(parents=True, exist_ok=True)

        monitor_index = int(cfg.get("monitor_index", 1))
        bbox_cfg = cfg.get("bbox")

        try:
            with mss() as sct:
                if bbox_cfg:
                    bbox_use = {
                        "left": int(bbox_cfg.get("left", 0)),
                        "top": int(bbox_cfg.get("top", 0)),
                        "width": max(1, int(bbox_cfg.get("width", 1))),
                        "height": max(1, int(bbox_cfg.get("height", 1))),
                    }
                else:
                    mon = sct.monitors[monitor_index] if monitor_index < len(sct.monitors) else sct.monitors[0]
                    bbox_use = {
                        "left": int(mon["left"]),
                        "top": int(mon["top"]),
                        "width": int(mon["width"]),
                        "height": int(mon["height"]),
                    }
                shot = sct.grab(bbox_use)
        except Exception as exc:
            return {
                "status": "error",
                "payload": {"screenshot": {"ok": False, "error": str(exc)}},
                "meta": {},
            }

        arr = np.array(shot)[:, :, :3]
        # mss gives BGRA; convert to RGB for Pillow save.
        img = Image.fromarray(arr[:, :, ::-1])
        path = out_dir / f"screenshot_{_stamp()}.png"
        try:
            img.save(path)
        except Exception as exc:
            return {
                "status": "error",
                "payload": {"screenshot": {"ok": False, "error": f"save_failed: {exc}"}},
                "meta": {},
            }

        payload = {"screenshot": {"ok": True, "image_path": str(path), "bbox": bbox_use}}
        return {"status": "ok", "payload": payload, "meta": {}}

    def shutdown(self, ctx: RunContext) -> None:
        return None
