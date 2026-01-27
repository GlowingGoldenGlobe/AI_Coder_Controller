from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, MutableMapping, Optional, Tuple

from ..interfaces import Module, RunContext


def _try_import_cv2() -> Any:
    try:
        import cv2  # type: ignore

        return cv2
    except Exception:
        return None


def _load_bbox(data: MutableMapping[str, Any]) -> Optional[Dict[str, int]]:
    shot = data.get("screenshot")
    if not isinstance(shot, dict):
        return None
    bbox = shot.get("bbox")
    if not isinstance(bbox, dict):
        return None
    try:
        return {
            "left": int(bbox.get("left", 0)),
            "top": int(bbox.get("top", 0)),
            "width": int(bbox.get("width", 0)),
            "height": int(bbox.get("height", 0)),
        }
    except Exception:
        return None


@dataclass
class TemplateMatchModule(Module):
    """Matches a template image within the last captured screenshot using OpenCV.

    Expects input from ScreenshotCaptureModule in data["screenshot"].

    Config section: ctx.config["match_template"]
      - enabled: bool (default True)
      - template_path: path to a PNG in assets/ui_templates (required)
      - threshold: float (default 0.85)

    Output payload keys:
      - match: {ok, score, center_x, center_y, bbox}
    """

    name: str = "match_template"

    def init(self, ctx: RunContext) -> None:
        return None

    def run_once(self, data: MutableMapping[str, Any], ctx: RunContext) -> Dict[str, Any]:
        cfg = dict(ctx.config.get("match_template") or {})
        if not bool(cfg.get("enabled", True)):
            return {"status": "skip", "payload": {}, "meta": {"reason": "disabled"}}

        template_path = str(cfg.get("template_path") or "").strip()
        if not template_path:
            return {"status": "skip", "payload": {}, "meta": {"reason": "template_path_missing"}}

        shot = data.get("screenshot")
        if not isinstance(shot, dict) or not shot.get("ok"):
            return {"status": "skip", "payload": {}, "meta": {"reason": "no_screenshot"}}

        image_path = shot.get("image_path")
        if not isinstance(image_path, str) or not image_path:
            return {"status": "skip", "payload": {}, "meta": {"reason": "screenshot_path_missing"}}

        cv2 = _try_import_cv2()
        if cv2 is None:
            return {"status": "skip", "payload": {}, "meta": {"reason": "missing_deps", "need": ["opencv-python"]}}

        screenshot_img = cv2.imread(str(image_path))
        tpl_img = cv2.imread(str(template_path))
        if screenshot_img is None or tpl_img is None:
            return {"status": "error", "payload": {"match": {"ok": False, "error": "image_read_failed"}}, "meta": {}}

        threshold = float(cfg.get("threshold", 0.85))
        try:
            res = cv2.matchTemplate(screenshot_img, tpl_img, cv2.TM_CCOEFF_NORMED)
            min_val, max_val, min_loc, max_loc = cv2.minMaxLoc(res)
        except Exception as exc:
            return {"status": "error", "payload": {"match": {"ok": False, "error": str(exc)}}, "meta": {}}

        score = float(max_val)
        if score < threshold:
            return {
                "status": "skip",
                "payload": {"match": {"ok": False, "score": score, "threshold": threshold}},
                "meta": {"reason": "below_threshold"},
            }

        h, w = tpl_img.shape[:2]
        x, y = int(max_loc[0]), int(max_loc[1])

        bbox_screen = _load_bbox(data)
        left0 = int(bbox_screen["left"]) if bbox_screen else 0
        top0 = int(bbox_screen["top"]) if bbox_screen else 0

        bbox = {"left": left0 + x, "top": top0 + y, "width": int(w), "height": int(h)}
        center_x = bbox["left"] + bbox["width"] // 2
        center_y = bbox["top"] + bbox["height"] // 2

        payload = {"match": {"ok": True, "score": score, "center_x": center_x, "center_y": center_y, "bbox": bbox}}
        return {"status": "ok", "payload": payload, "meta": {}}

    def shutdown(self, ctx: RunContext) -> None:
        return None
