from __future__ import annotations
import json
import subprocess
import sys
import time
import atexit
import os
from pathlib import Path
import glob
import hashlib
import re


def _as_bool(v, default: bool = False) -> bool:
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


def _load_json_file(path: Path) -> dict:
    try:
        if path.exists():
            obj = json.loads(path.read_text(encoding="utf-8"))
            return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}
    return {}


def _detect_active_agent_processes(root: Path) -> list[dict]:
    """Best-effort scan for other agent/controller processes on Windows."""

    if os.name != "nt":
        return []

    keywords = [
        "workflow_test_gather_assess.py",
        "orchestrator_agent.py",
        "run_deferred_workflow_actions.py",
        "copilot_commit.ps1",
        "copilot_commit_start.ps1",
        "copilot_commit_with_record.ps1",
        "vscode_terminal_run_loop.py",
        "observe_and_react.py",
        "ocr_commit_test.py",
        "navigation_test.py",
        "copilot_app_interaction_test.py",
        "vscode_multi_keepalive_daemon.py",
        "assess_chat_lanes.py",
    ]
    key_lc = [k.lower() for k in keywords]

    try:
        cmd = (
            "$procs = Get-CimInstance Win32_Process "
            "| Where-Object { $_.CommandLine -like '*AI_Coder_Controller*' } "
            "| Select-Object ProcessId,Name,CommandLine; "
            "$procs | ConvertTo-Json -Depth 3"
        )
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-Command", cmd],
            capture_output=True,
            text=True,
            check=False,
        )
        raw = (proc.stdout or "").strip()
        if not raw:
            return []
        try:
            data = json.loads(raw)
        except Exception:
            return []
        if isinstance(data, dict):
            items = [data]
        elif isinstance(data, list):
            items = data
        else:
            return []

        found: list[dict] = []
        for it in items:
            if not isinstance(it, dict):
                continue
            cmdline = str(it.get("CommandLine") or "")
            cl = cmdline.lower()
            matches = [k for k in key_lc if k in cl]
            if not matches:
                continue
            found.append(
                {
                    "pid": int(it.get("ProcessId") or 0),
                    "name": str(it.get("Name") or ""),
                    "command_line": cmdline,
                    "matches": matches,
                }
            )
        return found
    except Exception:
        return []


def _collect_issue_names(summary: dict) -> list[str]:
    names: set[str] = set()
    for s in summary.get("steps", []) or []:
        try:
            if ("pass" in s) and (not bool(s.get("pass"))) and (not bool(s.get("skipped", False))):
                n = str(s.get("name") or "").strip().lower()
                if n:
                    names.add(n)
        except Exception:
            pass
    for bucket in ("errors", "warnings"):
        for e in summary.get(bucket, []) or []:
            n = str((e or {}).get("name") or "").strip().lower()
            if n:
                names.add(n)
            for s in (e or {}).get("samples", []) or []:
                et = str((s or {}).get("event") or (s or {}).get("type") or "").strip().lower()
                if et:
                    names.add(et)
    return sorted(names)


def _sanitize_tag(s: str) -> str:
    s = str(s or "").strip()
    s = re.sub(r"[^A-Za-z0-9_.-]+", "_", s)
    return s[:120] if len(s) > 120 else s


def _deferred_done_marker_exists(root: Path, action_id: str, run_id: str) -> bool:
    try:
        done_dir = root / "logs" / "actions" / "deferred_workflow_actions_done"
        action_id = str(action_id or "").strip()
        run_id = str(run_id or "").strip()
        if not action_id:
            return False
        # Back-compat: no run_id -> legacy global marker.
        if run_id:
            p = done_dir / f"{action_id}__run_{_sanitize_tag(run_id)}.json"
        else:
            p = done_dir / f"{action_id}.json"
        return p.exists()
    except Exception:
        return False


def _choose_recommended_deferred_action(root: Path, run_id: str, deferred_steps: list[dict]) -> dict | None:
    """Pick a best first deferred action to run.

    Heuristic: prefer higher-signal setup/navigation steps first; avoid verify-only steps.
    Returns {"id":..., "name":..., "reason":...} or None.
    """

    def _score(step_name: str) -> int:
        n = (step_name or "").strip().lower()
        # Higher score = recommended earlier.
        if n in {"navigation_test", "focus_vscode"}:
            return 100
        if n in {"ocr_commit_test", "ocr_smoke_test", "ocr_gate_chat_ready"}:
            return 90
        if n in {"observe_and_react", "ocr_observe_react", "observe_react"}:
            return 80
        if n in {"gather_chat_evidence", "vscode_multi_keepalive", "copilot_app_interaction"}:
            return 70
        if "verify" in n:
            return 30
        if n in {"final_foreground_check"}:
            return 20
        return 50

    best: tuple[int, str, dict] | None = None
    for s in deferred_steps or []:
        if not isinstance(s, dict):
            continue
        if not bool(s.get("deferred", False)):
            continue
        if bool(s.get("queue_enqueued", True)) is False:
            continue
        action_id = str(s.get("deferred_id") or "").strip()
        name = str(s.get("name") or "").strip()
        if not action_id or not name:
            continue
        # Skip if already completed for this run (or globally if no run_id).
        if _deferred_done_marker_exists(root, action_id, run_id):
            continue
        score = _score(name)
        # Stable tie-breaker: by name.
        key = (score, name.lower(), s)
        if best is None or key > best:
            best = key

    if best is None:
        return None
    score, _, s = best
    return {
        "id": str(s.get("deferred_id") or ""),
        "name": str(s.get("name") or ""),
        "reason": str(s.get("reason") or ""),
        "score": int(score),
    }


