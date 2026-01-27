from __future__ import annotations

import glob
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, MutableMapping, Optional

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
class BestTemplateMatchModule(Module):
    """Template-matches against many templates and chooses the best hit.

    Expects input from ScreenshotCaptureModule in data["screenshot"].

    Config section: ctx.config["match_best_template"]
      - enabled: bool (default True)
      - templates_dir: directory to scan (default assets/ui_templates)
      - patterns: list[str] glob patterns relative to templates_dir (default ["*.png", "curated/*.png"])
      - threshold: float (default 0.85)
      - max_templates: int (default 200)

    Output payload keys:
      - match: {ok, score, center_x, center_y, bbox, template_path}
    """

    name: str = "match_best_template"

    def init(self, ctx: RunContext) -> None:
        return None

    def run_once(self, data: MutableMapping[str, Any], ctx: RunContext) -> Dict[str, Any]:
        cfg = dict(ctx.config.get("match_best_template") or {})
        if not bool(cfg.get("enabled", True)):
            return {"status": "skip", "payload": {}, "meta": {"reason": "disabled"}}

        shot = data.get("screenshot")
        if not isinstance(shot, dict) or not shot.get("ok"):
            return {"status": "skip", "payload": {}, "meta": {"reason": "no_screenshot"}}

        image_path = shot.get("image_path")
        if not isinstance(image_path, str) or not image_path:
            return {"status": "skip", "payload": {}, "meta": {"reason": "screenshot_path_missing"}}

        templates_dir = Path(str(cfg.get("templates_dir") or "assets/ui_templates"))
        patterns = cfg.get("patterns")
        if not isinstance(patterns, list) or not patterns:
            patterns = ["*.png", "curated/*.png"]

        # Collect candidates (bounded).
        candidates: list[Path] = []
        for pat in patterns:
            for p in glob.glob(str(templates_dir / pat)):
                candidates.append(Path(p))

        # De-dupe and bound.
        uniq: list[Path] = []
        seen: set[str] = set()
        for p in candidates:
            key = str(p).lower()
            if key in seen:
                continue
            seen.add(key)
            uniq.append(p)

        max_templates = int(cfg.get("max_templates", 200))
        if max_templates > 0:
            uniq = uniq[:max_templates]

        if not uniq:
            return {"status": "skip", "payload": {}, "meta": {"reason": "no_templates", "templates_dir": str(templates_dir)}}

        cv2 = _try_import_cv2()
        if cv2 is None:
            return {"status": "skip", "payload": {}, "meta": {"reason": "missing_deps", "need": ["opencv-python"]}}

        screenshot_img = cv2.imread(str(image_path))
        if screenshot_img is None:
            return {"status": "error", "payload": {"match": {"ok": False, "error": "screenshot_read_failed"}}, "meta": {}}

        threshold = float(cfg.get("threshold", 0.85))
        best_score = -1.0
        best_loc = None
        best_tpl = None
        best_shape = None

        for tpl_path in uniq:
            tpl_img = cv2.imread(str(tpl_path))
            if tpl_img is None:
                continue
            try:
                res = cv2.matchTemplate(screenshot_img, tpl_img, cv2.TM_CCOEFF_NORMED)
                _min_val, max_val, _min_loc, max_loc = cv2.minMaxLoc(res)
            except Exception:
                continue
            score = float(max_val)
            if score > best_score:
                best_score = score
                best_loc = (int(max_loc[0]), int(max_loc[1]))
                best_tpl = tpl_path
                best_shape = tpl_img.shape[:2]

        if best_loc is None or best_tpl is None or best_shape is None:
            return {"status": "skip", "payload": {}, "meta": {"reason": "no_readable_templates"}}

        if best_score < threshold:
            return {
                "status": "skip",
                "payload": {"match": {"ok": False, "score": best_score, "threshold": threshold, "template_path": str(best_tpl)}},
                "meta": {"reason": "below_threshold"},
            }

        h, w = int(best_shape[0]), int(best_shape[1])
        x, y = best_loc

        bbox_screen = _load_bbox(data)
        left0 = int(bbox_screen["left"]) if bbox_screen else 0
        top0 = int(bbox_screen["top"]) if bbox_screen else 0

        bbox = {"left": left0 + x, "top": top0 + y, "width": int(w), "height": int(h)}
        center_x = bbox["left"] + bbox["width"] // 2
        center_y = bbox["top"] + bbox["height"] // 2

        payload = {
            "match": {
                "ok": True,
                "score": float(best_score),
                "center_x": center_x,
                "center_y": center_y,
                "bbox": bbox,
                "template_path": str(best_tpl),
            }
        }
        return {"status": "ok", "payload": payload, "meta": {"template_count": len(uniq)}}

    def shutdown(self, ctx: RunContext) -> None:
        return None
