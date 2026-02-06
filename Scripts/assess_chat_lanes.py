from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path


def _root() -> Path:
    return Path(__file__).resolve().parent.parent


def _ts() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _parse_ts(ts: str) -> float | None:
    """Best-effort parse of our own %Y-%m-%d %H:%M:%S timestamps."""
    try:
        return time.mktime(time.strptime(str(ts or "").strip(), "%Y-%m-%d %H:%M:%S"))
    except Exception:
        return None


def _latest_workflow_run_id(root: Path) -> str:
    logs = root / "logs" / "tests"
    try:
        candidates = sorted(logs.glob("workflow_summary_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not candidates:
            return ""
        return candidates[0].stem.replace("workflow_summary_", "", 1)
    except Exception:
        return ""


def _agent_mode_active(root: Path) -> tuple[bool, str]:
    v = os.environ.get("AI_CONTROLLER_AGENT_MODE")
    if v is not None:
        return v.strip().lower() in {"1", "true", "yes", "on"}, "env"
    ui_state_path = root / "config" / "ui_state.json"
    if ui_state_path.exists():
        try:
            ui = json.loads(ui_state_path.read_text(encoding="utf-8")) or {}
        except Exception:
            ui = {}
        if isinstance(ui, dict):
            return bool(ui.get("agent_mode", False)), "config"
    return False, "unknown"


def assess_chat_lanes(root: Path, *, stale_minutes: float = 30.0) -> dict:
    lanes_dir = root / "projects" / "Chat_Lanes"
    notif = lanes_dir / "notifications.jsonl"

    now = time.time()
    stale_after_s = max(0.0, float(stale_minutes)) * 60.0

    agent_mode, agent_mode_source = _agent_mode_active(root)

    res: dict = {
        "ok": True,
        "ts": _ts(),
        "lanes_dir": str(lanes_dir),
        "notifications": str(notif),
        "notifications_exists": bool(notif.exists()),
        "agent_mode_active": bool(agent_mode),
        "agent_mode_source": str(agent_mode_source),
        "events_total": 0,
        "events_by_type": {},
        "events_by_lane": {},
        "last_event_ts": "",
        "last_event_age_s": None,
        "open_workflows": 0,
        "lane_files": [],
        "stale_minutes": float(stale_minutes),
        "stale_lanes": [],
        "notes": [],
        "recommendations": [],
    }

    if not lanes_dir.exists():
        res["ok"] = False
        res["notes"].append("Chat_Lanes directory missing.")
        res["recommendations"].append("Run: Scripts/python.exe Scripts/parallel_chat_lanes.py init")
        return res

    # Load notifications
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

    # Aggregate
    events_by_type: dict[str, int] = {}
    events_by_lane: dict[str, int] = {}
    open_workflows = 0

    last_event_epoch: float | None = None
    last_event_ts = ""

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

        ets = str(e.get("ts") or "")
        ep = _parse_ts(ets)
        if ep is not None and (last_event_epoch is None or ep > last_event_epoch):
            last_event_epoch = ep
            last_event_ts = ets

    res["events_by_type"] = dict(sorted(events_by_type.items(), key=lambda kv: (-kv[1], kv[0])))
    res["events_by_lane"] = dict(sorted(events_by_lane.items(), key=lambda kv: (-kv[1], kv[0])))
    res["open_workflows"] = int(open_workflows)

    if last_event_epoch is not None:
        res["last_event_ts"] = last_event_ts
        res["last_event_age_s"] = round(max(0.0, now - last_event_epoch), 1)

    # Lane file freshness (based on mtime)
    lane_paths = sorted([p for p in lanes_dir.glob("lane_*.md") if p.is_file()], key=lambda p: p.name)
    stale_lanes: list[dict] = []
    lane_files: list[dict] = []
    for p in lane_paths:
        try:
            age_s = max(0.0, now - p.stat().st_mtime)
        except Exception:
            age_s = 0.0
        item = {
            "path": str(p),
            "name": p.name,
            "age_s": round(age_s, 1),
        }
        lane_files.append(item)
        if stale_after_s > 0 and age_s >= stale_after_s:
            stale_lanes.append(item)

    res["lane_files"] = lane_files
    res["stale_lanes"] = stale_lanes

    # Recommendations
    if not notif.exists():
        res["recommendations"].append("Create notifications log by running: Scripts/python.exe Scripts/parallel_chat_lanes.py init")
    elif res["events_total"] == 0:
        res["recommendations"].append("notifications.jsonl is empty; verify workflows are emitting lane events or run the workflow once.")

    if res["open_workflows"] > 0:
        res["recommendations"].append(
            "A workflow appears to be in progress (workflow_started without workflow_finished). Avoid running multiple live UI runners; use deferred queue + lane notes instead."
        )

    if stale_lanes:
        names = ", ".join(x.get("name", "") for x in stale_lanes[:6])
        res["recommendations"].append(
            f"Some lane files look stale (no edits for >= {stale_minutes:g} min): {names}. Open each lane in a dedicated Copilot Chat tab and append a short status update."
        )

    if res["events_total"] > 2000:
        res["recommendations"].append(
            "notifications.jsonl is large; consider archiving/rotating it periodically to keep reviews fast (manual-only)."
        )

    return res


def render_md(assessment: dict) -> str:
    lines: list[str] = []
    lines.append(f"# Chat Lanes Assessment ({assessment.get('ts','')})")
    lines.append("")
    lines.append(f"- Lanes dir: {assessment.get('lanes_dir','')}")
    lines.append(f"- Notifications: {assessment.get('notifications','')}")
    lines.append(f"- Notifications exists: {bool(assessment.get('notifications_exists', False))}")
    lines.append(
        f"- Agent Mode (assessment process): {'ON' if bool(assessment.get('agent_mode_active')) else 'OFF'}"
        f" (source={assessment.get('agent_mode_source','unknown')})"
    )
    lines.append(f"- Events total: {int(assessment.get('events_total') or 0)}")
    lines.append(f"- Open workflows (best-effort): {int(assessment.get('open_workflows') or 0)}")
    if assessment.get("last_event_ts"):
        lines.append(f"- Last event: {assessment.get('last_event_ts')} (age_s={assessment.get('last_event_age_s')})")
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
    lanes = assessment.get("lane_files") or []
    if isinstance(lanes, list) and lanes:
        for it in lanes[:30]:
            if not isinstance(it, dict):
                continue
            lines.append(f"- {it.get('name')} age_s={it.get('age_s')}")
    else:
        lines.append("- (no lane_*.md files)")
    lines.append("")

    stale = assessment.get("stale_lanes") or []
    if isinstance(stale, list) and stale:
        lines.append("## Stale lanes")
        lines.append("")
        for it in stale[:30]:
            if not isinstance(it, dict):
                continue
            lines.append(f"- {it.get('name')} age_s={it.get('age_s')}")
        lines.append("")

    lines.append("## Recommendations")
    lines.append("")
    recs = assessment.get("recommendations") or []
    if isinstance(recs, list) and recs:
        for r in recs[:30]:
            lines.append(f"- {r}")
    else:
        lines.append("- No issues detected.")

    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description="Assess the health of the parallel Chat Lanes coordination system.")
    ap.add_argument("--stale-minutes", type=float, default=30.0, help="Mark lanes stale if not edited for this long")
    ap.add_argument("--run-id", default="", help="Optional workflow run_id to include in output filename")
    ap.add_argument("--out", default="", help="Optional output path (.md). Defaults to logs/tests/chat_lanes_assessment_<run_id>.md or timestamp.")
    ap.add_argument("--every-s", type=float, default=0.0, help="If >0, re-run assessment on this interval (seconds)")
    ap.add_argument("--count", type=int, default=1, help="Number of assessment iterations (<=0 for infinite)")
    args = ap.parse_args()

    root = _root()

    interval_s = float(args.every_s or 0.0)
    total = int(args.count)
    if total == 0:
        total = -1
    if total < 0:
        total = -1

    i = 0
    while True:
        i += 1
        run_id = str(args.run_id or "").strip() or _latest_workflow_run_id(root)

        assessment = assess_chat_lanes(root, stale_minutes=float(args.stale_minutes))

        if args.out:
            out_path = (root / args.out).resolve()
        else:
            logs = root / "logs" / "tests"
            logs.mkdir(parents=True, exist_ok=True)
            if run_id:
                out_path = logs / f"chat_lanes_assessment_{run_id}.md"
            else:
                out_path = logs / f"chat_lanes_assessment_{time.strftime('%Y%m%d_%H%M%S')}.md"

        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(render_md(assessment), encoding="utf-8")
        print(str(out_path))

        if total > 0 and i >= total:
            break
        if interval_s <= 0:
            break
        time.sleep(max(0.1, interval_s))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