def build_workflow_recommendations_md(summary: dict) -> str:
    """Create an actionable recommendations report from a workflow summary dict."""

    started = str(summary.get("started") or "")
    finished = str(summary.get("finished") or "")
    overall_pass = _as_bool(summary.get("pass"), False)
    status = str(summary.get("status") or "").strip().upper()
    if status not in {"PASS", "FAIL", "DEFERRED"}:
        status = "PASS" if overall_pass else "FAIL"
    run_id = str(summary.get("run_id") or "").strip()

    wi = summary.get("workflow_info") or {}
    ctx = (wi.get("interaction_context") or {}) if isinstance(wi, dict) else {}
    agent_mode_active = _as_bool(ctx.get("agent_mode_active"), False)
    interactive_allowed = _as_bool(ctx.get("interactive_allowed"), True)
    interactions_deferred = _as_bool(ctx.get("interactions_deferred"), False)
    defer_reason = str(ctx.get("defer_reason") or "").strip()

    deferred_steps_all = [s for s in (summary.get("steps") or []) if isinstance(s, dict) and bool(s.get("deferred", False))]
    deferred_steps = [s for s in deferred_steps_all if bool(s.get("queue_enqueued", True))]
    deferred_suppressed = [s for s in deferred_steps_all if bool(s.get("queue_enqueued", True)) is False]
    suppressed_by_reason: dict[str, int] = {}
    for s in deferred_suppressed:
        r = str(s.get("queue_skip_reason") or "").strip() or "unknown"
        suppressed_by_reason[r] = suppressed_by_reason.get(r, 0) + 1

    issue_names = _collect_issue_names(summary)

    lines: list[str] = []
    lines.append(f"# Workflow Recommendations ({time.strftime('%Y-%m-%d %H:%M:%S')})")
    lines.append("")
    if run_id:
        lines.append(f"- Run ID: {run_id}")
    lines.append(f"- Run window: {started} → {finished}")
    lines.append(f"- Overall: {status}")
    lines.append(f"- Agent Mode: {'ON' if agent_mode_active else 'OFF'}")
    lines.append(f"- Interactive allowed: {'YES' if interactive_allowed else 'NO'}")
    if interactions_deferred:
        lines.append(f"- Interactions deferred: YES ({defer_reason or 'unknown'})")
    lines.append("")

    lines.append("## Observations")
    lines.append("")

    # Preflight snapshot (guaranteed in this workflow runner).
    preflight = (wi.get("preflight") or {}) if isinstance(wi, dict) else {}
    pf_warn = preflight.get("warnings") or []
    pf_suggest = preflight.get("suggestions") or []
    if isinstance(pf_warn, list) and pf_warn:
        lines.append(f"- Preflight warnings: {len(pf_warn)}")
        for w in pf_warn[:6]:
            lines.append(f"  - {str(w)}")
    else:
        lines.append("- Preflight warnings: none")
    if isinstance(pf_suggest, list) and pf_suggest:
        lines.append("- Preflight suggested actions (quick):")
        for s in pf_suggest[:6]:
            lines.append(f"  - {str(s)}")

    if issue_names:
        short = issue_names[:18]
        tail = "…" if len(issue_names) > 18 else ""
        lines.append(f"- Detected issue types: {', '.join(short)}{tail}")
    else:
        lines.append("- Detected issue types: none")
    if interactions_deferred:
        lines.append(f"- Deferred steps: {len(deferred_steps_all)} (newly enqueued: {len(deferred_steps)}, suppressed: {len(deferred_suppressed)})")
        if deferred_suppressed:
            parts = [f"{k}={v}" for k, v in sorted(suppressed_by_reason.items(), key=lambda kv: (-kv[1], kv[0]))]
            lines.append(f"- Queue dedupe/cooldown active: {', '.join(parts)}")
    lines.append("")

    lines.append("## Recommendations")
    lines.append("")
    recs: list[str] = []

    if agent_mode_active and interactions_deferred:
        recs.append(
            "Agent Mode is active; interactive steps were deferred. For full workflow coverage, set `config/ui_state.json` `agent_mode=false` and rerun."
        )

    if interactions_deferred:
        if run_id:
            recs.append(
                f"Deferred actions were queued. Review and run them later (when Agent Mode is OFF and controls are free): `Scripts/python.exe Scripts/run_deferred_workflow_actions.py --dry-run --run-id {run_id}` then `Scripts/python.exe Scripts/run_deferred_workflow_actions.py --live --run-id {run_id}`."
            )
        else:
            recs.append(
                "Deferred actions were queued. Review and run them later (when Agent Mode is OFF and controls are free): `Scripts/python.exe Scripts/run_deferred_workflow_actions.py --dry-run` then `Scripts/python.exe Scripts/run_deferred_workflow_actions.py --live`."
            )

    if isinstance(pf_warn, list) and any("controls_state.paused=true" in str(w) for w in pf_warn):
        recs.append("Controls are paused. If you intend live UI automation, unpause first (or expect DEFERRED/skip behavior): task 'Controls: Unpause (paused=false)' or `Scripts/python.exe Scripts/controls_set_paused.py --paused false`." )
    if isinstance(pf_warn, list) and any("controls_state.owner=" in str(w) for w in pf_warn):
        recs.append("Controls are owned by another workflow. Wait for it to release, or (if truly stale) force-clear owner: task 'Release controls owner (force)' or `Scripts/python.exe Scripts/controls_release_owner.py --force`." )

    # Always include a lightweight reminder command for humans/agents.
    recs.append("Before starting a new live workflow run, inspect safety settings: `Scripts/python.exe Scripts/controls_inspect.py --stale-seconds 300`." )

    if interactions_deferred and deferred_suppressed:
        recs.append(
            "Deferred queue growth was suppressed for already-queued / recently-done actions. This is expected during repeated DEFERRED runs and helps prevent queue bloat; focus on draining the pending queue rather than rerunning the workflow repeatedly while paused."
        )

    if any(x in issue_names for x in ["text_input_wrong_field", "vscode_chat_type_failed", "input_aborted_focus_changed"]):
        recs.append(
            "Wrong-field / focus-change signals detected: avoid Command Palette typing; prefer deterministic hotkeys + verified focus. Consider increasing settle/delay knobs if UI is slow."
        )

    if any(x in issue_names for x in ["terminal_focus_failed", "terminal_type_failed"]):
        recs.append(
            "Terminal focus/type failures detected: verify VS Code is foreground and that Ctrl+` is not remapped; fail closed on focus uncertainty and re-run."
        )

    if any(x in issue_names for x in ["navigation_test", "navigation_test_verify", "focus_vscode"]):
        recs.append(
            "Navigation test failures detected: VS Code window focus may be ambiguous (multiple windows, minimized, or title mismatch). Consider narrowing the window match, ensuring Code.exe is foreground, and closing extra VS Code instances during tests."
        )

    if not recs:
        recs.append("No specific remediation needed; keep current settings.")

    for r in recs:
        lines.append(f"- {r}")
    lines.append("")

    # Optional: parallel chat lanes handoff section.
    # This helps users split work across multiple VS Code Copilot Chat tabs.
    lines.append("## Lane handoff (parallel VS Code chat tabs)")
    lines.append("")
    lines.append("If you're using `projects/Chat_Lanes/`, assign one tab per lane and use the lane files as shared working memory.")
    lines.append("")
    wi_dq = (wi.get("deferred_queue") or {}) if isinstance(wi, dict) else {}
    dq_unique = 0
    dq_total = 0
    try:
        dq_unique = int((wi_dq.get("unique_actions") or 0)) if isinstance(wi_dq, dict) else 0
        dq_total = int((wi_dq.get("total_lines") or 0)) if isinstance(wi_dq, dict) else 0
    except Exception:
        dq_unique = 0
        dq_total = 0

    # Workflow lane
    if interactions_deferred:
        lines.append("- Workflow lane (`projects/Chat_Lanes/lane_workflow.md`): run deferred actions when ready.")
        lines.append(f"  - Queue estimate: unique={dq_unique} total_lines={dq_total}")
        try:
            rec = (wi.get("recommended_deferred_action") or {}) if isinstance(wi, dict) else {}
        except Exception:
            rec = {}
        if isinstance(rec, dict) and str(rec.get("id") or "").strip():
            rid = str(rec.get("id") or "").strip()
            rname = str(rec.get("name") or "").strip()
            lines.append(f"  - Recommended first: {rname} (id={rid})")
            if run_id:
                lines.append(f"  - Run only this: `Scripts/python.exe Scripts/run_deferred_workflow_actions.py --dry-run --run-id {run_id} --id {rid} --max 1`")
                lines.append(f"    then: `Scripts/python.exe Scripts/run_deferred_workflow_actions.py --live --run-id {run_id} --id {rid} --max 1`")
            else:
                lines.append(f"  - Run only this: `Scripts/python.exe Scripts/run_deferred_workflow_actions.py --dry-run --id {rid} --max 1`")
                lines.append(f"    then: `Scripts/python.exe Scripts/run_deferred_workflow_actions.py --live --id {rid} --max 1`")
        if run_id:
            lines.append(f"  - Inspect: `Scripts/python.exe Scripts/run_deferred_workflow_actions.py --list --run-id {run_id}`")
        else:
            lines.append("  - Inspect: `Scripts/python.exe Scripts/run_deferred_workflow_actions.py --list`")
        if run_id:
            lines.append(f"  - Dry-run: `Scripts/python.exe Scripts/run_deferred_workflow_actions.py --dry-run --run-id {run_id}`")
            lines.append(f"  - Execute: `Scripts/python.exe Scripts/run_deferred_workflow_actions.py --live --run-id {run_id}`")
        else:
            lines.append("  - Dry-run: `Scripts/python.exe Scripts/run_deferred_workflow_actions.py --dry-run`")
            lines.append("  - Execute: `Scripts/python.exe Scripts/run_deferred_workflow_actions.py --live`")
    else:
        lines.append("- Workflow lane (`projects/Chat_Lanes/lane_workflow.md`): monitor new runs; manual prune only.")
    lines.append("")

    # Primary lane
    lines.append("- Primary lane (`projects/Chat_Lanes/lane_primary.md`): read latest summary + recommendations and decide priority.")
    lines.append("  - Outcome meanings: PASS vs DEFERRED vs FAIL")
    if agent_mode_active and interactions_deferred:
        lines.append("  - To allow click/type: set `agent_mode=false`, unpause controls, rerun workflow")
    lines.append("")

    # OCR lane
    lines.append("- OCR lane (`projects/Chat_Lanes/lane_ocr.md`): review OCR logs/screens for focus/text-field issues.")
    lines.append("  - Propose tweaks: delays, focus verification, field targeting")
    lines.append("")

    # Triage lane
    lines.append("- Triage lane (`projects/Chat_Lanes/lane_triage.md`): watch `projects/Chat_Lanes/notifications.jsonl`.")
    lines.append("  - Call out conflicts: controls paused/owned; repeated failure patterns")
    lines.append("")

    lines.append("## Useful knobs")
    lines.append("")
    lines.append("- `AI_CONTROLLER_AGENT_MODE` (force Agent Mode ON/OFF for this process)")
    lines.append("- `AI_CONTROLLER_ENABLE_COMMIT_VERIFY` (enables commit+verify step)")
    lines.append("- `AI_CONTROLLER_ENABLE_COPILOT_APP_INTERACTION` (enables Copilot app interaction step)")
    lines.append("- `AI_CONTROLLER_DEFER_INTERACTIONS_WHEN_AGENT_MODE` (defer interactive steps when Agent Mode is on)")
    lines.append("")

    return "\n".join(lines)


def run_cmd(cmd: list[str], cwd: Path) -> dict:
    t0 = time.time()
    try:
        proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, check=False)
        return {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "stdout": proc.stdout[-4000:],
            "stderr": proc.stderr[-4000:],
            "started_ts": t0,
            "finished_ts": time.time(),
            "seconds": round(time.time() - t0, 2),
            "cmd": cmd,
        }
    except KeyboardInterrupt:
        # Important: this script runs GUI automation subprocesses which can
        # misdirect keystrokes into the terminal. If a stray Ctrl+C hits this
        # process, capture it as a failed step rather than aborting silently.
        return {
            "ok": False,
            "returncode": 130,
            "stderr": "KeyboardInterrupt (SIGINT)",
            "started_ts": t0,
            "finished_ts": time.time(),
            "seconds": round(time.time() - t0, 2),
            "cmd": cmd,
        }
    except Exception as e:
        return {
            "ok": False,
            "error": str(e),
            "started_ts": t0,
            "finished_ts": time.time(),
            "seconds": round(time.time() - t0, 2),
            "cmd": cmd,
        }


def summarize_deferred_queue(queue_path: Path, *, max_top: int = 8) -> dict:
    """Summarize deferred workflow queue health without modifying it."""
    if not queue_path.exists():
        return {
            "exists": False,
            "total_lines": 0,
            "parsed": 0,
            "unique_actions": 0,
            "top_repeated": [],
            "run_ids": [],
        }

    lines = queue_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    parsed: list[dict] = []
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

    counts: dict[str, int] = {}
    latest_by_id: dict[str, dict] = {}
    run_ids: set[str] = set()
    for a in parsed:
        try:
            rid = str(a.get("run_id") or "").strip()
            if rid:
                run_ids.add(rid)
            action_id = str(a.get("id") or "").strip()
            if not action_id:
                # Fall back to stable id computation if the writer omitted it.
                name = str(a.get("name") or "")
                cmd = a.get("cmd")
                if isinstance(cmd, list) and all(isinstance(x, str) for x in cmd):
                    h = hashlib.sha256()
                    h.update((name + "\n").encode("utf-8"))
                    h.update("\u241f".join(cmd).encode("utf-8", errors="ignore"))
                    action_id = h.hexdigest()[:16]
            if not action_id:
                continue
            counts[action_id] = counts.get(action_id, 0) + 1
            latest_by_id[action_id] = a
        except Exception:
            continue

    top = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[: max(0, int(max_top))]
    top_repeated: list[dict] = []
    for action_id, n in top:
        a = latest_by_id.get(action_id) or {}
        top_repeated.append({
            "id": action_id,
            "queued": int(n),
            "name": str(a.get("name") or ""),
            "run_id": str(a.get("run_id") or ""),
        })

    return {
        "exists": True,
        "total_lines": int(len(lines)),
        "parsed": int(len(parsed)),
        "unique_actions": int(len(counts)),
        "top_repeated": top_repeated,
        "run_ids": sorted(run_ids),
    }


