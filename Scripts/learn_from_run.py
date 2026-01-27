from __future__ import annotations
import argparse
import json
import re
import time
from pathlib import Path


def tail_lines(path: Path, max_lines: int = 400) -> list[str]:
    if not path.exists():
        return []
    data = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    return data[-max_lines:]


def write_jsonl(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def write_json(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description="Learn from run: extract failed commands and record lessons/solutions")
    ap.add_argument("--since-ts", type=str, default="", help="Optional YYYY-MM-DD HH:MM:SS; include events at or after this time")
    args = ap.parse_args()

    root = Path(__file__).resolve().parent.parent
    errors_path = root / "logs" / "errors" / "events.jsonl"
    commit_log = root / "logs" / "actions" / "copilot_commit_safe.log"
    lessons_jsonl = root / "projects" / "Self-Improve" / "lessons.jsonl"
    solutions_jsonl = root / "projects" / "Self-Improve" / "solutions.jsonl"
    error_commands_json = root / "projects" / "Self-Improve" / "error_commands.json"
    policy_path = root / "config" / "policy_rules.json"

    # Parse error events
    since_ts_num = 0.0
    if args.since_ts:
        try:
            t = time.strptime(args.since_ts.replace("T"," "), "%Y-%m-%d %H:%M:%S")
            since_ts_num = time.mktime(t)
        except Exception:
            since_ts_num = 0.0
    events = []
    if errors_path.exists():
        for line in errors_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            ts_s = (obj.get("ts") or "").replace("T"," ")
            try:
                tt = time.strptime(ts_s, "%Y-%m-%d %H:%M:%S")
                ts_n = time.mktime(tt)
            except Exception:
                ts_n = 0.0
            if since_ts_num and ts_n < since_ts_num:
                continue
            events.append(obj)

    # Extract palette commands from commit log
    palette_cmds: set[str] = set()
    for ln in tail_lines(commit_log, 600):
        m = re.search(r"palette command='([^']+)'", ln)
        if m:
            palette_cmds.add(m.group(1))

    # Extract command-like strings from error events (for a consolidated artifact)
    event_cmds: set[str] = set()
    for e in events:
        # Common top-level keys
        for k in ("command", "cmd", "command_preview", "prompt", "text"):
            v = e.get(k)
            if isinstance(v, str) and v.strip():
                event_cmds.add(v.strip())
        # Nested data payload
        data = e.get("data") or {}
        if isinstance(data, dict):
            for k in ("command", "cmd", "command_preview", "prompt", "text"):
                v = data.get(k)
                if isinstance(v, str) and v.strip():
                    event_cmds.add(v.strip())

    # Write consolidated error-commands artifact for workflow evidence & downstream improvements
    try:
        ts_now = time.strftime("%Y-%m-%d %H:%M:%S")
        all_cmds = sorted({*palette_cmds, *event_cmds})
        err_types = sorted({str((e.get("type") or e.get("event") or "")).lower() for e in events if (e.get("type") or e.get("event"))})
        write_json(
            error_commands_json,
            {
                "ts": ts_now,
                "since_ts": args.since_ts or "",
                "num_events": len(events),
                "event_types": err_types,
                "palette_commands": sorted(palette_cmds),
                "event_commands": sorted(event_cmds),
                "all_commands": all_cmds,
                "evidence": {
                    "errors": str(errors_path.relative_to(root)) if errors_path.exists() else "",
                    "commit_log": str(commit_log.relative_to(root)) if commit_log.exists() else "",
                },
            },
        )
    except Exception:
        pass

    # Record lessons for each event type with related commands
    ts_now = time.strftime("%Y-%m-%d %H:%M:%S")
    for ev in events:
        ev_type = (ev.get("type") or ev.get("event") or "unknown")
        ev_msg = ev.get("message") or ""
        data = ev.get("data") or {}
        lesson = {
            "ts": ts_now,
            "kind": "failure",
            "event_type": ev_type,
            "message": ev_msg,
            "data": data,
            "failed_commands": sorted(palette_cmds),
            "evidence": {
                "errors": str(errors_path.relative_to(root)) if errors_path.exists() else "",
                "commit_log": str(commit_log.relative_to(root)) if commit_log.exists() else ""
            },
            "solution_refs": ["vscode.foreground_process_gate","chat.ocr_readiness_gate","palette.hygiene_esc"],
        }
        write_jsonl(lessons_jsonl, lesson)

    # If we observed focus/browser or palette-bypass errors, add related palette commands to policy.banned
    try:
        err_types = {(e.get("type") or e.get("event")) for e in events}
        needs_ban = bool(err_types & {"browser_foreground_detected", "foreground_not_vscode_before_send", "focus_failed", "palette_command_bypassed"})
        # Gather commands from events (palette bypass) in addition to commit logs
        bypass_cmds = set()
        for e in events:
            if ((e.get("type") or e.get("event")) == "palette_command_bypassed") and e.get("command"):
                try:
                    bypass_cmds.add(str(e.get("command")))
                except Exception:
                    pass
        to_ban = set(palette_cmds) | set(bypass_cmds)
        if needs_ban and to_ban:
            rules = {}
            if policy_path.exists():
                try:
                    rules = json.loads(policy_path.read_text(encoding="utf-8"))
                except Exception:
                    rules = {}
            pal = rules.get("palette") or {}
            banned = set([str(x).lower() for x in (pal.get("banned") or [])])
            for c in to_ban:
                banned.add(c.lower())
            pal["banned"] = sorted(banned)
            rules["palette"] = pal
            policy_path.parent.mkdir(parents=True, exist_ok=True)
            policy_path.write_text(json.dumps(rules, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass

    # Seed solutions file entries (id + description)
    solutions = [
        {"id": "vscode.foreground_process_gate", "command": "Guard: proceed only when foreground process is Code.exe", "how": "Use GetWindowThreadProcessId to fetch process name and skip actions unless it is Code.exe."},
        {"id": "chat.ocr_readiness_gate", "command": "Gate send on template/heuristic chat readiness", "how": "Use template match on chat input image or text heuristics to allow sending only when ready."},
        {"id": "palette.hygiene_esc", "command": "Dismiss overlays with ESC and re-observe", "how": "If command palette/search is open, press ESC and capture again instead of typing."},
        {"id": "error.input_wrong_search_palette", "command": "Detect and avoid typing in VS Code search/palette", "how": "OCR before/after palette open/type/enter; if overlay tokens (e.g., 'Search', 'Command Palette') are present, log error and skip typing."},
        {"id": "copilot.app_send_and_read", "command": "Ensure Copilot app is foreground before send/read", "how": "Verify foreground via window title/class; log misfocus; wait and re-observe OCR until text stabilizes."},
        {"id": "chat.cursor_field_verification", "command": "Verify chat input field via OCR before typing", "how": "Observe OCR before text input; check for chat input cues; if mismatch, log 'text_input_wrong_field' and abort."},
        {"id": "ocr.observation_checklist", "command": "Observe OCR around TAB/TEXT/ENTER and response", "how": "1) before TAB, 2) after TAB, 3) before TEXT, 4) after TEXT, 5) after ENTER (assess reaction), 6) after Copilot response finishes (observe twice, then analyze)."},
        {"id": "decision.policy.ocr_sequential_selection", "command": "Explicit selection after each OCR observe", "how": "After every OCR observe, choose among safe options: close overlay (ESC), adjust focus (toggle/scroll/tab), advance with TAB, type only when input confirmed ready, press ENTER only after pre-check, and for responses run read→wait 1.5s→re-read loops until stabilized or timeout; abstain on ambiguity."}
    ]
    for sol in solutions:
        write_jsonl(solutions_jsonl, sol)

    # Persist user-provided assessment as a lesson for visibility
    user_assessment = {
        "ts": ts_now,
        "kind": "assessment",
        "message": "You made some progress. Issues remain: input in VS Code search palette; Copilot message not sent/read; improper text input field without OCR verification.",
        "actions": [
            "Add OCR before/after TAB, TEXT, ENTER",
            "Ensure Copilot foreground and wait for response",
            "Log errors and trigger improvement when wrong field or palette overlay detected"
        ]
    }
    write_jsonl(lessons_jsonl, user_assessment)

    print(f"Learned from {len(events)} error events; lessons appended: {lessons_jsonl}")
    print(f"Solutions appended: {solutions_jsonl}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
