from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from pathlib import Path


def _root() -> Path:
    return Path(__file__).resolve().parent.parent


def _append_lane_event(root: Path, type_: str, message: str, lane: str | None = None) -> None:
    d = root / "projects" / "Chat_Lanes"
    d.mkdir(parents=True, exist_ok=True)
    notif = d / "notifications.jsonl"
    evt = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "type": type_,
        "message": message,
    }
    if lane:
        evt["lane"] = lane
    with notif.open("a", encoding="utf-8") as f:
        f.write(json.dumps(evt, ensure_ascii=False) + "\n")


def _detect_processes() -> list[dict]:
    if os.name != "nt":
        return []
    try:
        cmd = (
            "$procs = Get-CimInstance Win32_Process "
            "| Where-Object { $_.CommandLine -like '*AI_Coder_Controller*' } "
            "| Select-Object ProcessId,Name,CommandLine; "
            "$procs | ConvertTo-Json -Depth 3"
        )
        proc = subprocess.run(["powershell", "-NoProfile", "-Command", cmd], capture_output=True, text=True, check=False)
        raw = (proc.stdout or "").strip()
        if not raw:
            return []
        data = json.loads(raw)
        if isinstance(data, dict):
            return [data]
        if isinstance(data, list):
            return [d for d in data if isinstance(d, dict)]
        return []
    except Exception:
        return []


def _count_lanes(root: Path) -> int:
    lanes_dir = root / "projects" / "Chat_Lanes"
    if not lanes_dir.exists():
        return 0
    lanes = list(lanes_dir.glob("lane_*.md"))
    return len(lanes)


def _is_orchestrator_running(procs: list[dict]) -> bool:
    for p in procs:
        cl = str(p.get("CommandLine") or "").lower()
        if "orchestrator_agent.py" in cl or "src.main" in cl:
            return True
    return False