def _chat_lanes_enabled(root: Path) -> bool:
    """Enable file-based parallel chat lanes when explicitly enabled or when the board exists."""
    v = os.environ.get("AI_CONTROLLER_ENABLE_PARALLEL_CHAT_LANES")
    if v is not None:
        return _as_bool(v, False)
    # If the user has initialized lanes once, keep using them.
    return (root / "projects" / "Chat_Lanes" / "BOARD.md").exists()


def _append_chat_lane_event(root: Path, *, type_: str, message: str, run_id: str | None = None, lane: str | None = None) -> bool:
    """Append an event to projects/Chat_Lanes/notifications.jsonl (best-effort)."""
    try:
        d = root / "projects" / "Chat_Lanes"
        if not d.exists() and not _chat_lanes_enabled(root):
            return False
        d.mkdir(parents=True, exist_ok=True)
        notif = d / "notifications.jsonl"
        evt = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "type": type_,
            "message": message,
        }
        if run_id:
            evt["run_id"] = run_id
        if lane:
            evt["lane"] = lane
        with notif.open("a", encoding="utf-8") as f:
            f.write(json.dumps(evt, ensure_ascii=False) + "\n")
        return True
    except Exception:
        return False


def _assess_chat_lanes(root: Path, *, stale_minutes: float = 30.0) -> dict:
    """Best-effort assessment of the parallel Chat Lanes coordination files."""

    lanes_dir = root / "projects" / "Chat_Lanes"
    notif = lanes_dir / "notifications.jsonl"
    now = time.time()
    stale_after_s = max(0.0, float(stale_minutes)) * 60.0

    res: dict = {
        "ok": True,
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "lanes_dir": str(lanes_dir),
        "notifications": str(notif),
        "notifications_exists": bool(notif.exists()),
        "events_total": 0,
        "events_by_type": {},
        "events_by_lane": {},
        "open_workflows": 0,
        "lane_files": [],
        "stale_lanes": [],
        "stale_minutes": float(stale_minutes),
        "recommendations": [],
    }

    if not lanes_dir.exists():
        res["ok"] = False
        res["recommendations"].append("Chat_Lanes directory missing. Run: Scripts/python.exe Scripts/parallel_chat_lanes.py init")
        return res

    # Load notifications.jsonl (best-effort)
    events: list[dict] = []
    if notif.exists():
        for line in notif.read_text(encoding="utf-8", errors="ignore").splitlines():
            s = line.strip()
            if not s:
                continue
            try:
                obj = json.loads(s)
            except Exception:
                continue
            if isinstance(obj, dict):
                events.append(obj)

    res["events_total"] = int(len(events))
    events_by_type: dict[str, int] = {}
    events_by_lane: dict[str, int] = {}
    open_workflows = 0
    for e in events:
        t = str(e.get("type") or "")
        if t:
            events_by_type[t] = events_by_type.get(t, 0) + 1
        lane = str(e.get("lane") or "")
        if lane:
            events_by_lane[lane] = events_by_lane.get(lane, 0) + 1
        if t == "workflow_started":
            open_workflows += 1
        elif t == "workflow_finished":
            open_workflows = max(0, open_workflows - 1)

    res["events_by_type"] = dict(sorted(events_by_type.items(), key=lambda kv: (-kv[1], kv[0])))
    res["events_by_lane"] = dict(sorted(events_by_lane.items(), key=lambda kv: (-kv[1], kv[0])))
    res["open_workflows"] = int(open_workflows)

    # Lane file freshness based on mtime
    lane_paths = sorted([p for p in lanes_dir.glob("lane_*.md") if p.is_file()], key=lambda p: p.name)
    stale_lanes: list[dict] = []
    lane_files: list[dict] = []
    for p in lane_paths:
        try:
            age_s = max(0.0, now - p.stat().st_mtime)
        except Exception:
            age_s = 0.0
        item = {"name": p.name, "age_s": round(age_s, 1)}
        lane_files.append(item)
        if stale_after_s > 0 and age_s >= stale_after_s:
            stale_lanes.append(item)
    res["lane_files"] = lane_files
    res["stale_lanes"] = stale_lanes

    # Recommendations (keep short)
    if not notif.exists():
        res["recommendations"].append("notifications.jsonl missing; run: Scripts/python.exe Scripts/parallel_chat_lanes.py init")
    elif res["events_total"] == 0:
        res["recommendations"].append("notifications.jsonl is empty; run the workflow once or post a note event.")
    if res["open_workflows"] > 0:
        res["recommendations"].append("A workflow may be in progress; avoid multiple live UI runners. Use deferred queue + lane notes.")
    if stale_lanes:
        res["recommendations"].append("Some lane files look stale; open each lane tab and append a short status update.")
    if res["events_total"] > 2000:
        res["recommendations"].append("notifications.jsonl is large; consider manual archiving/rotation to keep reviews fast.")

    return res


def _render_chat_lanes_assessment_md(run_id: str, assessment: dict) -> str:
    lines: list[str] = []
    lines.append(f"# Chat Lanes Assessment ({time.strftime('%Y-%m-%d %H:%M:%S')})")
    lines.append("")
    lines.append(f"- Run ID: {run_id}")
    lines.append(f"- Lanes dir: {assessment.get('lanes_dir','')}")
    lines.append(f"- Notifications exists: {bool(assessment.get('notifications_exists', False))}")
    lines.append(f"- Events total: {int(assessment.get('events_total') or 0)}")
    lines.append(f"- Open workflows (best-effort): {int(assessment.get('open_workflows') or 0)}")
    lines.append("")

    lines.append("## Events by type")
    lines.append("")
    by_type = assessment.get("events_by_type") or {}
    if isinstance(by_type, dict) and by_type:
        for k, v in list(by_type.items())[:30]:
            lines.append(f"- {k}: {v}")
    else:
        lines.append("- (none)")
    lines.append("")

    lines.append("## Events by lane")
    lines.append("")
    by_lane = assessment.get("events_by_lane") or {}
    if isinstance(by_lane, dict) and by_lane:
        for k, v in list(by_lane.items())[:30]:
            lines.append(f"- {k}: {v}")
    else:
        lines.append("- (none)")
    lines.append("")

    lines.append("## Lane file freshness")
    lines.append("")
    lane_files = assessment.get("lane_files") or []
    if isinstance(lane_files, list) and lane_files:
        for it in lane_files[:30]:
            if not isinstance(it, dict):
                continue
            lines.append(f"- {it.get('name')} age_s={it.get('age_s')}")
    else:
        lines.append("- (no lane_*.md files)")
    lines.append("")

    lines.append("## Recommendations")
    lines.append("")
    recs = assessment.get("recommendations") or []
    if isinstance(recs, list) and recs:
        for r in recs[:20]:
            lines.append(f"- {r}")
    else:
        lines.append("- No issues detected.")

    return "\n".join(lines)


def _assess_assessment_schedule(root: Path) -> dict:
    schedule_path = root / "config" / "assessment_schedule.json"
    logic_guide = root / "docs" / "ASSESSMENT_LOGIC.md"
    res: dict = {
        "ok": False,
        "path": str(schedule_path),
        "logic_guide": str(logic_guide),
        "exists": bool(schedule_path.exists()),
        "errors": [],
        "warnings": [],
        "assessment_count": 0,
        "interval_count": 0,
        "upgrade_tasks": [],
        "decision": {},
    }
    if not schedule_path.exists():
        res["errors"].append("assessment_schedule.json missing")
        res["upgrade_tasks"].append("Create config/assessment_schedule.json with required fields")
        return res
    try:
        obj = json.loads(schedule_path.read_text(encoding="utf-8"))
    except Exception as e:
        res["errors"].append(f"failed to parse JSON: {e}")
        res["upgrade_tasks"].append("Fix JSON syntax in config/assessment_schedule.json")
        return res
    if not isinstance(obj, dict):
        res["errors"].append("schedule must be a JSON object")
        res["upgrade_tasks"].append("Make assessment_schedule.json a JSON object with required keys")
        return res
    assessments = obj.get("assessments")
    if not isinstance(assessments, list):
        res["errors"].append("assessments must be a list")
        res["upgrade_tasks"].append("Add 'assessments' list to assessment_schedule.json")
        return res

    for item in assessments:
        if not isinstance(item, dict):
            res["warnings"].append("non-object assessment entry")
            continue
        res["assessment_count"] += 1
        aid = str(item.get("id") or "").strip()
        cadence = str(item.get("cadence") or "").strip()
        if not aid:
            res["errors"].append("assessment entry missing id")
        if not cadence:
            res["errors"].append(f"assessment '{aid or 'unknown'}' missing cadence")
        if cadence == "interval":
            res["interval_count"] += 1
            iv = item.get("interval_seconds")
            try:
                if float(iv) <= 0:
                    res["errors"].append(f"assessment '{aid}' has non-positive interval_seconds")
                    res["upgrade_tasks"].append(f"Fix interval_seconds for assessment '{aid}'")
            except Exception:
                res["errors"].append(f"assessment '{aid}' missing interval_seconds")
                res["upgrade_tasks"].append(f"Add interval_seconds for assessment '{aid}'")
        elif cadence in {"per_run", "manual"}:
            pass
        elif cadence:
            res["warnings"].append(f"assessment '{aid}' uses unknown cadence '{cadence}'")
            res["upgrade_tasks"].append(f"Normalize cadence for assessment '{aid}' to per_run/manual/interval")

    res["ok"] = len(res["errors"]) == 0
    # Decision logic (prefer immediate upgrades unless blocking).
    if res["ok"] and not res["warnings"]:
        res["decision"] = {"action": "no_action", "reason": "schedule valid"}
    elif res["errors"]:
        # Blocking if file missing or JSON invalid; otherwise prefer immediate.
        blocking = any(
            "missing" in str(e).lower() or "parse" in str(e).lower() or "json" in str(e).lower()
            for e in res["errors"]
        )
        res["decision"] = {
            "action": "schedule_upgrade" if blocking else "apply_immediate_upgrade",
            "reason": "blocking schedule error" if blocking else "non-blocking schedule errors; prefer immediate",
        }
    else:
        res["decision"] = {"action": "apply_immediate_upgrade", "reason": "schedule warnings; prefer immediate"}
    return res


