from __future__ import annotations

import pytest

from src.orchestrator.bbox import clamp_bbox_to_monitor, roi_to_absolute_bbox


def test_roi_to_absolute_bbox_adds_monitor_offset() -> None:
    mon = {"left": 100, "top": 200, "width": 800, "height": 600}
    bbox = roi_to_absolute_bbox((10, 20, 30, 40), mon)
    assert bbox.left == 110
    assert bbox.top == 220
    assert bbox.width == 30
    assert bbox.height == 40


def test_roi_to_absolute_bbox_rejects_empty() -> None:
    mon = {"left": 0, "top": 0, "width": 800, "height": 600}
    with pytest.raises(ValueError):
        roi_to_absolute_bbox((0, 0, 0, 10), mon)


def test_clamp_bbox_to_monitor() -> None:
    mon = {"left": 100, "top": 200, "width": 50, "height": 60}
    bbox = roi_to_absolute_bbox((0, 0, 80, 90), mon)
    clamped = clamp_bbox_to_monitor(bbox, mon)
    assert clamped.left == 100
    assert clamped.top == 200
    assert clamped.width == 50
    assert clamped.height == 60
