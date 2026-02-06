from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import time
from pathlib import Path


def _root() -> Path:
    return Path(__file__).resolve().parent.parent


def _load_json(path: Path) -> dict:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}
    return {}


def _agent_mode_active(root: Path) -> bool:
    v = os.environ.get("AI_CONTROLLER_AGENT_MODE")
    if v is not None:
        return v.strip().lower() in {"1", "true", "yes", "on"}
    ui_state = _load_json(root / "config" / "ui_state.json")
    return bool(ui_state.get("agent_mode", False))


def _controls_blocked(root: Path) -> tuple[bool, str]:
    try:
        from src.control_state import get_controls_state, is_state_stale  # type: ignore
    except Exception:
        return False, ""

    rules = _load_json(root / "config" / "policy_rules.json")
    controls_cfg = (rules.get("controls") or {}) if isinstance(rules, dict) else {}
    stale_after_s = float(controls_cfg.get("stale_after_s", 10.0) or 10.0)

    st = get_controls_state(root) or {}
    owner = str(st.get("owner", "") or "")
    paused = bool(st.get("paused", False))
    try:
        stale = bool(is_state_stale(st, stale_after_s))
    except Exception:
        stale = False

    # Fail closed: if controls state is stale, do not attempt live UI automation.
    # Users can refresh the snapshot by toggling paused (e.g., Scripts/controls_set_paused.py --paused false).
    if stale:
        return True, "controls_state.stale"

    if paused:
        return True, "controls_state.paused"
    if owner and owner != "workflow_test":
        return True, f"controls owned by '{owner}'"
    return False, ""


def _stable_id(name: str, cmd: list[str]) -> str:
    h = hashlib.sha256()
    h.update((name + "\n").encode("utf-8"))
    h.update("\u241f".join(cmd).encode("utf-8", errors="ignore"))
    return h.hexdigest()[:16]


def _sanitize_tag(s: str) -> str:
    s = str(s or "").strip()
    s = re.sub(r"[^A-Za-z0-9_.-]+", "_", s)
    return s[:120] if len(s) > 120 else s


def _done_marker_path(done_dir: Path, action_id: str, run_id: str) -> Path:
    """Back-compat: if run_id is missing, keep the legacy global marker filename."""

    action_id = str(action_id or "").strip()
    run_id = str(run_id or "").strip()
    if run_id:
        return done_dir / f"{action_id}__run_{_sanitize_tag(run_id)}.json"
    return done_dir / f"{action_id}.json"


