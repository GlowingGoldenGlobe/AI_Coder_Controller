from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any


def _root() -> Path:
    return Path(__file__).resolve().parent.parent


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


def _lanes_dir(root: Path) -> Path:
    return root / "projects" / "Chat_Lanes"


def _ts() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _append_jsonl(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def _write_if_missing(path: Path, content: str) -> None:
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def init_lanes(root: Path, lanes: list[str]) -> dict:
    d = _lanes_dir(root)
    d.mkdir(parents=True, exist_ok=True)

    notif = d / "notifications.jsonl"

    board = d / "BOARD.md"
    board_text = "\n".join(
        [
            "# Parallel Chat Lanes (VS Code tabs)",
            "",
            f"Generated/updated: {_ts()}",
            "",
            "## Goal",
            "",
            "Use multiple VS Code Copilot Chat *tabs/conversations* in the same window to work in parallel.",
            "Each chat lane writes its progress to a lane file so other lanes can read it without needing UI automation.",
            "",
            "## How to use (manual, low-friction)",
            "",
            "1) Open Copilot Chat in VS Code.",
            "2) Create/open one conversation per lane (e.g., Primary / Workflow / OCR / Triage).",
            "3) In each lane conversation, paste the corresponding lane file contents (or attach it if your Copilot supports attachments).",
            "4) When you finish a step, append a short update to that lane file under the \"Notes\" section.",
            "5) Periodically skim `notifications.jsonl` to see what other lanes changed.",
            "",
            "## Files",
            "",
            "- `notifications.jsonl`: append-only event log (workflow scripts write here)",
            "- `lane_<name>.md`: per-lane working memory and handoff notes",
            "",
            "## Safety",
            "",
            "- This system does not click/type in the UI.",
            "- It does not auto-clean or prune logs; pruning is always manual.",
        ]
    )
    board.write_text(board_text, encoding="utf-8")

    created = 0
    for name in lanes:
        safe = "".join(ch for ch in name.lower().strip() if ch.isalnum() or ch in {"_", "-"})
        if not safe:
            continue
        p = d / f"lane_{safe}.md"
        lane_text = "\n".join(
            [
                f"# Lane: {safe}",
                "",
                f"Created: {_ts()}",
                "",
                "## Purpose",
                "",
                "Describe what this lane owns (keep it short).",
                "",
                "## Inputs",
                "",
                "- Links to summaries/artifacts you rely on",
                "- Key files you are editing",
                "",
                "## Outputs",
                "",
                "- What you will produce (PR/patch, report, script, etc.)",
                "",
                "## Current focus",
                "",
                "- (empty)",
                "",
                "## Notes (append-only)",
                "",
                f"- {_ts()} Initialized.",
            ]
        )
        if not p.exists():
            p.write_text(lane_text, encoding="utf-8")
            created += 1

    _append_jsonl(
        notif,
        {
            "ts": _ts(),
            "type": "lanes_init",
            "lanes": lanes,
            "note": "Parallel chat lanes initialized/updated",
        },
    )

    return {
        "ok": True,
        "dir": str(d),
        "board": str(board),
        "notifications": str(notif),
        "created_lane_files": created,
    }


def post_event(root: Path, *, type_: str, message: str, lane: str | None, run_id: str | None) -> dict:
    d = _lanes_dir(root)
    d.mkdir(parents=True, exist_ok=True)
    notif = d / "notifications.jsonl"

    evt = {
        "ts": _ts(),
        "type": type_,
        "message": message,
    }
    if lane:
        evt["lane"] = lane
    if run_id:
        evt["run_id"] = run_id

    _append_jsonl(notif, evt)

    if lane:
        safe = "".join(ch for ch in lane.lower().strip() if ch.isalnum() or ch in {"_", "-"})
        if safe:
            lane_path = d / f"lane_{safe}.md"
            if lane_path.exists():
                try:
                    with lane_path.open("a", encoding="utf-8") as f:
                        f.write(f"\n- {_ts()} [{type_}] {message}\n")
                except Exception:
                    pass

    return {"ok": True, "notifications": str(notif)}


def main() -> int:
    ap = argparse.ArgumentParser(description="File-based parallel chat lanes for VS Code Copilot Chat tabs.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    ap_init = sub.add_parser("init", help="Create Chat_Lanes board/lane files (non-destructive).")
    ap_init.add_argument(
        "--lanes",
        default="primary,workflow,ocr,triage",
        help="Comma-separated lane names",
    )

    ap_post = sub.add_parser("post", help="Append an event to notifications.jsonl (optionally to a lane file).")
    ap_post.add_argument("--type", default="note")
    ap_post.add_argument("--message", required=True)
    ap_post.add_argument("--lane", default="")
    ap_post.add_argument("--run-id", default="")

    args = ap.parse_args()
    root = _root()

    if args.cmd == "init":
        lanes = [x.strip() for x in str(args.lanes).split(",") if x.strip()]
        res = init_lanes(root, lanes)
        print(json.dumps(res, indent=2))
        return 0

    if args.cmd == "post":
        lane = str(args.lane or "").strip() or None
        run_id = str(args.run_id or "").strip() or None
        res = post_event(root, type_=str(args.type or "note"), message=str(args.message), lane=lane, run_id=run_id)
        print(json.dumps(res, indent=2))
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
