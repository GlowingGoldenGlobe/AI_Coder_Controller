from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
import subprocess


def _root() -> Path:
    return Path(__file__).resolve().parent.parent


def _load_json(path: Path) -> dict:
    try:
        if path.exists():
            obj = json.loads(path.read_text(encoding="utf-8"))
            return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}
    return {}


def _is_stale(root: Path, st: dict) -> bool:
    try:
        from src.control_state import is_state_stale  # type: ignore
    except Exception:
        return False


def _auto_unpause_allowed(root: Path, st: dict) -> bool:
    try:
        rules = _load_json(root / "config" / "policy_rules.json")
        controls_cfg = (rules.get("controls") or {}) if isinstance(rules, dict) else {}
        enabled = bool(controls_cfg.get("auto_unpause_when_idle", True))
        allow = controls_cfg.get("auto_unpause_owner_allowlist")
        if not isinstance(allow, list):
            allow = ["", "agent", "workflow_test", "orchestrator", "orchestrator_agent"]
    except Exception:
        enabled = True
        allow = ["", "agent", "workflow_test", "orchestrator", "orchestrator_agent"]

    if not enabled:
        return False
    if not isinstance(st, dict):
        return False
    if not bool(st.get("paused", False)):
        return False
    if bool(st.get("in_control_window", False)):
        return False
    owner = str(st.get("owner", "") or "")
    return owner in allow
    try:
        rules = _load_json(root / "config" / "policy_rules.json")
        controls_cfg = (rules.get("controls") or {}) if isinstance(rules, dict) else {}
        stale_after_s = float(controls_cfg.get("stale_after_s", 10.0) or 10.0)
        return bool(is_state_stale(st, stale_after_s))
    except Exception:
        return False


def _run(cmd: list[str], cwd: Path) -> dict:
    t0 = time.time()
    try:
        proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, check=False)
        return {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "stdout": (proc.stdout or "")[-4000:],
            "stderr": (proc.stderr or "")[-4000:],
            "seconds": round(time.time() - t0, 2),
            "cmd": cmd,
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "seconds": round(time.time() - t0, 2), "cmd": cmd}


def main() -> int:
    ap = argparse.ArgumentParser(description="Reset workflow state before starting a new run.")
    ap.add_argument("--force-clear-owner", action="store_true", help="Force-clear controls owner even if not stale")
    ap.add_argument("--refresh-controls", action="store_true", help="Refresh controls_state.json timestamp by re-writing paused state")
    ap.add_argument("--auto-unpause", action="store_true", help="Unpause controls when policy allows and no active control window")
    ap.add_argument("--force-unpause", action="store_true", help="Force controls_state.paused=false regardless of control window")
    ap.add_argument("--out", default="", help="Optional output JSON path (default: logs/tests/workflow_reset_<ts>.json)")
    ap.add_argument("--dry-run", action="store_true", help="Do not execute any changes; only report")
    args = ap.parse_args()

    root = _root()
    py = str(root / "Scripts" / "python.exe")
    logs = root / "logs" / "tests"
    logs.mkdir(parents=True, exist_ok=True)

    st = _load_json(root / "config" / "controls_state.json")
    paused = bool(st.get("paused", False)) if isinstance(st, dict) else False
    owner = str(st.get("owner", "") or "") if isinstance(st, dict) else ""
    stale = _is_stale(root, st if isinstance(st, dict) else {})

    actions: list[dict] = []

    if (stale or args.force_clear_owner) and owner:
        if not args.dry_run:
            actions.append(_run([py, "Scripts/controls_release_owner.py", "--force"], root))
        else:
            actions.append({"ok": True, "cmd": [py, "Scripts/controls_release_owner.py", "--force"], "dry_run": True})

    if args.force_unpause:
        if not args.dry_run:
            actions.append(_run([py, "Scripts/controls_set_paused.py", "--paused", "false"], root))
            paused = False
        else:
            actions.append({"ok": True, "cmd": [py, "Scripts/controls_set_paused.py", "--paused", "false"], "dry_run": True})
            paused = False

    if (not args.force_unpause) and args.auto_unpause and _auto_unpause_allowed(root, st if isinstance(st, dict) else {}):
        if not args.dry_run:
            actions.append(_run([py, "Scripts/controls_set_paused.py", "--paused", "false"], root))
            paused = False
        else:
            actions.append({"ok": True, "cmd": [py, "Scripts/controls_set_paused.py", "--paused", "false"], "dry_run": True})
            paused = False

    if args.refresh_controls:
        if not args.dry_run:
            actions.append(_run([py, "Scripts/controls_set_paused.py", "--paused", "true" if paused else "false"], root))
        else:
            actions.append({"ok": True, "cmd": [py, "Scripts/controls_set_paused.py", "--paused", "true" if paused else "false"], "dry_run": True})

    out_path = Path(args.out) if args.out else logs / f"workflow_reset_{time.strftime('%Y%m%d_%H%M%S')}.json"
    if not out_path.is_absolute():
        out_path = (root / out_path).resolve()

    report = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "controls_state": st,
        "stale": bool(stale),
        "actions": actions,
        "dry_run": bool(args.dry_run),
    }

    out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(str(out_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