def main() -> int:
    ap = argparse.ArgumentParser(description="Run deferred workflow actions when it is safe (Agent Mode off, controls free).")
    ap.add_argument("--queue", default="logs/actions/deferred_workflow_actions.jsonl")
    ap.add_argument("--max", type=int, default=50, help="Max actions to attempt this run")
    ap.add_argument("--id", action="append", help="Only consider a specific deferred action id (repeatable)")
    ap.add_argument("--run-id", action="append", help="Only consider deferred actions from a specific workflow run_id")
    ap.add_argument("--force", action="store_true", help="Re-run even if done marker exists")
    ap.add_argument("--dry-run", action="store_true", help="Only print what would run (default)")
    ap.add_argument("--live", action="store_true", help="Actually execute deferred actions")
    ap.add_argument("--list", action="store_true", help="List pending deferred actions and exit")
    ap.add_argument("--all", action="store_true", help="Do not de-duplicate by action id (shows every queued line)")
    ap.add_argument("--prune", action="store_true", help="Rewrite queue file to keep only the latest entry per action id (creates a .bak_ timestamp backup)")
    args = ap.parse_args()

    root = _root()
    queue_path = (root / args.queue).resolve()
    done_dir = root / "logs" / "actions" / "deferred_workflow_actions_done"
    done_dir.mkdir(parents=True, exist_ok=True)
    results_path = root / "logs" / "actions" / "deferred_workflow_actions_results.jsonl"

    if not queue_path.exists():
        print(f"No deferred queue found: {queue_path}")
        return 0

    # Default to dry-run unless explicitly --live.
    if not args.live:
        args.dry_run = True

    # Only block *execution* when Agent Mode is active or controls are blocked.
    # Listing/dry-run is safe and should still work so users can see what is queued.
    agent_mode_now = _agent_mode_active(root)
    blocked, reason = _controls_blocked(root)
    if args.live:
        if agent_mode_now:
            print("Agent Mode active; will not run deferred actions. Use --list/--dry-run to inspect the queue.")
            return 0
        if blocked:
            print(f"Controls not available; will not run deferred actions ({reason}). Use --list/--dry-run to inspect the queue.")
            return 0

    lines = queue_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    actions: list[dict] = []
    for line in lines:
        s = line.strip()
        if not s:
            continue
        try:
            obj = json.loads(s)
            if isinstance(obj, dict):
                actions.append(obj)
        except Exception:
            continue

    if args.prune:
        # Compact the queue file to latest entry per action id.
        latest_by_id: dict[str, dict] = {}
        order: list[str] = []
        for a in actions:
            try:
                name = str(a.get("name") or "")
                cmd = a.get("cmd")
                if not isinstance(cmd, list) or not all(isinstance(x, str) for x in cmd):
                    continue
                action_id = str(a.get("id") or "")
                if not action_id:
                    action_id = _stable_id(name, cmd)
                # Maintain order of last occurrence.
                if action_id in latest_by_id:
                    try:
                        order.remove(action_id)
                    except Exception:
                        pass
                latest_by_id[action_id] = a
                order.append(action_id)
            except Exception:
                continue

        ts = time.strftime("%Y%m%d_%H%M%S")
        backup_path = queue_path.with_suffix(queue_path.suffix + f".bak_{ts}")
        try:
            backup_path.write_text(queue_path.read_text(encoding="utf-8", errors="ignore"), encoding="utf-8")
        except Exception:
            pass

        try:
            with queue_path.open("w", encoding="utf-8") as f:
                for action_id in order:
                    obj = latest_by_id.get(action_id)
                    if isinstance(obj, dict):
                        f.write(json.dumps(obj, ensure_ascii=False) + "\n")
            print(f"Pruned queue: kept {len(order)} unique actions (backup: {backup_path})")
        except Exception as e:
            print(f"Failed to prune queue: {e}")
            return 1

        # Reload after prune so subsequent list/dry-run reflects the new state.
        lines = queue_path.read_text(encoding="utf-8", errors="ignore").splitlines()
        actions = []
        for line in lines:
            s = line.strip()
            if not s:
                continue
            try:
                obj = json.loads(s)
                if isinstance(obj, dict):
                    actions.append(obj)
            except Exception:
                continue

    attempted = 0
    ran = 0
    only_ids: set[str] = set()
    try:
        only_ids = {str(x).strip() for x in (args.id or []) if str(x).strip()}
    except Exception:
        only_ids = set()

    only_run_ids: set[str] = set()
    try:
        only_run_ids = {str(x).strip() for x in (args.run_id or []) if str(x).strip()}
    except Exception:
        only_run_ids = set()

    # Default behavior: treat deferred actions as global by action id.
    # If the user scopes by --run-id, switch to per-run behavior.
    per_run = bool(only_run_ids)

    pending_raw: list[dict] = []
    for a in actions:
        name = str(a.get("name") or "")
        cmd = a.get("cmd")
        if not isinstance(cmd, list) or not all(isinstance(x, str) for x in cmd):
            continue
        action_id = str(a.get("id") or "")
        if not action_id:
            action_id = _stable_id(name, cmd)

        run_id = str(a.get("run_id") or "")
        if only_run_ids and (not run_id or run_id not in only_run_ids):
            continue

        if only_ids and action_id not in only_ids:
            continue

        effective_run_id = run_id if per_run else ""
        done_marker = _done_marker_path(done_dir, action_id, effective_run_id)
        if done_marker.exists() and not args.force:
            continue

        pending_raw.append({
            "id": action_id,
            "name": name,
            "cmd": cmd,
            "reason": str(a.get("reason") or ""),
            "run_id": run_id,
        })

    if not pending_raw:
        if only_ids:
            print(f"No pending deferred actions matching --id ({len(only_ids)} requested).")
        elif only_run_ids:
            print(f"No pending deferred actions matching --run-id ({len(only_run_ids)} requested).")
        else:
            # Common case after global completion: the queue still contains historical lines,
            # but every action id has a done marker.
            if actions:
                print("No pending deferred actions (all action ids already have done markers).")
                print("Tip: use --run-id <run_id> to treat actions per workflow run, or --force to re-run.")
            else:
                print("No pending deferred actions.")
        return 0

    # De-dupe globally by action id by default (queue can contain repeats across runs).
    # If --run-id is specified, de-dupe by (action id, run_id).
    pending: list[dict] = []
    if args.all:
        pending = pending_raw
    else:
        counts: dict[str, int] = {}
        latest_by_key: dict[str, dict] = {}
        order: list[str] = []
        for p in reversed(pending_raw):
            action_id = str(p.get("id") or "")
            rid = str(p.get("run_id") or "")
            k = f"{action_id}::{rid}" if per_run else action_id
            counts[k] = counts.get(k, 0) + 1
            if k in latest_by_key:
                continue
            latest_by_key[k] = p
            order.append(k)
        for k in reversed(order):
            item = latest_by_key.get(k) or {}
            item["_queued_count"] = int(counts.get(k, 1))
            pending.append(item)

    print(f"Pending deferred actions: {len(pending)}" + (" (deduped)" if not args.all else " (all queued lines)"))
    for i, p in enumerate(pending[: max(0, int(args.max))], start=1):
        cmd = p.get("cmd") or []
        reason = str(p.get("reason") or "").strip()
        rid = str(p.get("run_id") or "").strip()
        cnt = int(p.get("_queued_count") or 1)
        suffix = []
        if reason:
            suffix.append(f"reason={reason}")
        if rid:
            suffix.append(f"run_id={rid}")
        if cnt > 1:
            suffix.append(f"queued={cnt}x")
        extra = (" (" + ", ".join(suffix) + ")") if suffix else ""
        print(f"  {i}. {p.get('id')} {p.get('name')}{extra}")
        print("     " + " ".join(str(x) for x in cmd))

    if args.list:
        return 0

    if args.dry_run and not args.live:
        print("Dry-run only (no execution). Use --live to execute.")
        return 0

    # Execute
    for p in pending:
        if attempted >= max(0, int(args.max)):
            break
        name = str(p.get("name") or "")
        cmd = p.get("cmd")
        action_id = str(p.get("id") or "")
        run_id = str(p.get("run_id") or "")
        if not isinstance(cmd, list) or not all(isinstance(x, str) for x in cmd):
            continue

        effective_run_id = run_id if per_run else ""
        done_marker = _done_marker_path(done_dir, action_id, effective_run_id)
        if done_marker.exists() and not args.force:
            continue

        # Re-check safety right before executing.
        if _agent_mode_active(root):
            print("Agent Mode became active; stopping.")
            break
        blocked, reason = _controls_blocked(root)
        if blocked:
            print(f"Controls became unavailable; stopping ({reason}).")
            break

        attempted += 1
        t0 = time.time()
        proc = subprocess.run(cmd, cwd=str(root), capture_output=True, text=True, check=False)
        dt = round(time.time() - t0, 2)
        ok = proc.returncode == 0
        ran += 1

        result = {
            "id": action_id,
            "run_id": run_id,
            "name": name,
            "cmd": cmd,
            "returncode": proc.returncode,
            "ok": ok,
            "seconds": dt,
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "stdout_tail": (proc.stdout or "")[-4000:],
            "stderr_tail": (proc.stderr or "")[-4000:],
        }
        try:
            with results_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(result, ensure_ascii=False) + "\n")
        except Exception:
            pass

        try:
            done_marker.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

        print(f"Deferred action {action_id} ({name}) -> {'OK' if ok else 'FAIL'} ({dt}s)")
        if not ok:
            # Stop on first failure to avoid thrashing.
            break

    print(f"Deferred actions: attempted={attempted} ran={ran}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