def _render_assessment_schedule_md(run_id: str, assessment: dict) -> str:
    lines: list[str] = []
    lines.append(f"# Assessment Schedule Check ({time.strftime('%Y-%m-%d %H:%M:%S')})")
    lines.append("")
    lines.append(f"- Run ID: {run_id}")
    lines.append(f"- Schedule path: {assessment.get('path','')}")
    lines.append(f"- Logic guide: {assessment.get('logic_guide','')}")
    lines.append(f"- Exists: {bool(assessment.get('exists', False))}")
    lines.append(f"- OK: {bool(assessment.get('ok', False))}")
    lines.append(f"- Assessments: {int(assessment.get('assessment_count') or 0)}")
    lines.append(f"- Interval-based entries: {int(assessment.get('interval_count') or 0)}")
    lines.append("")

    decision = assessment.get("decision") or {}
    if isinstance(decision, dict) and decision:
        lines.append("## Decision")
        lines.append("")
        lines.append(f"- action: {decision.get('action')}")
        lines.append(f"- reason: {decision.get('reason')}")
        lines.append("")

    tasks = assessment.get("upgrade_tasks") or []
    lines.append("## Upgrade tasks")
    lines.append("")
    if isinstance(tasks, list) and tasks:
        for t in tasks[:30]:
            lines.append(f"- {t}")
    else:
        lines.append("- (none)")
    lines.append("")

    errs = assessment.get("errors") or []
    lines.append("## Errors")
    lines.append("")
    if isinstance(errs, list) and errs:
        for e in errs[:30]:
            lines.append(f"- {e}")
    else:
        lines.append("- (none)")
    lines.append("")

    warns = assessment.get("warnings") or []
    lines.append("## Warnings")
    lines.append("")
    if isinstance(warns, list) and warns:
        for w in warns[:30]:
            lines.append(f"- {w}")
    else:
        lines.append("- (none)")

    return "\n".join(lines)


