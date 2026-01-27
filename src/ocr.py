import time
from pathlib import Path
from typing import Optional, Tuple, Dict, Any, List
import os
import glob

import numpy as np
from mss import mss

try:
    from PIL import Image, ImageOps, ImageFilter
except Exception:
    Image = None  # type: ignore
    ImageOps = None  # type: ignore
    ImageFilter = None  # type: ignore

try:
    import cv2
except Exception:
    cv2 = None

try:
    import pytesseract  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    pytesseract = None


# Backwards-compatible alias for existing callers/imports using "CopilotOCR".


class ImageAnalyzer:
    """Screen capture + image analysis (templates/elements) with optional OCR.

    Behaviour:
    - Captures a configured screen ROI or provided bbox as PNG images (one per observation).
    - Always runs a lightweight image-analysis pass (``detect_ui_elements`` and templates)
      that returns candidate button/control bounding boxes in ``elements``.
    - Optionally runs Tesseract via ``pytesseract`` to populate a ``text`` field when
      the dependency and binary are available.

    Callers that only care about images/elements can ignore ``text``. Existing
    scripts that expect OCR text (e.g. verification helpers) can rely on
    ``text`` when Tesseract is installed and configured.
    """

    def __init__(self, cfg: Dict[str, Any], log=print, debug_dir: Optional[Path] = None):
        self.cfg = cfg or {}
        self.log = log
        self.enabled = bool(self.cfg.get("enabled", True))
        self.monitor_index = int(self.cfg.get("monitor_index", 1))
        self.region_percent = self.cfg.get(
            "region_percent",
            {"left": 65, "top": 8, "width": 34, "height": 88},
        )
        self.save_debug = bool(self.cfg.get("save_debug_images", True))
        self.debug_dir = debug_dir

        # Optional Tesseract wiring (best-effort; safe to run without it).
        try:
            if pytesseract is not None:
                cmd = str(self.cfg.get("tesseract_cmd") or "").strip()
                if cmd:
                    pytesseract.pytesseract.tesseract_cmd = cmd
        except Exception:
            # Misconfiguration should not crash the controller; it will simply
            # result in ``text`` being empty.
            pass
        # template cache (name -> {'img': np.ndarray, 'shape': (h,w)})
        self._template_cache: Optional[Dict[str, Dict[str, Any]]] = None

    def _percent_roi_to_bbox(self, screen_w: int, screen_h: int) -> Tuple[int, int, int, int]:
        lp = float(self.region_percent.get("left", 65)) / 100.0
        tp = float(self.region_percent.get("top", 0)) / 100.0
        wp = float(self.region_percent.get("width", 35)) / 100.0
        hp = float(self.region_percent.get("height", 100)) / 100.0
        left = int(screen_w * lp)
        top = int(screen_h * tp)
        width = max(1, int(screen_w * wp))
        height = max(1, int(screen_w * hp))
        return left, top, width, height

    def _stamp(self) -> int:
        try:
            return int(time.time_ns())
        except Exception:
            return int(time.time() * 1000)

    def _save_image(self, arr: np.ndarray, save_dir: Optional[Path], tag: str) -> Optional[Path]:
        if Image is None:
            return None
        try:
            img = Image.fromarray(arr[:, :, ::-1])  # BGR->RGB if needed
            ts = self._stamp()
            ddir = save_dir or self.debug_dir
            if ddir is None:
                return None
            ddir.mkdir(parents=True, exist_ok=True)
            p = ddir / f"capture_{tag}_{ts}.png"
            img.save(p)
            return p
        except Exception:
            return None

    def capture_image(self, save_dir: Optional[Path] = None, bbox: Optional[Dict[str, int]] = None, tag: str = "screen") -> Dict[str, Any]:
        """Capture a full ROI (configured) or a provided absolute bbox.

        Returns a dict with keys:
        - ``ok`` (bool)
        - ``text`` (str): OCR text when available, else empty string
        - ``image_path`` (str | None)
        - ``elements`` (list): detected UI element descriptors
        """
        if not getattr(self, "enabled", True):
            return {"ok": False, "text": "", "error": "disabled", "image_path": None, "elements": []}
        try:
            with mss() as sct:
                if bbox is None:
                    mon = sct.monitors[self.monitor_index]
                    sw, sh = mon["width"], mon["height"]
                    left, top, width, height = self._percent_roi_to_bbox(sw, sh)
                    bbox_use = {"left": mon["left"] + left, "top": mon["top"] + top, "width": width, "height": height}
                else:
                    bbox_use = {"left": int(bbox.get("left", 0)), "top": int(bbox.get("top", 0)), "width": max(1, int(bbox.get("width", 1))), "height": max(1, int(bbox.get("height", 1)))}
                shot = sct.grab(bbox_use)
        except Exception as e:
            return {"ok": False, "text": "", "error": f"capture failed: {e}", "image_path": None, "elements": []}

        arr = np.array(shot)[:, :, :3]
        # mss returns BGRA on some platforms; keep raw RGB-like ordering
        img_path = None
        if self.save_debug:
            img_path = self._save_image(arr, save_dir, tag)

        # Optional text OCR (best-effort).
        text = ""
        if pytesseract is not None and Image is not None:
            try:
                # Use the in-memory array to avoid re-reading from disk.
                img = Image.fromarray(arr[:, :, ::-1])  # BGR -> RGB
                psm = None
                try:
                    psm = int(self.cfg.get("tesseract_psm")) if self.cfg.get("tesseract_psm") is not None else None
                except Exception:
                    psm = None
                config = f"--psm {psm}" if psm is not None else ""
                text = pytesseract.image_to_string(img, config=config) or ""
            except Exception:
                text = ""

        elements: List[Dict[str, Any]] = []
        try:
            if img_path:
                elements = self.detect_ui_elements_from_path(img_path)
        except Exception:
            elements = []

        return {"ok": True, "text": text or "", "error": None, "image_path": img_path, "elements": elements}

    def capture_chat_text(self, save_dir: Optional[Path] = None) -> Dict[str, Any]:
        # Kept name for compatibility; now returns image and element detections instead of pure text
        return self.capture_image(save_dir=save_dir, bbox=None, tag="copilot_chat")

    def capture_bbox_text(self, bbox: Dict[str, int], save_dir: Optional[Path] = None, tag: str = "bbox", preprocess_mode: str = "default") -> Dict[str, Any]:
        # Kept name for compatibility; returns image and element detections for the bbox
        return self.capture_image(save_dir=save_dir, bbox=bbox, tag=tag)

    def detect_ui_elements_from_path(self, image_path: Path) -> List[Dict[str, Any]]:
        """Detect rectangular UI elements (buttons/controls) in the image.

        Returns list of {'type':'button','bbox':{'left','top','width','height'}, 'score':float}
        """
        try:
            if cv2 is None:
                # Fallback: simple threshold-based bounding boxes via Pillow->numpy
                from PIL import Image

                img = Image.open(image_path).convert("L")
                arr = np.array(img)
                # adaptive-ish threshold
                th = max(10, int(arr.mean() * 1.1))
                bw = (arr < th).astype(np.uint8) * 255
                # find connected components
                from scipy import ndimage  # type: ignore

                labeled, n = ndimage.label(bw)
                objects = ndimage.find_objects(labeled)
                out: List[Dict[str, Any]] = []
                for o in objects:
                    if not o:
                        continue
                    y0, y1 = int(o[0].start), int(o[0].stop)
                    x0, x1 = int(o[1].start), int(o[1].stop)
                    w = x1 - x0
                    h = y1 - y0
                    if w < 8 or h < 8:
                        continue
                    score = float(w * h)
                    out.append({"type": "button", "bbox": {"left": x0, "top": y0, "width": w, "height": h}, "score": score})
                return out
            # Use OpenCV path
            img = cv2.imread(str(image_path))
            if img is None:
                return []
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            # Blur and Canny to find edges
            blur = cv2.GaussianBlur(gray, (5, 5), 0)
            edges = cv2.Canny(blur, 50, 150)
            # Dilate to close gaps
            kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))
            closed = cv2.dilate(edges, kernel, iterations=1)
            contours, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            h_img, w_img = gray.shape[:2]
            results: List[Dict[str, Any]] = []
            for cnt in contours:
                x, y, w, h = cv2.boundingRect(cnt)
                if w < 8 or h < 8:
                    continue
                # filter full-image boxes
                if w > 0.9 * w_img and h > 0.9 * h_img:
                    continue
                area = w * h
                aspect = float(w) / float(h) if h else 0.0
                # heuristics for button-like shapes: moderate area and aspect ratio not extremely tall
                if area < 40 or area > (w_img * h_img * 0.9):
                    continue
                # mark likely buttons
                results.append({"type": "button", "bbox": {"left": int(x), "top": int(y), "width": int(w), "height": int(h)}, "score": float(area)})
            # sort by score desc
            # Run template matching (optional) to detect known UI icons/buttons
            try:
                templates = self._load_templates()
                th = float(self.cfg.get("template_match_threshold", 0.8))
                if templates and cv2 is not None:
                    for name, tpl in templates.items():
                        try:
                            res = cv2.matchTemplate(gray, tpl["img"], cv2.TM_CCOEFF_NORMED)
                            locs = np.where(res >= th)
                            # record matches (limit duplicates)
                            for (y, x) in zip(locs[0].tolist(), locs[1].tolist()):
                                h_t, w_t = tpl["shape"]
                                score = float(res[y, x])
                                # skip tiny or out-of-bounds
                                if w_t < 4 or h_t < 4:
                                    continue
                                results.append({"type": f"template:{name}", "bbox": {"left": int(x), "top": int(y), "width": int(w_t), "height": int(h_t)}, "score": score})
                        except Exception:
                            continue
            except Exception:
                pass

            # final sort including template hits
            # final sort including template hits
            results.sort(key=lambda r: r.get("score", 0), reverse=True)

            # --- Non-max suppression and merging ---
            def _iou(a: Dict[str, int], b: Dict[str, int]) -> float:
                ax1, ay1 = a["left"], a["top"]
                ax2, ay2 = ax1 + a["width"], ay1 + a["height"]
                bx1, by1 = b["left"], b["top"]
                bx2, by2 = bx1 + b["width"], by1 + b["height"]
                ix1, iy1 = max(ax1, bx1), max(ay1, by1)
                ix2, iy2 = min(ax2, bx2), min(ay2, by2)
                iw = max(0, ix2 - ix1)
                ih = max(0, iy2 - iy1)
                inter = iw * ih
                union = (a["width"] * a["height"]) + (b["width"] * b["height"]) - inter
                return float(inter) / float(union) if union > 0 else 0.0

            nms_iou = float(self.cfg.get("nms_iou", 0.3))
            template_contour_iou = float(self.cfg.get("template_contour_iou", 0.5))

            template_hits = [r for r in results if isinstance(r.get("type"), str) and r.get("type").startswith("template:")]
            contour_hits = [r for r in results if not (isinstance(r.get("type"), str) and r.get("type").startswith("template:"))]

            # NMS for templates (keep highest scored, suppress overlapping)
            template_hits_sorted = sorted(template_hits, key=lambda x: x.get("score", 0), reverse=True)
            kept_templates: List[Dict[str, Any]] = []
            for cand in template_hits_sorted:
                bb = cand["bbox"]
                skip = False
                for kept in kept_templates:
                    if _iou(bb, kept["bbox"]) > nms_iou:
                        skip = True
                        break
                if not skip:
                    kept_templates.append(cand)

            # remove contour hits that overlap kept templates heavily
            filtered_contours: List[Dict[str, Any]] = []
            for c in contour_hits:
                bbc = c["bbox"]
                overlaps = False
                for t in kept_templates:
                    if _iou(bbc, t["bbox"]) > template_contour_iou:
                        overlaps = True
                        break
                if not overlaps:
                    filtered_contours.append(c)

            # Optionally run NMS on remaining contours to reduce duplicates
            contour_nms_iou = float(self.cfg.get("contour_nms_iou", nms_iou))
            contour_sorted = sorted(filtered_contours, key=lambda x: x.get("score", 0), reverse=True)
            kept_contours: List[Dict[str, Any]] = []
            for cand in contour_sorted:
                bb = cand["bbox"]
                skip = False
                for kept in kept_contours:
                    if _iou(bb, kept["bbox"]) > contour_nms_iou:
                        skip = True
                        break
                if not skip:
                    kept_contours.append(cand)

            final = kept_templates + kept_contours
            final.sort(key=lambda r: r.get("score", 0), reverse=True)
            return final
        except Exception:
            return []

    def _load_templates(self) -> Dict[str, Dict[str, Any]]:
        """Load grayscale template PNGs from configured templates directory.

        Returns mapping name -> {'img': np.ndarray, 'shape': (h,w)}.
        """
        if self._template_cache is not None:
            return self._template_cache
        out: Dict[str, Dict[str, Any]] = {}
        try:
            td = str(self.cfg.get("templates_dir") or "assets/ui_templates")
            td = os.path.expanduser(td)
            p = Path(td)
            if not p.exists():
                self._template_cache = out
                return out
            pattern = str(p / "*.png")
            for tpl_path in glob.glob(pattern):
                try:
                    name = Path(tpl_path).stem
                    if cv2 is None:
                        continue
                    img = cv2.imread(tpl_path, cv2.IMREAD_GRAYSCALE)
                    if img is None:
                        continue
                    h, w = img.shape[:2]
                    out[name] = {"img": img, "shape": (h, w)}
                except Exception:
                    continue
        except Exception:
            pass
        self._template_cache = out
        return out


# Backwards-compatible alias
CopilotOCR = ImageAnalyzer
