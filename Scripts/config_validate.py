from __future__ import annotations
import json
from pathlib import Path
from typing import Any, Dict, List


def _is_str_list(x: Any) -> bool:
    return isinstance(x, list) and all(isinstance(it, str) for it in x)


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


def _check_vscode_orchestrator(cfg: Dict[str, Any], issues: List[str]) -> None:
    if not isinstance(cfg, dict):
        issues.append("vscode_orchestrator.json root is not an object (dict)")
        return

    action_hints = cfg.get("action_hints")
    if action_hints is not None and not _is_str_list(action_hints):
        issues.append("vscode_orchestrator.action_hints should be a list[str]")

    msg = cfg.get("message")
    if msg is not None and not isinstance(msg, dict):
        issues.append("vscode_orchestrator.message should be an object (dict)")
        return

    if isinstance(msg, dict):
        templates = msg.get("default_templates")
        if templates is not None:
            if not isinstance(templates, list):
                issues.append("vscode_orchestrator.message.default_templates should be a list")
            else:
                for i, t in enumerate(templates):
                    if not isinstance(t, dict):
                        issues.append(f"vscode_orchestrator.message.default_templates[{i}] is not an object")
                        continue
                    tid = t.get("id")
                    text = t.get("text")
                    if not isinstance(tid, str) or not tid.strip():
                        issues.append(f"vscode_orchestrator.message.default_templates[{i}].id is missing/empty")
                    if not isinstance(text, str) or not text.strip():
                        issues.append(f"vscode_orchestrator.message.default_templates[{i}].text is missing/empty")

        send_keys = msg.get("send_keys")
        if send_keys is not None and not _is_str_list(send_keys):
            issues.append("vscode_orchestrator.message.send_keys should be a list[str]")


def _check_orchestrator_pipelines(root: Path, issues: List[str]) -> None:
    """Validate config/orchestrator_pipeline*.json against the default registry."""
    try:
        from src.orchestrator.default_registry import build_default_registry
    except Exception as e:
        issues.append(f"Cannot import orchestrator default registry: {e}")
        return

    reg = build_default_registry()
    cfg_dir = root / "config"
    for path in sorted(cfg_dir.glob("orchestrator_pipeline*.json")):
        cfg = _load_json(path, issues)
        if cfg is None:
            continue
        if not isinstance(cfg, dict):
            issues.append(f"Pipeline config is not an object (dict): {path}")
            continue

        pipeline = cfg.get("pipeline")
        if not _is_str_list(pipeline):
            issues.append(f"Pipeline config missing/invalid 'pipeline' list[str]: {path}")
            continue

        # Ensure registry knows all modules.
        for name in pipeline:
            try:
                reg.create(str(name))
            except Exception as e:
                issues.append(f"Unknown pipeline module {name!r} in {path.name}: {e}")

        # Best-effort file checks for known module configs.
        mt = cfg.get("match_template")
        if isinstance(mt, dict):
            tpath = mt.get("template_path")
            if isinstance(tpath, str) and tpath.strip():
                p = root / tpath
                if not p.exists():
                    issues.append(f"match_template.template_path missing on disk: {p} (from {path.name})")

        mb = cfg.get("match_best_template")
        if isinstance(mb, dict):
            tdir = mb.get("templates_dir")
            if isinstance(tdir, str) and tdir.strip():
                pdir = root / tdir
                if not pdir.exists():
                    issues.append(f"match_best_template.templates_dir missing on disk: {pdir} (from {path.name})")


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

    orchestrator_cfg = _load_json(orchestrator_path, issues)
    if orchestrator_cfg is not None:
        _check_vscode_orchestrator(orchestrator_cfg, issues)

    _check_orchestrator_pipelines(root, issues)

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