def _stop_user_activity_monitors() -> bool:
    if os.name != "nt":
        return False
    try:
        cmd = (
            "$procs = Get-CimInstance Win32_Process "
            "| Where-Object { $_.CommandLine -like '*user_activity_monitor.py*' }; "
            "if ($procs) { $procs | ForEach-Object { Stop-Process -Id $_.ProcessId -Force } }; "
            "'ok'"
        )
        proc = subprocess.run(["powershell", "-NoProfile", "-Command", cmd], capture_output=True, text=True, check=False)
        return "ok" in (proc.stdout or "")
    except Exception:
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description="Start the simultaneous workflow system (lanes + orchestrator).")
    ap.add_argument("--init-lanes", action="store_true", help="Initialize Chat Lanes if missing")
    ap.add_argument("--launch-orchestrator", action="store_true", help="Launch orchestrator agent if not running")
    ap.add_argument("--open-agent-tabs", action="store_true", help="Open Copilot Chat tabs for Agent Mode lanes")
    ap.add_argument("--tab-count", type=int, default=0, help="Override how many chat tabs to open")
    ap.add_argument("--launch-user-monitor", action="store_true", help="Launch user activity monitor (pause on input)")
    ap.add_argument("--reset-controls", action="store_true", help="Reset controls state before start (auto-unpause + refresh)")
    ap.add_argument("--dry-run", action="store_true", help="Do not launch anything; only report")
    args = ap.parse_args()

    root = _root()
    py = str(root / "Scripts" / "python.exe")
    logs = root / "logs" / "tests"
    logs.mkdir(parents=True, exist_ok=True)

    report = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "lanes_initialized": False,
        "lanes_init_log": None,
        "lanes_init_error": None,
        "orchestrator_running": False,
        "orchestrator_launched": False,
        "orchestrator_pid": None,
        "open_agent_tabs_requested": bool(args.open_agent_tabs),
        "open_agent_tabs_count": 0,
        "open_agent_tabs_log": None,
        "open_agent_tabs_success": None,
        "open_agent_tabs_stdout": None,
        "open_agent_tabs_stderr": None,
        "user_monitor_launched": False,
        "user_monitor_pid": None,
        "user_monitor_existing": False,
        "controls_reset": False,
        "controls_reset_log": None,
        "controls_reset_stdout": None,
        "controls_reset_stderr": None,
        "user_monitor_stopped": False,
        "dry_run": bool(args.dry_run),
    }

    lanes_dir = root / "projects" / "Chat_Lanes"
    if args.init_lanes and not lanes_dir.exists():
        if not args.dry_run:
            proc = subprocess.run(
                [py, "Scripts/parallel_chat_lanes.py", "init"],
                cwd=str(root),
                capture_output=True,
                text=True,
                check=False,
            )
            report["lanes_init_log"] = (proc.stdout or "").strip() or None
            if proc.returncode == 0:
                report["lanes_initialized"] = True
            else:
                report["lanes_initialized"] = False
                report["lanes_init_error"] = (proc.stderr or proc.stdout or "").strip() or None
                _append_lane_event(
                    root,
                    "lanes_init_failed",
                    f"Chat lanes init failed (exit {proc.returncode})"
                    + (f": {report['lanes_init_error'][:180]}" if report["lanes_init_error"] else ""),
                    lane="workflow",
                )
        else:
            report["lanes_initialized"] = True
    elif args.init_lanes:
        report["lanes_initialized"] = True

    if args.reset_controls:
        if not args.dry_run:
            report["user_monitor_stopped"] = bool(_stop_user_activity_monitors())
        if not args.dry_run:
            try:
                proc = subprocess.run(
                    [py, "Scripts/reset_workflow_state.py", "--force-unpause", "--refresh-controls"],
                    cwd=str(root),
                    capture_output=True,
                    text=True,
                    check=False,
                )
                report["controls_reset_stdout"] = (proc.stdout or "").strip() or None
                report["controls_reset_stderr"] = (proc.stderr or "").strip() or None
                if proc.returncode == 0:
                    report["controls_reset"] = True
                    if proc.stdout:
                        report["controls_reset_log"] = proc.stdout.strip().splitlines()[-1]
                else:
                    report["controls_reset"] = False
                    failure_msg = (proc.stderr or proc.stdout or "").strip()
                    report["controls_reset_log"] = failure_msg or None
                    _append_lane_event(
                        root,
                        "controls_reset_failed",
                        f"Controls reset failed (exit {proc.returncode})",
                        lane="workflow",
                    )
            except Exception as exc:
                report["controls_reset"] = False
                report["controls_reset_log"] = str(exc)
                _append_lane_event(root, "controls_reset_failed", "Controls reset raised an exception", lane="workflow")
        else:
            report["controls_reset"] = True
            report["controls_reset_log"] = None

    procs = _detect_processes()
    report["orchestrator_running"] = _is_orchestrator_running(procs)

    if args.launch_orchestrator and not report["orchestrator_running"]:
        if not args.dry_run:
            p = subprocess.Popen([py, "Scripts/orchestrator_agent.py"], cwd=str(root))
            report["orchestrator_launched"] = True
            report["orchestrator_pid"] = p.pid
        else:
            report["orchestrator_launched"] = True
            report["orchestrator_pid"] = None

    def _has_user_monitor(processes: list[dict]) -> bool:
        for proc in processes:
            cmd = str(proc.get("CommandLine") or "").lower()
            if "user_activity_monitor.py" in cmd:
                return True
        return False

    if args.launch_user_monitor:
        if not args.dry_run:
            procs = _detect_processes()
        existing_monitor = _has_user_monitor(procs)
        report["user_monitor_existing"] = bool(existing_monitor)
        if existing_monitor:
            report["user_monitor_launched"] = False
        else:
            if not args.dry_run:
                try:
                    p = subprocess.Popen([py, "Scripts/user_activity_monitor.py", "--popup"], cwd=str(root))
                    report["user_monitor_launched"] = True
                    report["user_monitor_pid"] = p.pid
                except Exception:
                    report["user_monitor_launched"] = False
            else:
                report["user_monitor_launched"] = True
                report["user_monitor_pid"] = None

    if args.open_agent_tabs:
        lane_count = _count_lanes(root)
        desired = int(args.tab_count or 0) or lane_count or 1
        report["open_agent_tabs_count"] = desired
        if not args.dry_run:
            try:
                proc = subprocess.run(
                    [py, "Scripts/open_agent_mode_tabs.py", "--count", str(desired)],
                    cwd=str(root),
                    capture_output=True,
                    text=True,
                    check=False,
                )
                report["open_agent_tabs_stdout"] = (proc.stdout or "").strip() or None
                report["open_agent_tabs_stderr"] = (proc.stderr or "").strip() or None
                if proc.returncode == 0:
                    log_path = (proc.stdout or "").strip().splitlines()[-1] if proc.stdout else None
                    report["open_agent_tabs_log"] = log_path
                    report["open_agent_tabs_success"] = True
                else:
                    report["open_agent_tabs_success"] = False
                    report["open_agent_tabs_log"] = None
            except Exception as exc:
                report["open_agent_tabs_log"] = None
                report["open_agent_tabs_success"] = False
                report["open_agent_tabs_stdout"] = None
                report["open_agent_tabs_stderr"] = str(exc)
        else:
            report["open_agent_tabs_log"] = None
            report["open_agent_tabs_success"] = None

    # Notify lanes
    try:
        _append_lane_event(root, "simultaneous_system_start", "Simultaneous workflow system start requested", lane="workflow")
        if args.open_agent_tabs:
            if report["open_agent_tabs_success"] is True:
                _append_lane_event(
                    root,
                    "agent_tabs_requested",
                    f"Requested opening {report['open_agent_tabs_count']} Agent Mode chat tabs",
                    lane="workflow",
                )
            elif report["open_agent_tabs_success"] is False:
                err_msg = report["open_agent_tabs_stderr"] or report["open_agent_tabs_stdout"] or "unknown error"
                _append_lane_event(
                    root,
                    "agent_tabs_failed",
                    f"Failed to open Agent Mode chat tabs ({err_msg[:180]})",
                    lane="workflow",
                )
        if args.launch_user_monitor:
            if report["user_monitor_existing"]:
                _append_lane_event(root, "user_monitor_existing", "User activity monitor already running", lane="workflow")
            elif report["user_monitor_launched"]:
                _append_lane_event(root, "user_monitor_launched", "User activity monitor started", lane="workflow")
        if report["orchestrator_running"]:
            _append_lane_event(root, "orchestrator_detected", "Orchestrator agent already running", lane="workflow")
        elif report["orchestrator_launched"]:
            _append_lane_event(root, "orchestrator_launched", "Orchestrator agent launched", lane="workflow")
        else:
            _append_lane_event(root, "orchestrator_missing", "Orchestrator agent not running", lane="workflow")
    except Exception:
        pass

    out_path = logs / f"start_simultaneous_workflow_{time.strftime('%Y%m%d_%H%M%S')}.json"
    out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(str(out_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