def main():
    root = Path(__file__).resolve().parent.parent
    py = str(root / "Scripts" / "python.exe")
    logs = root / "logs" / "tests"
    logs.mkdir(parents=True, exist_ok=True)

    run_id = f"{time.strftime('%Y%m%d_%H%M%S')}_{os.getpid()}"

    # Shared controls gating: avoid stealing controls from an active owner.
    # When controls are owned by someone else (and state isn't stale), we run
    # in passive-only mode (no UI automation) and optionally defer actions.
    orig_owner: str | None = None
    acquired_owner = False
    passive_only = False
    passive_reason = ""
    controls_state_snapshot: dict | None = None
    controls_state_stale: bool | None = None
    try:
        from src.control_state import get_controls_state, is_state_stale, set_controls_owner  # type: ignore
    except Exception:
        get_controls_state = None  # type: ignore
        is_state_stale = None  # type: ignore
        set_controls_owner = None  # type: ignore

    # Optional policy/config gating for expensive or environment-specific steps
    rules = {}
    try:
        rules_path = root / "config" / "policy_rules.json"
        rules = json.loads(rules_path.read_text(encoding="utf-8"))
    except Exception:
        rules = {}
    workflow_cfg = (rules.get("workflow") or {}) if isinstance(rules, dict) else {}
    controls_cfg = (rules.get("controls") or {}) if isinstance(rules, dict) else {}
    stale_after_s = float(controls_cfg.get("stale_after_s", 10.0) or 10.0)

    # Optional: reset state before any workflow start event.
    reset_before_start = bool(workflow_cfg.get("reset_before_start", True))
    reset_result: dict | None = None
    if reset_before_start:
        try:
            reset_out = logs / f"workflow_reset_{run_id}.json"
            reset_result = run_cmd(
                [py, "Scripts/reset_workflow_state.py", "--refresh-controls", "--out", str(reset_out)],
                root,
            )
        except Exception:
            reset_result = None

    # Optional: file-based coordination for multiple VS Code Copilot Chat tabs.
    if _chat_lanes_enabled(root):
        _append_chat_lane_event(
            root,
            type_="workflow_started",
            message="Test/Gather/Assess workflow started",
            run_id=run_id,
            lane="workflow",
        )

    # Agent Mode detection (best-effort). ui_state.json may be untracked in git.
    agent_mode = False
    try:
        v = os.environ.get("AI_CONTROLLER_AGENT_MODE")
        if v is not None:
            agent_mode = v.strip().lower() in {"1", "true", "yes", "on"}
        else:
            ui_state_path = root / "config" / "ui_state.json"
            if ui_state_path.exists():
                ui_state = json.loads(ui_state_path.read_text(encoding="utf-8")) or {}
                agent_mode = bool(ui_state.get("agent_mode", False))
    except Exception:
        agent_mode = False

    defer_interactions_when_agent_mode = bool(workflow_cfg.get("defer_interactions_when_agent_mode", True))
    env_defer = os.environ.get("AI_CONTROLLER_DEFER_INTERACTIONS_WHEN_AGENT_MODE")
    if env_defer is not None:
        defer_interactions_when_agent_mode = _as_bool(env_defer, defer_interactions_when_agent_mode)

    # If Agent Mode is active and policy says to defer interactions, run the workflow
    # in passive-only mode and queue any interactive steps instead of executing them.
    if agent_mode and defer_interactions_when_agent_mode:
        passive_only = True
        passive_reason = passive_reason or "agent_mode"

    # Best-effort scan for other active agents (Windows-only).
    try:
        active_agents = _detect_active_agent_processes(root)
    except Exception:
        active_agents = []

    if get_controls_state is not None and set_controls_owner is not None:
        try:
            st = get_controls_state(root) or {}
            controls_state_snapshot = dict(st) if isinstance(st, dict) else None
            owner = str(st.get("owner", "") or "")
            orig_owner = owner or None
            paused = bool(st.get("paused", False))
            stale = False
            try:
                if is_state_stale is not None:
                    stale = bool(is_state_stale(st, stale_after_s))
            except Exception:
                stale = False
            controls_state_stale = bool(stale)

            if paused and not stale:
                passive_only = True
                passive_reason = "controls_state.paused"
            elif owner and owner != "workflow_test" and not stale:
                passive_only = True
                passive_reason = f"controls owned by '{owner}'"
            else:
                # If Agent Mode is active and policy says to defer interactions,
                # do not claim controls ownership (we won't run interactive steps anyway).
                if not (agent_mode and defer_interactions_when_agent_mode):
                    set_controls_owner(root, "workflow_test")
                    acquired_owner = True
        except Exception:
            orig_owner = None
            acquired_owner = False
    enable_verify_phase = bool(workflow_cfg.get("enable_verify_phase", True))
    env_verify = os.environ.get("AI_CONTROLLER_ENABLE_VERIFY_PHASE")
    if env_verify is not None:
        enable_verify_phase = _as_bool(env_verify, enable_verify_phase)
    if passive_only:
        enable_verify_phase = False
    env_flag = os.environ.get("AI_CONTROLLER_ENABLE_COPILOT_APP_INTERACTION")
    if env_flag is not None:
        enable_copilot_app_step = env_flag.strip().lower() in {"1", "true", "yes"}
    else:
        enable_copilot_app_step = bool(workflow_cfg.get("enable_copilot_app_interaction", False))

    def _as_rel_artifact(p: Path) -> str:
        try:
            return str(p.resolve().relative_to(root.resolve()))
        except Exception:
            return str(p)

    def _parse_path_from_output(text: str, marker: str) -> Path | None:
        if not text:
            return None
        for line in reversed(text.splitlines()):
            s = line.strip()
            if not s:
                continue
            if s.startswith(marker):
                raw = s[len(marker):].strip()
                if raw.startswith(":"):
                    raw = raw[1:].strip()
                raw = raw.strip().strip('"').strip("'")
                if not raw:
                    continue
                try:
                    p = Path(raw)
                    if not p.is_absolute():
                        p = (root / p).resolve()
                    if p.exists():
                        return p
                except Exception:
                    continue
        return None

    def _find_latest_since(glob_rel: str, since_ts: float) -> Path | None:
        try:
            candidates = sorted((root / glob_rel).parent.glob(Path(glob_rel).name))
        except Exception:
            candidates = []
        best: Path | None = None
        best_mtime = 0.0
        for p in candidates:
            try:
                mt = p.stat().st_mtime
            except Exception:
                continue
            if mt >= (since_ts - 1.0) and mt >= best_mtime:
                best = p
                best_mtime = mt
        return best

    deferred_queue = root / "logs" / "actions" / "deferred_workflow_actions.jsonl"
    deferred_queue.parent.mkdir(parents=True, exist_ok=True)
    deferred_done_dir = root / "logs" / "actions" / "deferred_workflow_actions_done"
    deferred_done_dir.mkdir(parents=True, exist_ok=True)

    # Prevent repeated DEFERRED runs from bloating the queue with identical actions.
    # - dedupe_window_s: if an action id is already queued and not yet marked done, skip re-enqueue for this window.
    # - done_cooldown_s: if an action id was completed very recently, skip re-enqueue for this window.
    dedupe_window_s = float(workflow_cfg.get("deferred_queue_dedupe_window_s", 6 * 3600) or 0.0)
    done_cooldown_s = float(workflow_cfg.get("deferred_queue_done_cooldown_s", 10 * 60) or 0.0)
    try:
        v = os.environ.get("AI_CONTROLLER_DEFERRED_QUEUE_DEDUPE_WINDOW_S")
        if v is not None:
            dedupe_window_s = float(v)
    except Exception:
        pass
    try:
        v = os.environ.get("AI_CONTROLLER_DEFERRED_QUEUE_DONE_COOLDOWN_S")
        if v is not None:
            done_cooldown_s = float(v)
    except Exception:
        pass

    queued_cache: dict[str, float] = {}
    enqueue_meta: dict[str, dict] = {}

    def _parse_ts(s: str) -> float | None:
        s = (s or "").strip()
        if not s:
            return None
        try:
            # Matches the format we write in this repo: "%Y-%m-%d %H:%M:%S"
            return time.mktime(time.strptime(s, "%Y-%m-%d %H:%M:%S"))
        except Exception:
            return None

    def _seed_queued_cache(max_lines: int = 4000) -> None:
        if not deferred_queue.exists():
            return
        try:
            lines = deferred_queue.read_text(encoding="utf-8", errors="ignore").splitlines()
        except Exception:
            return
        tail = lines[-max_lines:] if len(lines) > max_lines else lines
        for line in tail:
            s = line.strip()
            if not s:
                continue
            try:
                obj = json.loads(s)
            except Exception:
                continue
            if not isinstance(obj, dict):
                continue
            action_id = str(obj.get("id") or "").strip()
            if not action_id:
                continue
            created_ts = _parse_ts(str(obj.get("created") or ""))
            if created_ts is None:
                created_ts = 0.0
            queued_cache[action_id] = max(float(queued_cache.get(action_id, 0.0)), float(created_ts))

    _seed_queued_cache()

    def _stable_action_id(name: str, cmd: list[str]) -> str:
        h = hashlib.sha256()
        h.update((name + "\n").encode("utf-8"))
        h.update("\u241f".join(cmd).encode("utf-8", errors="ignore"))
        return h.hexdigest()[:16]

    def _enqueue_deferred(name: str, cmd: list[str], reason: str) -> str:
        action_id = _stable_action_id(name, cmd)
        now = time.time()

        done_marker = deferred_done_dir / f"{action_id}.json"
        if done_cooldown_s > 0 and done_marker.exists():
            try:
                done_obj = json.loads(done_marker.read_text(encoding="utf-8", errors="ignore"))
                done_ts = _parse_ts(str((done_obj or {}).get("ts") or ""))
                if done_ts is not None and (now - done_ts) < done_cooldown_s:
                    enqueue_meta[action_id] = {"enqueued": False, "skip_reason": "done_cooldown"}
                    return action_id
            except Exception:
                pass

        if dedupe_window_s > 0:
            last_queued = float(queued_cache.get(action_id, 0.0) or 0.0)
            # If queued before and not yet completed, do not re-enqueue (prevents queue bloat across repeated DEFERRED runs).
            if last_queued > 0.0 and not done_marker.exists():
                enqueue_meta[action_id] = {"enqueued": False, "skip_reason": "already_queued"}
                return action_id

        entry = {
            "id": action_id,
            "run_id": run_id,
            "created": time.strftime("%Y-%m-%d %H:%M:%S"),
            "name": name,
            "cmd": cmd,
            "cwd": str(root),
            "reason": reason,
        }
        try:
            with deferred_queue.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            queued_cache[action_id] = float(now)
            enqueue_meta[action_id] = {"enqueued": True}
        except Exception:
            enqueue_meta[action_id] = {"enqueued": False, "skip_reason": "write_failed"}
            pass
        return action_id
    start_ts = time.time()
    # Start cleanup daemon for run window
    cleanup_daemon = subprocess.Popen([py, "Scripts/cleanup_daemon.py", "--interval", "5", "--retain", "5"], cwd=str(root))

    # Preflight settings snapshot (guaranteed, recorded once per run).
    preflight_warnings: list[str] = []
    preflight_suggestions: list[str] = []
    try:
        st = controls_state_snapshot or _load_json_file(root / "config" / "controls_state.json")
        paused_now = bool((st or {}).get("paused", False)) if isinstance(st, dict) else False
        owner_now = str((st or {}).get("owner", "") or "") if isinstance(st, dict) else ""
        if active_agents:
            names = sorted({str(a.get("name") or "") for a in active_agents if isinstance(a, dict)})
            preflight_warnings.append(
                "Active agent processes detected (process scan). "
                + ("; ".join([n for n in names if n])[:160] or "unknown")
            )
        if paused_now and not bool(controls_state_stale or False):
            preflight_warnings.append("controls_state.paused=true (live UI automation will be blocked/DEFERRED)")
            preflight_suggestions.append("If you intend to run live UI automation now: run task 'Controls: Unpause (paused=false)'.")
        if owner_now and (owner_now not in {"workflow_test", ""}) and not bool(controls_state_stale or False):
            preflight_warnings.append(f"controls_state.owner={owner_now!r} (another workflow owns controls)")
            preflight_suggestions.append("If ownership is stale/stuck: run task 'Release controls owner (force)' (use sparingly).")
        if (not paused_now) and (not owner_now):
            preflight_warnings.append("controls are currently unpaused and unowned (safe for live runs, but consider pausing when idle)")
            preflight_suggestions.append("After finishing live actions: run task 'Controls: Pause (paused=true)'.")

        if agent_mode and defer_interactions_when_agent_mode:
            preflight_warnings.append("Agent Mode is ON and defer policy is enabled (interactive steps will be queued as DEFERRED)")
            preflight_suggestions.append("For full interactive workflow coverage: set config/ui_state.json agent_mode=false (or set AI_CONTROLLER_AGENT_MODE=0 for the process) and rerun.")

        if controls_state_stale:
            preflight_warnings.append("controls_state is stale; ownership/activity is uncertain. Use process scan + refresh controls_state.")

        # Quick queue health at start (non-destructive).
        dq0 = summarize_deferred_queue(deferred_queue)
    except Exception:
        dq0 = {}

    preflight = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "agent_mode_active": bool(agent_mode),
        "defer_interactions_when_agent_mode": bool(defer_interactions_when_agent_mode),
        "passive_only": bool(passive_only),
        "passive_reason": str(passive_reason or ""),
        "enable_verify_phase": bool(enable_verify_phase),
        "enable_copilot_app_interaction": bool(enable_copilot_app_step),
        "reset_before_start": bool(reset_before_start),
        "reset_state_result": reset_result,
        "active_agent_processes": active_agents,
        "active_agent_process_count": int(len(active_agents)),
        "controls_state": controls_state_snapshot if isinstance(controls_state_snapshot, dict) else _load_json_file(root / "config" / "controls_state.json"),
        "controls_state_stale": bool(controls_state_stale) if controls_state_stale is not None else None,
        "controls_stale_after_s": float(stale_after_s),
        "deferred_queue_at_start": dq0,
        "warnings": preflight_warnings,
        "suggestions": preflight_suggestions,
    }

    summary = {
        "run_id": run_id,
        "started": time.strftime("%Y-%m-%d %H:%M:%S"),
        "steps": [],
        "artifacts": [],
        "status": "UNKNOWN",
        "pass": False,
        "successes": [],
        "errors": [],
        "warnings": []
    }

    # Attach preflight to workflow_info and as an explicit first step.
    try:
        wi = summary.setdefault("workflow_info", {})
        if isinstance(wi, dict):
            wi["preflight"] = preflight
        summary["steps"].append({
            "name": "preflight_settings",
            "pass": True,
            "warnings": preflight_warnings,
            "suggestions": preflight_suggestions,
        })
        if _chat_lanes_enabled(root) and preflight_warnings:
            _append_chat_lane_event(
                root,
                type_="preflight_warning",
                message="; ".join(preflight_warnings)[:800],
                run_id=run_id,
                lane="triage",
            )
    except Exception:
        pass

    finalized = False

    interrupted = False

    def _finalize(exit_code: int) -> int:
        nonlocal finalized
        # Ensure status/interaction context are always present, even on early exits.
        try:
            ctx = (summary.get("workflow_info") or {}).get("interaction_context")
            if isinstance(ctx, dict):
                ctx["agent_mode_active"] = bool(agent_mode)
                ctx["defer_interactions_when_agent_mode"] = bool(defer_interactions_when_agent_mode)
                ctx["passive_only"] = bool(passive_only)
                ctx["passive_reason"] = passive_reason
                ctx["interactive_allowed"] = bool((not passive_only) and (not (agent_mode and defer_interactions_when_agent_mode)))
        except Exception:
            pass

        try:
            status = str(summary.get("status") or "").strip().upper()
            if status not in {"PASS", "FAIL", "DEFERRED"}:
                summary["status"] = "PASS" if bool(summary.get("pass", False)) else "FAIL"
        except Exception:
            pass

        summary["exit_code"] = int(exit_code)
        summary["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
        if interrupted:
            summary["warnings"].append({"name": "workflow_interrupted", "note": "KeyboardInterrupt observed during workflow"})

        # Assess deferred queue health (non-destructive). Do NOT auto-prune.
        try:
            dq_summary = summarize_deferred_queue(deferred_queue)
            wi = summary.setdefault("workflow_info", {})
            if isinstance(wi, dict):
                wi["deferred_queue"] = dq_summary
            assess_out = logs / f"deferred_queue_assessment_{run_id}.md"
            lines: list[str] = []
            lines.append(f"# Deferred Queue Assessment ({time.strftime('%Y-%m-%d %H:%M:%S')})")
            lines.append("")
            lines.append(f"- Run ID: {run_id}")
            lines.append(f"- Queue path: {deferred_queue}")
            lines.append(f"- Exists: {bool(dq_summary.get('exists', False))}")
            lines.append(f"- Total lines: {int(dq_summary.get('total_lines') or 0)}")
            lines.append(f"- Parsed actions: {int(dq_summary.get('parsed') or 0)}")
            lines.append(f"- Unique actions (deduped): {int(dq_summary.get('unique_actions') or 0)}")
            rids = dq_summary.get("run_ids") or []
            if isinstance(rids, list) and rids:
                lines.append(f"- Queue spans run_ids: {len(rids)}")
            lines.append("")
            lines.append("## Top repeated actions")
            lines.append("")
            top = dq_summary.get("top_repeated") or []
            if isinstance(top, list) and top:
                for item in top:
                    if not isinstance(item, dict):
                        continue
                    lines.append(f"- {item.get('id')} queued={item.get('queued')} name={item.get('name')}")
            else:
                lines.append("- (none)")
            lines.append("")
            lines.append("## Notes")
            lines.append("")
            lines.append("- No auto-clean was performed.")
            lines.append(f"- To inspect: `Scripts/python.exe Scripts/run_deferred_workflow_actions.py --list --run-id {run_id}`")
            lines.append("- To prune manually (keeps latest per action id, creates backup): `Scripts/python.exe Scripts/run_deferred_workflow_actions.py --prune`")
            lines.append("")
            assess_out.write_text("\n".join(lines), encoding="utf-8")
            summary["steps"].append({"name": "assess_deferred_queue", "pass": True, "out": str(assess_out.name)})
            summary["artifacts"].append(str(assess_out.relative_to(root)))

            if _chat_lanes_enabled(root):
                _append_chat_lane_event(
                    root,
                    type_="deferred_queue_assessed",
                    message=f"Deferred queue assessed: unique={int(dq_summary.get('unique_actions') or 0)} total_lines={int(dq_summary.get('total_lines') or 0)} (see {assess_out.relative_to(root)})",
                    run_id=run_id,
                    lane="workflow",
                )
        except Exception as e:
            try:
                summary["steps"].append({"name": "assess_deferred_queue", "pass": False, "error": str(e)})
            except Exception:
                pass
        out = logs / f"workflow_summary_{run_id}.json"

        reco_out = logs / f"workflow_recommendations_{run_id}.md"
        try:
            reco_out.write_text(build_workflow_recommendations_md(summary), encoding="utf-8")
            summary["steps"].append({"name": "generate_workflow_recommendations", "pass": True, "out": str(reco_out.name)})
            summary["artifacts"].append(str(reco_out.relative_to(root)))
        except Exception as e:
            summary["steps"].append({"name": "generate_workflow_recommendations", "pass": False, "error": str(e)})

        # Validate assessment schedule (Agent Mode requirement) during workflow.
        try:
            sched = _assess_assessment_schedule(root)
            sched_out = logs / f"assessment_schedule_{run_id}.md"
            sched_out.write_text(_render_assessment_schedule_md(run_id, sched), encoding="utf-8")
            summary["steps"].append({"name": "assess_assessment_schedule", "pass": bool(sched.get("ok", False)), "out": str(sched_out.name)})
            summary["artifacts"].append(str(sched_out.relative_to(root)))
            if _chat_lanes_enabled(root):
                _append_chat_lane_event(
                    root,
                    type_="assessment_schedule_checked",
                    message=f"Assessment schedule checked: ok={bool(sched.get('ok', False))} entries={int(sched.get('assessment_count') or 0)} (see {sched_out.relative_to(root)})",
                    run_id=run_id,
                    lane="triage",
                )
        except Exception:
            pass

        # Optional: assess the file-based parallel chat lanes system.
        try:
            if _chat_lanes_enabled(root):
                a = _assess_chat_lanes(root, stale_minutes=30.0)
                assess_lanes_out = logs / f"chat_lanes_assessment_{run_id}.md"
                assess_lanes_out.write_text(_render_chat_lanes_assessment_md(run_id, a), encoding="utf-8")
                summary["steps"].append({"name": "assess_chat_lanes", "pass": True, "out": str(assess_lanes_out.name)})
                summary["artifacts"].append(str(assess_lanes_out.relative_to(root)))
                _append_chat_lane_event(
                    root,
                    type_="lanes_assessed",
                    message=f"Chat lanes assessed: events_total={int(a.get('events_total') or 0)} open_workflows={int(a.get('open_workflows') or 0)} (see {assess_lanes_out.relative_to(root)})",
                    run_id=run_id,
                    lane="triage",
                )
        except Exception:
            pass

        out.write_text(json.dumps(summary, indent=2), encoding="utf-8")

        # Notify chat lanes about workflow completion (best-effort).
        status = str(summary.get("status") or "").strip().upper()
        if status not in {"PASS", "FAIL", "DEFERRED"}:
            status = "PASS" if summary.get("pass") else "FAIL"
        if _chat_lanes_enabled(root):
            _append_chat_lane_event(
                root,
                type_="workflow_finished",
                message=f"Test/Gather/Assess finished: status={status} exit_code={int(exit_code)} (summary: {out.relative_to(root)})",
                run_id=run_id,
                lane="workflow",
            )

        try:
            run_cmd([py, "Scripts/prepare_context_pack.py"], root)
        except Exception:
            pass
        print(f"Workflow summary: {out}")
        # (status computed above)
        print("OVERALL:", status)
        print("EXIT CODE:", exit_code)
        try:
            cleanup_daemon.terminate()
            try:
                cleanup_daemon.wait(timeout=3)
            except Exception:
                pass
        except Exception:
            pass
        finalized = True
        return exit_code

    def _atexit_finalize() -> None:
        # Best-effort: if something (like stray Ctrl+C) interrupts the workflow,
        # ensure we still emit a summary and stop the cleanup daemon.
        nonlocal finalized
        if finalized:
            return
        try:
            summary.setdefault("pass", False)
            try:
                status = str(summary.get("status") or "").strip().upper()
                if status not in {"PASS", "FAIL", "DEFERRED"}:
                    summary["status"] = "PASS" if bool(summary.get("pass", False)) else "FAIL"
            except Exception:
                pass
            if "finished" not in summary:
                summary["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
            summary.setdefault("exit_code", 1)
            out = logs / f"workflow_summary_{run_id}.json"

            reco_out = logs / f"workflow_recommendations_{run_id}.md"
            try:
                reco_out.write_text(build_workflow_recommendations_md(summary), encoding="utf-8")
                summary.setdefault("steps", []).append({"name": "generate_workflow_recommendations", "pass": True, "out": str(reco_out.name)})
                summary.setdefault("artifacts", []).append(str(reco_out.relative_to(root)))
            except Exception:
                pass

            out.write_text(json.dumps(summary, indent=2), encoding="utf-8")

            try:
                run_cmd([py, "Scripts/prepare_context_pack.py"], root)
            except Exception:
                pass
        except Exception:
            pass
        try:
            cleanup_daemon.terminate()
        except Exception:
            pass
        finalized = True

    atexit.register(_atexit_finalize)
    # Ensure we restore the original owner on normal exit as well.
    def _restore_owner() -> None:
        try:
            if set_controls_owner is not None and acquired_owner:
                set_controls_owner(root, orig_owner)
        except Exception:
            pass
    atexit.register(_restore_owner)

    if agent_mode:
        summary["warnings"].append({"name": "agent_mode_active", "note": "Agent Mode active; interactive steps may be deferred"})
    if passive_only:
        summary["warnings"].append({"name": "passive_only", "note": f"Passive-only mode: {passive_reason}"})

    # IMPORTANT: do not overwrite workflow_info; it may already contain
    # preflight/deferred_queue metadata.
    wi = summary.setdefault("workflow_info", {})
    if isinstance(wi, dict):
        wi["interaction_context"] = {
            "agent_mode_active": bool(agent_mode),
            "interactive_allowed": bool((not passive_only) and (not (agent_mode and defer_interactions_when_agent_mode))),
            "defer_interactions_when_agent_mode": bool(defer_interactions_when_agent_mode),
            "passive_only": bool(passive_only),
            "passive_reason": passive_reason,
        }

    def _should_defer_interactive() -> tuple[bool, str]:
        if passive_only:
            return True, passive_reason or "passive_only"
        if agent_mode and defer_interactions_when_agent_mode:
            return True, "agent_mode"
        return False, ""

    def _run_or_defer_step(name: str, cmd: list[str], *, interactive: bool, report_marker: str | None = None, fallback_glob_rel: str | None = None) -> tuple[dict | None, bool]:
        defer, why = _should_defer_interactive() if interactive else (False, "")
        if defer:
            action_id = _enqueue_deferred(name, cmd, why)
            meta = enqueue_meta.get(action_id) if isinstance(enqueue_meta, dict) else None
            summary["steps"].append({
                "name": name,
                "pass": True,
                "deferred": True,
                "deferred_id": action_id,
                "queue_enqueued": bool((meta or {}).get("enqueued", True)) if isinstance(meta, dict) else True,
                "queue_skip_reason": str((meta or {}).get("skip_reason") or "") if isinstance(meta, dict) else "",
                "reason": why,
                "cmd": cmd,
            })
            summary["warnings"].append({"name": f"deferred:{name}", "reason": why, "id": action_id})
            return None, True

        step = run_cmd(cmd, root)
        ok = bool(step.get("ok", False))
        summary["steps"].append({"name": name, "result": step, "pass": ok})
        if ok:
            summary["successes"].append({"name": name, "seconds": step.get("seconds")})
        else:
            summary["errors"].append({"name": name, "returncode": step.get("returncode"), "stderr_tail": (step.get("stderr") or "")[-800:]})

        try:
            out_text = (step.get("stdout") or "") + "\n" + (step.get("stderr") or "")
            p: Path | None = None
            if report_marker:
                p = _parse_path_from_output(out_text, report_marker)
            if (p is None) and fallback_glob_rel:
                p = _find_latest_since(fallback_glob_rel, float(step.get("started_ts") or start_ts))
            if p is not None and p.exists():
                summary["artifacts"].append(_as_rel_artifact(p))
        except Exception:
            pass

        return step, ok

    # 1) Navigation Test (interactive)
    step_nav, step_nav_pass = _run_or_defer_step(
        "navigation_test",
        [py, "scripts/navigation_test.py"],
        interactive=True,
        report_marker="Report written:",
        fallback_glob_rel="logs/tests/navigation_test_*.json",
    )
    if step_nav is not None:
        try:
            nav_p = _parse_path_from_output((step_nav.get("stdout") or "") + "\n" + (step_nav.get("stderr") or ""), "Report written:")
            if nav_p is None:
                nav_p = _find_latest_since("logs/tests/navigation_test_*.json", float(step_nav.get("started_ts") or start_ts))
            if nav_p is not None and nav_p.exists():
                nav_obj = json.loads(nav_p.read_text(encoding="utf-8"))
                nav_ok = bool(((nav_obj.get("summary") or {}).get("ok")))
                if (not nav_ok) and step_nav_pass:
                    summary["errors"].append({"name": "navigation_test_soft_fail", "note": "nav report indicates failed steps", "file": _as_rel_artifact(nav_p)})
        except Exception:
            pass

    # 2) OCR Commit Test (interactive; appends to improvements.md)
    _, _ = _run_or_defer_step(
        "ocr_commit_test",
        [py, "Scripts/ocr_commit_test.py"],
        interactive=True,
        report_marker="OCR commit test report:",
        fallback_glob_rel="logs/tests/ocr_commit_test_*.json",
    )
    summary["artifacts"].append("projects/Self-Improve/improvements.md")

    # 2b) Gather chat evidence (interactive; explicit proof artifact)
    step_evd, pass_evd = _run_or_defer_step(
        "gather_chat_evidence",
        [py, "Scripts/gather_chat_evidence.py"],
        interactive=True,
        report_marker="Chat evidence:",
        fallback_glob_rel="logs/tests/chat_evidence_*.json",
    )
    if step_evd is not None and pass_evd:
        try:
            evp = _parse_path_from_output((step_evd.get("stdout") or "") + "\n" + (step_evd.get("stderr") or ""), "Chat evidence:")
            if evp is not None and evp.exists():
                summary["successes"].append({"name": "gather_chat_evidence", "file": _as_rel_artifact(evp)})
        except Exception:
            pass

    # 2c) VS Code chat keepalive (mouse/keyboard-driven button click probe)
    # This exercises the MultiWindowChatKeepalive + ChatButtonAnalyzer path,
    # which focuses VS Code windows, captures the chat ROI, and may move/click
    # the mouse on actionable chat buttons. This ensures the workflow includes
    # real input actions against Copilot UI elements, not just OCR reads.
    _, _ = _run_or_defer_step(
        "vscode_multi_keepalive",
        [py, "Scripts/vscode_multi_keepalive_daemon.py", "--interval-s", "4", "--max-cycles", "1"],
        interactive=True,
    )

    # 2d) Copilot app interaction test (foreground, useful prompt, OCR verify)
    if enable_copilot_app_step:
        _, _ = _run_or_defer_step(
            "copilot_app_interaction",
            [py, "Scripts/copilot_app_interaction_test.py"],
            interactive=True,
        )
    else:
        # When disabled via config or environment, record a skipped-but-pass step;
        # this keeps the workflow usable on machines without the Copilot app.
        summary["steps"].append({
            "name": "copilot_app_interaction",
            "pass": True,
            "skipped": True,
            "reason": "disabled via policy_rules.workflow.enable_copilot_app_interaction or AI_CONTROLLER_ENABLE_COPILOT_APP_INTERACTION",
        })
        summary["warnings"].append({"name": "copilot_app_interaction_skipped"})

    # 3) Observe & React (interactive; short)
    obs_log = str(Path("logs/tests/observe_react_workflow.jsonl"))
    step_obs, pass_obs = _run_or_defer_step(
        "observe_and_react",
        [py, "Scripts/observe_and_react.py", "--ticks", "12", "--interval-ms", "400", "--log", obs_log],
        interactive=True,
    )
    if step_obs is not None:
        summary["artifacts"].append(obs_log)

    # 3b) Scan error events and remediate if needed
    def read_error_events_since(root: Path, since_ts: float) -> list[dict]:
        import json, time
        p = root / "logs" / "errors" / "events.jsonl"
        ev = []
        if not p.exists():
            return ev
        try:
            with p.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                        ts_raw = obj.get("ts")
                        ts_num = None
                        # 1) epoch float/int (some scripts log time.time())
                        if isinstance(ts_raw, (int, float)):
                            ts_num = float(ts_raw)
                        # 2) ISO string with optional milliseconds + Z
                        if ts_num is None:
                            ts_str = str(ts_raw or "").strip().replace("T", " ")
                            # take first 19 chars YYYY-MM-DD HH:MM:SS
                            if len(ts_str) >= 19:
                                ts_core = ts_str[:19]
                                try:
                                    ttuple = time.strptime(ts_core, "%Y-%m-%d %H:%M:%S")
                                    ts_num = time.mktime(ttuple)
                                except Exception:
                                    ts_num = None
                        if ts_num is None:
                            continue
                        if ts_num >= since_ts:
                            ev.append(obj)
                    except Exception:
                        pass
        except Exception:
            return ev
        return ev

    errs_before = read_error_events_since(root, start_ts)
    summary["steps"].append({"name": "scan_error_events_before", "count": len(errs_before), "samples": errs_before[-3:]})
    if errs_before:
        # Attempt remediation by running observe_and_react again briefly
        step_fix = run_cmd([py, "Scripts/observe_and_react.py", "--ticks", "6", "--interval-ms", "400", "--log", obs_log], root)
        summary["steps"].append({"name": "remediate_errors", "result": step_fix, "pass": bool(step_fix.get("ok", False))})
    errs_after = read_error_events_since(root, start_ts)
    summary["steps"].append({"name": "scan_error_events_after", "count": len(errs_after), "samples": errs_after[-3:]})

    # 3b-2) Classify error-like events (not all events are failures)
    error_like = {
        "terminal_focus_failed",
        "terminal_type_failed",
        "text_input_wrong_field",
        "input_aborted_focus_changed",
        "input_aborted_not_ready",
        "vscode_chat_type_failed",
        "vscode_chat_enter_failed",
        "copilot_app_send_blocked",
        "copilot_app_send_misdirected",
        "copilot_app_verify_failed",
        "copilot_app_focus_failed_foreground",
        "copilot_app_type_failed",
        "copilot_app_enter_failed",
        "focus_thrash_detected",
        "navigation_step_failed",
    }
    warn_like = {
        "copilot_app_not_foreground_when_read",
    }
    err_like = []
    warn_hits = []
    for e in errs_after:
        et = (e.get("event") or e.get("type") or "").lower()
        if et in error_like:
            err_like.append(e)
        elif et in warn_like:
            warn_hits.append(e)
    summary["steps"].append({"name": "scan_error_events_errorlike", "count": len(err_like), "samples": err_like[-3:]})
    if err_like:
        summary["errors"].append({"name": "error_events_detected", "count": len(err_like), "samples": err_like[-3:]})
    summary["steps"].append({"name": "scan_error_events_warnlike", "count": len(warn_hits), "samples": warn_hits[-3:]})
    if warn_hits:
        summary["warnings"].append({"name": "warning_events_detected", "count": len(warn_hits), "samples": warn_hits[-3:]})

    # 3c) Assert no palette bypass/repeat events occurred this run
    def count_events(evts: list[dict], names: set[str]) -> int:
        n = 0
        for e in evts:
            et = (e.get("type") or e.get("event") or "").lower()
            if et in names:
                n += 1
        return n
    bad_events = {"palette_command_bypassed", "palette_command_repeated"}
    bad_count = count_events(errs_after, bad_events)
    summary["steps"].append({"name": "assert_palette_events_clean", "count": bad_count, "pass": bad_count == 0})
    if bad_count > 0:
        summary["errors"].append({"name": "assert_palette_events_clean", "bad_count": bad_count})

    # 4) Short Recording (auto-mark assessed)
    out_vid = str(Path("logs/screens/workflow_out.mp4"))
    rec = run_cmd([py, "scripts/monitor_live.py", "--seconds", "3", "--fps", "10", "--out", out_vid, "--backend", "mss", "--mark-assessed"], root)
    out_abs = root / out_vid
    marker_abs = root / (out_vid + ".assessed")
    made_video = out_abs.exists()
    made_marker = marker_abs.exists()
    step_rec_pass = bool(rec.get("ok", False) and made_video and made_marker)
    summary["steps"].append({"name": "monitor_live", "result": rec, "pass": step_rec_pass})
    if step_rec_pass:
        summary["successes"].append({"name": "monitor_live", "out": out_vid, "marker": out_vid + ".assessed"})
    else:
        summary["errors"].append({"name": "monitor_live", "returncode": rec.get("returncode"), "stderr_tail": (rec.get("stderr") or "")[-800:]})
    summary["artifacts"].append(out_vid)
    summary["artifacts"].append(out_vid + ".assessed")

    # 5) Cleanup pass (enforces retain_seconds-if-assessed for logs/screens)
    # Wait long enough to exceed retention (Windows timestamp resolution can be coarse).
    screen_retain_s = 5
    try:
        cleanup_cfg = (rules.get("cleanup") or {}) if isinstance(rules, dict) else {}
        for r in (cleanup_cfg.get("rules") or []):
            if isinstance(r, dict) and str(r.get("dir") or "") == "logs/screens":
                screen_retain_s = int(r.get("retain_seconds", screen_retain_s))
                break
    except Exception:
        screen_retain_s = 5

    try:
        time.sleep(max(6, screen_retain_s + 2))
    except KeyboardInterrupt:
        interrupted = True
        summary["pass"] = False
        summary["errors"].append({"name": "workflow_keyboardinterrupt", "note": "KeyboardInterrupt during retention sleep"})
        return _finalize(1)
    step_cleanup = run_cmd([py, "Scripts/cleanup_run.py"], root)
    deleted_video = not out_abs.exists()
    deleted_marker = not marker_abs.exists()

    # If not deleted yet, retry once; file might still be younger than threshold.
    if not deleted_video:
        try:
            time.sleep(2)
        except KeyboardInterrupt:
            interrupted = True
        step_cleanup_retry = run_cmd([py, "Scripts/cleanup_run.py"], root)
        summary["steps"].append({"name": "cleanup_retry", "result": step_cleanup_retry, "pass": bool(step_cleanup_retry.get("ok", False))})
        deleted_video = not out_abs.exists()
        deleted_marker = not marker_abs.exists()

    pass_cleanup = bool(step_cleanup.get("ok", False) and deleted_video)
    summary["steps"].append({"name": "cleanup", "result": step_cleanup, "pass": pass_cleanup, "deleted_video": deleted_video, "deleted_marker": deleted_marker})
    if pass_cleanup:
        summary["successes"].append({"name": "cleanup", "deleted_video": deleted_video, "deleted_marker": deleted_marker})
    else:
        summary["errors"].append({"name": "cleanup", "returncode": step_cleanup.get("returncode"), "stderr_tail": (step_cleanup.get("stderr") or "")[-800:]})

    # Final foreground check: ensure no disallowed windows remain (close if found)
    final_log = str(Path("logs/tests/observe_react_final.jsonl"))
    step_final, _ = _run_or_defer_step(
        "final_foreground_check",
        [py, "Scripts/observe_and_react.py", "--ticks", "6", "--interval-ms", "350", "--log", final_log],
        interactive=True,
    )
    if step_final is not None:
        summary["artifacts"].append(final_log)
    # Parse final log for close actions
    try:
        import json as _json
        p = root / final_log
        closes = 0
        close_failed = 0
        if p.exists():
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = _json.loads(line)
                    act = (obj.get("action") or "").lower()
                    if act == "close":
                        closes += 1
                    elif act == "close_failed":
                        close_failed += 1
                except Exception:
                    pass
        summary["steps"].append({"name": "final_close_summary", "closes": closes, "close_failed": close_failed, "pass": close_failed == 0})
        if close_failed > 0:
            summary["errors"].append({"name": "final_close_summary", "close_failed": close_failed})
    except Exception:
        pass

    # Context pack: generate a Copilot-ready summary of project, objectives, and this run
    step_ctx = run_cmd([py, "Scripts/prepare_context_pack.py"], root)
    summary["steps"].append({"name": "prepare_context_pack", "result": step_ctx, "pass": bool(step_ctx.get("ok", False))})
    if step_ctx.get("ok", False):
        summary["successes"].append({"name": "prepare_context_pack"})
        summary["artifacts"].append("Copilot_Attachments/ContextPack_Current.md")
    else:
        summary["errors"].append({"name": "prepare_context_pack", "returncode": step_ctx.get("returncode"), "stderr_tail": (step_ctx.get("stderr") or "")[-800:]})

    # Learn phase: persist lessons/solutions from this run
    step_learn = run_cmd([py, "Scripts/learn_from_run.py", "--since-ts", summary["started"]], root)
    summary["steps"].append({"name": "learn_from_run", "result": step_learn, "pass": bool(step_learn.get("ok", False))})
    if step_learn.get("ok", False):
        summary["successes"].append({"name": "learn_from_run"})
        summary["artifacts"].append("projects/Self-Improve/lessons.jsonl")
        summary["artifacts"].append("projects/Self-Improve/solutions.jsonl")
        if (root / "projects" / "Self-Improve" / "error_commands.json").exists():
            summary["artifacts"].append("projects/Self-Improve/error_commands.json")
    else:
        summary["errors"].append({"name": "learn_from_run", "returncode": step_learn.get("returncode"), "stderr_tail": (step_learn.get("stderr") or "")[-800:]})

    # Improve phase: apply module improvements based on policy (remove banned commands from source)
    step_improve = run_cmd([py, "Scripts/improve_modules_from_policy.py"], root)
    summary["steps"].append({"name": "improve_modules", "result": step_improve, "pass": bool(step_improve.get("ok", False))})
    if step_improve.get("ok", False):
        summary["successes"].append({"name": "improve_modules"})
    else:
        summary["errors"].append({"name": "improve_modules", "returncode": step_improve.get("returncode"), "stderr_tail": (step_improve.get("stderr") or "")[-800:]})

    # Verify phase: re-run navigation and assert no palette errors since improvement
    verify_start = time.time()
    if enable_verify_phase:
        _, _ = _run_or_defer_step(
            "navigation_test_verify",
            [py, "scripts/navigation_test.py"],
            interactive=True,
            report_marker="Report written:",
            fallback_glob_rel="logs/tests/navigation_test_*.json",
        )
    else:
        summary["steps"].append({"name": "navigation_test_verify", "pass": True, "skipped": True, "reason": "disabled via policy_rules.workflow.enable_verify_phase or passive_only"})
    errs_verify = []
    try:
        errs_verify = read_error_events_since(root, verify_start)
    except Exception:
        errs_verify = []
    bad_verify = 0
    for e in errs_verify:
        et = (e.get("type") or e.get("event") or "").lower()
        if et in {"palette_command_bypassed", "palette_command_repeated"}:
            bad_verify += 1
    summary["steps"].append({"name": "assert_palette_events_clean_verify", "count": bad_verify, "pass": bad_verify == 0})
    if bad_verify > 0:
        summary["errors"].append({"name": "assert_palette_events_clean_verify", "bad_count": bad_verify})

    # Final pass computation (after Learn/Improve/Verify)
    explicit_pass = [s for s in summary["steps"] if "pass" in s]
    steps_ok = all(bool(s.get("pass", False)) for s in explicit_pass) if explicit_pass else True
    no_errors = True
    for s in summary["steps"]:
        if s.get("name") == "scan_error_events_errorlike" and int(s.get("count") or 0) > 0:
            no_errors = False
            break
    for s in summary["steps"]:
        if s.get("name") == "assert_palette_events_clean" and not bool(s.get("pass", False)):
            no_errors = False
            break
    for s in summary["steps"]:
        if s.get("name") == "assert_palette_events_clean_verify" and not bool(s.get("pass", False)):
            no_errors = False
            break
    # Update interaction context now that we know what we deferred.
    try:
        deferred_steps = [s for s in (summary.get("steps") or []) if bool((s or {}).get("deferred", False))]
        ctx = (summary.get("workflow_info") or {}).get("interaction_context")
        if isinstance(ctx, dict):
            ctx["interactions_deferred"] = bool(deferred_steps)
            ctx["deferred_count"] = int(len(deferred_steps))
            if bool(deferred_steps):
                ctx.setdefault(
                    "defer_reason",
                    "agent_mode" if (agent_mode and defer_interactions_when_agent_mode) else (passive_reason or "unknown"),
                )
    except Exception:
        pass

    deferred_steps = [s for s in (summary.get("steps") or []) if bool((s or {}).get("deferred", False))]
    interactions_deferred = bool(deferred_steps)

    # Recommend a best first deferred action (best-effort heuristic).
    try:
        wi = summary.setdefault("workflow_info", {})
        if isinstance(wi, dict):
            wi["recommended_deferred_action"] = _choose_recommended_deferred_action(root, str(summary.get("run_id") or ""), deferred_steps)
    except Exception:
        pass

    # Status semantics:
    # - PASS: workflow executed and all checks passed
    # - DEFERRED: workflow executed safely but skipped interactive steps (queued)
    # - FAIL: at least one check/step failed or error-like events were detected
    summary["pass"] = bool(steps_ok and no_errors)
    if not summary["pass"]:
        summary["status"] = "FAIL"
    else:
        summary["status"] = "DEFERRED" if interactions_deferred else "PASS"

    # If we deferred any interactive actions, surface the queue file.
    try:
        if deferred_queue.exists():
            summary["artifacts"].append(_as_rel_artifact(deferred_queue))
    except Exception:
        pass

    # Exit behavior:
    # - PASS: 0
    # - FAIL: non-zero
    # - DEFERRED: 0 (not a PASS, but not a failure either)
    if str(summary.get("status") or "").upper() == "DEFERRED":
        return _finalize(0)
    return _finalize(0 if summary["pass"] else 1)


if __name__ == "__main__":
    sys.exit(main())
