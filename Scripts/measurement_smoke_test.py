from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List

from src.ocr import CopilotOCR

try:
    import cv2  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    cv2 = None  # type: ignore


def template_ready(image_path: Path, template_path: Path, threshold: float) -> bool:
    if cv2 is None:
        return False
    if (not image_path) or (not template_path.exists()) or (not image_path.exists()):
        return False
    try:
        img = cv2.imread(str(image_path), cv2.IMREAD_GRAYSCALE)
        tpl = cv2.imread(str(template_path), cv2.IMREAD_GRAYSCALE)
        if img is None or tpl is None:
            return False
        res = cv2.matchTemplate(img, tpl, cv2.TM_CCOEFF_NORMED)
        _min_val, max_val, _min_loc, _max_loc = cv2.minMaxLoc(res)
        return bool(max_val >= float(threshold))
    except Exception:
        return False


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    cfg_dir = root / "config"
    logs_dir = root / "logs" / "tests"
    logs_dir.mkdir(parents=True, exist_ok=True)

    ocr_cfg_path = cfg_dir / "ocr.json"
    policy_path = cfg_dir / "policy_rules.json"
    templates_path = cfg_dir / "templates.json"

    try:
        ocr_cfg = json.loads(ocr_cfg_path.read_text(encoding="utf-8")) if ocr_cfg_path.exists() else {"enabled": True}
    except Exception:
        ocr_cfg = {"enabled": True}

    try:
        rules = json.loads(policy_path.read_text(encoding="utf-8")) if policy_path.exists() else {}
    except Exception:
        rules = {}

    meas_cfg = (rules.get("measurement") or {}) if isinstance(rules, dict) else {}
    threshold = float(meas_cfg.get("threshold", 0.85))

    templates_cfg: Dict[str, Any] = {}
    try:
        if templates_path.exists():
            templates_cfg = json.loads(templates_path.read_text(encoding="utf-8")) or {}
    except Exception:
        templates_cfg = {}

    chat_templates: List[Path] = []
    try:
        rels = (templates_cfg.get("chat_input", {}) or {}).get("templates", []) or []
        for rel in rels:
            try:
                p = (root / str(rel)).resolve()
                if p.exists():
                    chat_templates.append(p)
            except Exception:
                continue
    except Exception:
        chat_templates = []

    ocr_debug = root / "logs" / "ocr"
    ocr_debug.mkdir(parents=True, exist_ok=True)
    ocr = CopilotOCR(ocr_cfg, log=lambda m: None, debug_dir=ocr_debug)

    # Single capture using the generic chat capture helper.
    try:
        res = ocr.capture_chat_text(save_dir=ocr_debug)
    except Exception as e:
        out = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "ok": False,
            "error": f"capture_failed:{e}",
        }
        out_path = logs_dir / f"measurement_smoke_{time.strftime('%Y%m%d_%H%M%S')}.json"
        out_path.write_text(json.dumps(out, indent=2), encoding="utf-8")
        return 1

    img_path = Path(str(res.get("image_path") or "")) if isinstance(res, dict) else None
    elems = (res.get("elements") or []) if isinstance(res, dict) else []

    matches: List[Dict[str, Any]] = []
    if img_path and img_path.exists() and chat_templates:
        for tpl in chat_templates:
            ok = template_ready(img_path, tpl, threshold=threshold)
            matches.append({
                "template": str(tpl.relative_to(root)),
                "matched": bool(ok),
            })

    out = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "ok": bool(res.get("ok", False)) if isinstance(res, dict) else False,
        "image_path": str(img_path) if img_path else "",
        "elements": len(elems),
        "threshold": threshold,
        "templates": matches,
    }

    out_path = logs_dir / f"measurement_smoke_{time.strftime('%Y%m%d_%H%M%S')}.json"
    out_path.write_text(json.dumps(out, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
