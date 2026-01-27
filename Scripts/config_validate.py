from __future__ import annotations
import json
from pathlib import Path
from typing import Any, Dict, List


def _load_json(path: Path, issues: List[str]) -> Dict[str, Any] | None:
    if not path.exists():
        issues.append(f"MISSING: {path}")
        return None
    try:
        txt = path.read_text(encoding="utf-8")
    except Exception as e:
        issues.append(f"ERROR reading {path}: {e}")
        return None
    try:
        return json.loads(txt)
    except Exception as e:
        issues.append(f"ERROR parsing {path} as JSON: {e}")
        return None


def _check_measurement(rules: Dict[str, Any], issues: List[str]) -> None:
    meas = rules.get("measurement") or {}
    if not isinstance(meas, dict):
        issues.append("policy_rules.measurement is not an object (dict)")
        return
    threshold = meas.get("threshold")
    if threshold is not None:
        try:
            t = float(threshold)
            if not (0.0 < t <= 1.0):
                issues.append(f"measurement.threshold should be in (0,1], got {threshold!r}")
        except Exception:
            issues.append(f"measurement.threshold is not numeric: {threshold!r}")
    retry_attempts = meas.get("retry_attempts")
    if retry_attempts is not None and (not isinstance(retry_attempts, int) or retry_attempts < 0):
        issues.append(f"measurement.retry_attempts should be a non-negative int, got {retry_attempts!r}")
    backoff_ms = meas.get("backoff_ms")
    if backoff_ms is not None and (not isinstance(backoff_ms, int) or backoff_ms < 0):
        issues.append(f"measurement.backoff_ms should be a non-negative int, got {backoff_ms!r}")


def _check_templates(root: Path, tmpl_cfg: Dict[str, Any], issues: List[str]) -> None:
    chat = (tmpl_cfg.get("chat_input") or {}) if isinstance(tmpl_cfg, dict) else {}
    if not isinstance(chat, dict):
        issues.append("templates.chat_input is not an object (dict)")
        return
    templates = chat.get("templates")
    if templates is None:
        # optional, but helpful to call out
        issues.append("templates.chat_input.templates is missing (no curated chat templates configured)")
        return
    if not isinstance(templates, list):
        issues.append("templates.chat_input.templates should be a list of relative paths")
        return
    for rel in templates:
        if not isinstance(rel, str):
            issues.append(f"templates.chat_input.templates entry is not a string: {rel!r}")
            continue
        p = root / rel
        if not p.exists():
            issues.append(f"TEMPLATE MISSING on disk: {p}")


def _check_ocr(ocr_cfg: Dict[str, Any], issues: List[str]) -> None:
    if not isinstance(ocr_cfg, dict):
        issues.append("ocr.json root is not an object (dict)")
        return
    region = ocr_cfg.get("region_percent")
    if isinstance(region, dict):
        # region_percent is expressed in percentage units (0-100) and converted
        # to fractions inside CopilotOCR._percent_roi_to_bbox.
        for key in ("left", "top", "width", "height"):
            val = region.get(key)
            if val is None:
                continue
            try:
                f = float(val)
                if not (0.0 <= f <= 100.0):
                    issues.append(f"ocr.region_percent.{key} should be in [0,100], got {val!r}")
            except Exception:
                issues.append(f"ocr.region_percent.{key} is not numeric: {val!r}")


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    issues: List[str] = []

    policy_path = root / "config" / "policy_rules.json"
    ocr_path = root / "config" / "ocr.json"
    tmpl_path = root / "config" / "templates.json"
    orchestrator_path = root / "config" / "vscode_orchestrator.json"

    policy = _load_json(policy_path, issues)
    if policy is not None:
        _check_measurement(policy, issues)

    ocr_cfg = _load_json(ocr_path, issues)
    if ocr_cfg is not None:
        _check_ocr(ocr_cfg, issues)

    tmpl_cfg = _load_json(tmpl_path, issues)
    if tmpl_cfg is not None:
        _check_templates(root, tmpl_cfg, issues)

    _ = _load_json(orchestrator_path, issues)  # basic JSON validity only

    if not issues:
        print("CONFIG OK: core JSON files parsed and basic checks passed.")
        return 0

    print("CONFIG WARNINGS/ERRORS:")
    for msg in issues:
        print(f"- {msg}")
    # Non-zero exit so CI/scripts can detect problems.
    return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
