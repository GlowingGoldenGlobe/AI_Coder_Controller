from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Any, Dict


def _load_json(path: Path) -> Dict[str, Any]:
    try:
        if path.exists():
            obj = json.loads(path.read_text(encoding="utf-8"))
            return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}
    return {}


def _as_bool(v: Any, default: bool = False) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    s = str(v or "").strip().lower()
    if s in {"1", "true", "yes", "y", "on"}:
        return True
    if s in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _agent_mode_active(root: Path) -> tuple[bool, str]:
    v = os.environ.get("AI_CONTROLLER_AGENT_MODE")
    if v is not None:
        return (_as_bool(v, False), "env:AI_CONTROLLER_AGENT_MODE")
    ui = _load_json(root / "config" / "ui_state.json")
    return (bool(ui.get("agent_mode", False)), "config:config/ui_state.json")


def _summarize_deferred_queue(root: Path, rel: str = "logs/actions/deferred_workflow_actions.jsonl") -> Dict[str, Any]:
    q = (root / rel).resolve()
    if not q.exists():
        return {"exists": False, "path": str(q), "total_lines": 0, "parsed": 0, "unique_actions": 0}

    lines = q.read_text(encoding="utf-8", errors="ignore").splitlines()
    parsed = []
    for line in lines:
        s = (line or "").strip()
        if not s:
            continue
        try:
            obj = json.loads(s)
        except Exception:
            continue
        if isinstance(obj, dict):
            parsed.append(obj)

    counts: Dict[str, int] = {}
    for a in parsed:
        try:
            action_id = str(a.get("id") or "").strip()
            if not action_id:
                continue
            counts[action_id] = counts.get(action_id, 0) + 1
        except Exception:
            continue

    return {
        "exists": True,
        "path": str(q),
        "total_lines": int(len(lines)),
        "parsed": int(len(parsed)),
        "unique_actions": int(len(counts)),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Workflow preflight: inspect effective settings before running workflow actions.")
    ap.add_argument("--out", type=str, default="", help="Optional output path (JSON). If omitted, only prints.")
    ap.add_argument("--stale-seconds", type=float, default=0.0, help="Optional staleness threshold override (0 uses policy_rules.json).")
    args = ap.parse_args()

    root = Path(__file__).resolve().parent.parent
    rules = _load_json(root / "config" / "policy_rules.json")
    workflow_cfg = (rules.get("workflow") or {}) if isinstance(rules, dict) else {}
    controls_cfg = (rules.get("controls") or {}) if isinstance(rules, dict) else {}

    agent_mode, agent_mode_src = _agent_mode_active(root)

    defer_interactions = bool(workflow_cfg.get("defer_interactions_when_agent_mode", True))
    env_defer = os.environ.get("AI_CONTROLLER_DEFER_INTERACTIONS_WHEN_AGENT_MODE")
    if env_defer is not None:
        defer_interactions = _as_bool(env_defer, defer_interactions)

    stale_after_s = float(controls_cfg.get("stale_after_s", 10.0) or 10.0)
    if args.stale_seconds and args.stale_seconds > 0:
        stale_after_s = float(args.stale_seconds)

    # Controls state
    try:
        from src.control_state import get_controls_state, is_state_stale  # type: ignore

        st = get_controls_state(root) or {}
        stale = bool(is_state_stale(st, stale_after_s)) if stale_after_s > 0 else False
    except Exception:
        st = _load_json(root / "config" / "controls_state.json")
        stale = False

    paused = bool((st or {}).get("paused", False)) if isinstance(st, dict) else False
    owner = str((st or {}).get("owner", "") or "") if isinstance(st, dict) else ""

    warnings = []
    suggestions = []

    if paused and not stale:
        warnings.append("controls_state.paused=true (live UI automation blocked)")
        suggestions.append("If you want live automation: run task 'Controls: Unpause (paused=false)'.")
    if owner and owner not in {"", "workflow_test"} and not stale:
        warnings.append(f"controls_state.owner={owner!r} (controls owned by another workflow)")
        suggestions.append("Wait for release; if truly stale, use task 'Release controls owner (force)'.")
    if (not paused) and (not owner):
        warnings.append("controls are unpaused + unowned (live automation possible; consider pausing when idle)")
        suggestions.append("After finishing live work: run task 'Controls: Pause (paused=true)'.")

    if agent_mode and defer_interactions:
        warnings.append("Agent Mode ON + defer enabled (interactive workflow actions will be queued as DEFERRED)")
        suggestions.append("For full interactive coverage: set agent_mode=false or run with AI_CONTROLLER_AGENT_MODE=0.")

    dq = _summarize_deferred_queue(root)

    snap = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "agent_mode_active": bool(agent_mode),
        "agent_mode_source": agent_mode_src,
        "defer_interactions_when_agent_mode": bool(defer_interactions),
        "controls_stale_after_s": float(stale_after_s),
        "controls_state_stale": bool(stale),
        "controls_state": st,
        "deferred_queue": dq,
        "warnings": warnings,
        "suggestions": suggestions,
    }

    print(json.dumps(snap, indent=2, ensure_ascii=False))

    if args.out:
        out_path = (root / args.out).resolve() if not Path(args.out).is_absolute() else Path(args.out).resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(snap, indent=2, ensure_ascii=False), encoding="utf-8")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
