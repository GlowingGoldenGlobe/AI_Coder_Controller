from __future__ import annotations

import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, MutableMapping, Optional

from ..interfaces import Module, RunContext


def _try_imports() -> tuple[Any, Any, Any, Any]:
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

    try:
        import cv2  # type: ignore
    except Exception:
        cv2 = None

    return np, mss, Image, cv2


def _stamp() -> int:
    try:
        return int(time.time_ns())
    except Exception:
        return int(time.time() * 1000)


def _get_bbox_from_data(data: MutableMapping[str, Any]) -> Optional[Dict[str, int]]:
    shot = data.get("screenshot")
    if not isinstance(shot, dict) or not shot.get("ok"):
        return None
    bbox = shot.get("bbox")
    if not isinstance(bbox, dict):
        return None
    try:
        return {
            "left": int(bbox.get("left", 0)),
            "top": int(bbox.get("top", 0)),
            "width": max(1, int(bbox.get("width", 1))),
            "height": max(1, int(bbox.get("height", 1))),
        }
    except Exception:
        return None


def _get_match_from_data(data: MutableMapping[str, Any]) -> Optional[Dict[str, Any]]:
    m = data.get("match")
    if not isinstance(m, dict) or not m.get("ok"):
        return None
    return m


def _get_click_from_data(data: MutableMapping[str, Any]) -> Optional[Dict[str, Any]]:
    c = data.get("click")
    if not isinstance(c, dict):
        return None
    return c


def _distance(a: tuple[int, int], b: tuple[int, int]) -> float:
    return math.hypot(float(a[0] - b[0]), float(a[1] - b[1]))


@dataclass
class VerifyAfterClickModule(Module):
    """Re-captures a screenshot after a click and verifies UI changed.

    Intended to prevent silent failures in live automation.

    Inputs:
    - data["screenshot"] from capture_screenshot
    - data["match"] from match_template or match_best_template
    - data["click"] from act_click

    Config section: ctx.config["verify_after_click"]
      - enabled: bool (default True)
      - delay_ms: int (default 350)
      - out_dir: str (default logs/screens)
      - disappear_threshold: float (default 0.75)  # pass if post-click score drops below this
      - min_move_px: int (default 6)               # or if center moves by >= this

    Behavior:
    - In dry_run: skips by default.
    - In live: if a click happened and we can recapture + match, fails closed when evidence is weak.

    Output payload keys:
      - verify: {ok, rule, before, after}
    """

    name: str = "verify_after_click"

    def init(self, ctx: RunContext) -> None:
        return None

    def run_once(self, data: MutableMapping[str, Any], ctx: RunContext) -> Dict[str, Any]:
        cfg = dict(ctx.config.get("verify_after_click") or {})
        if not bool(cfg.get("enabled", True)):
            return {"status": "skip", "payload": {}, "meta": {"reason": "disabled"}}

        # Skip in dry-run (click is not real, and this adds latency).
        if ctx.dry_run:
            return {"status": "skip", "payload": {}, "meta": {"reason": "dry_run"}}

        click = _get_click_from_data(data)
        if not click or not bool(click.get("ok", False)):
            return {"status": "skip", "payload": {}, "meta": {"reason": "no_live_click"}}

        match = _get_match_from_data(data)
        if match is None:
            return {"status": "skip", "payload": {}, "meta": {"reason": "no_match"}}

        bbox_use = _get_bbox_from_data(data)
        if bbox_use is None:
            return {"status": "skip", "payload": {}, "meta": {"reason": "no_bbox"}}

        template_path = str(match.get("template_path") or cfg.get("template_path") or "").strip()
        if not template_path:
            return {"status": "skip", "payload": {}, "meta": {"reason": "template_path_missing"}}

        delay_ms = int(cfg.get("delay_ms", 350))
        if delay_ms > 0:
            time.sleep(delay_ms / 1000.0)

        np, mss, Image, cv2 = _try_imports()
        if np is None or mss is None or Image is None or cv2 is None:
            return {
                "status": "error",
                "payload": {"verify": {"ok": False, "error": "missing_deps", "need": ["numpy", "mss", "pillow", "opencv-python"]}},
                "meta": {},
            }

        out_dir = Path(str(cfg.get("out_dir") or "logs/screens"))
        out_dir.mkdir(parents=True, exist_ok=True)

        # Recapture the same bbox.
        try:
            with mss() as sct:
                shot = sct.grab(bbox_use)
        except Exception as exc:
            return {"status": "error", "payload": {"verify": {"ok": False, "error": f"recapture_failed: {exc}"}}, "meta": {}}

        arr = np.array(shot)[:, :, :3]
        img = Image.fromarray(arr[:, :, ::-1])
        after_path = out_dir / f"verify_after_click_{_stamp()}.png"
        try:
            img.save(after_path)
        except Exception as exc:
            return {"status": "error", "payload": {"verify": {"ok": False, "error": f"save_failed: {exc}"}}, "meta": {}}

        # Match again on the recaptured image.
        after_img = cv2.imread(str(after_path))
        tpl_img = cv2.imread(str(template_path))
        if after_img is None or tpl_img is None:
            return {"status": "error", "payload": {"verify": {"ok": False, "error": "image_read_failed"}}, "meta": {}}

        try:
            res = cv2.matchTemplate(after_img, tpl_img, cv2.TM_CCOEFF_NORMED)
            _min_val, max_val, _min_loc, max_loc = cv2.minMaxLoc(res)
        except Exception as exc:
            return {"status": "error", "payload": {"verify": {"ok": False, "error": str(exc)}}, "meta": {}}

        after_score = float(max_val)
        h, w = tpl_img.shape[:2]
        after_bbox = {
            "left": int(bbox_use["left"]) + int(max_loc[0]),
            "top": int(bbox_use["top"]) + int(max_loc[1]),
            "width": int(w),
            "height": int(h),
        }
        after_center = (after_bbox["left"] + after_bbox["width"] // 2, after_bbox["top"] + after_bbox["height"] // 2)

        before_center = (int(match.get("center_x")), int(match.get("center_y")))
        before_score = float(match.get("score", 0.0))

        disappear_threshold = float(cfg.get("disappear_threshold", 0.75))
        min_move_px = int(cfg.get("min_move_px", 6))

        moved_px = _distance(before_center, after_center)
        passed_by_disappear = after_score < disappear_threshold
        passed_by_move = moved_px >= float(min_move_px)

        ok = bool(passed_by_disappear or passed_by_move)
        rule = "disappear" if passed_by_disappear else ("move" if passed_by_move else "none")

        payload = {
            "verify": {
                "ok": ok,
                "rule": rule,
                "before": {
                    "score": before_score,
                    "center": {"x": before_center[0], "y": before_center[1]},
                    "template_path": template_path,
                },
                "after": {
                    "score": after_score,
                    "center": {"x": after_center[0], "y": after_center[1]},
                    "bbox": after_bbox,
                    "image_path": str(after_path),
                },
                "moved_px": moved_px,
            }
        }

        if not ok:
            return {"status": "error", "payload": payload, "meta": {"reason": "verification_failed"}}

        return {"status": "ok", "payload": payload, "meta": {}}

    def shutdown(self, ctx: RunContext) -> None:
        return None
