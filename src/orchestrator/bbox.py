from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Tuple


@dataclass(frozen=True)
class BBox:
    left: int
    top: int
    width: int
    height: int

    def as_dict(self) -> dict:
        return {"left": self.left, "top": self.top, "width": self.width, "height": self.height}


def roi_to_absolute_bbox(
    roi_xywh: Tuple[int, int, int, int],
    monitor: Mapping[str, int],
) -> BBox:
    """Convert a ROI selected in monitor-relative pixels to an absolute-screen bbox.

    `roi_xywh` is (x, y, w, h) relative to the top-left of the selected monitor image.
    `monitor` is the mss monitor dict with keys left/top/width/height.
    """

    x, y, w, h = roi_xywh

    left = int(monitor.get("left", 0)) + int(x)
    top = int(monitor.get("top", 0)) + int(y)
    width = int(w)
    height = int(h)

    if width <= 0 or height <= 0:
        raise ValueError(f"Invalid ROI size: {roi_xywh}")

    return BBox(left=left, top=top, width=width, height=height)


def clamp_bbox_to_monitor(bbox: BBox, monitor: Mapping[str, int]) -> BBox:
    """Clamp an absolute bbox to the bounds of the monitor (best-effort)."""

    mon_left = int(monitor.get("left", 0))
    mon_top = int(monitor.get("top", 0))
    mon_w = int(monitor.get("width", 0))
    mon_h = int(monitor.get("height", 0))

    if mon_w <= 0 or mon_h <= 0:
        return bbox

    left = max(mon_left, bbox.left)
    top = max(mon_top, bbox.top)
    right = min(mon_left + mon_w, bbox.left + bbox.width)
    bottom = min(mon_top + mon_h, bbox.top + bbox.height)

    width = max(1, right - left)
    height = max(1, bottom - top)

    return BBox(left=left, top=top, width=width, height=height)
