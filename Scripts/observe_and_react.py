from __future__ import annotations
import argparse
import json
import time
from pathlib import Path
import os

from src.windows import WindowsManager


DISALLOWED_TITLES = [
    "microsoft edge",
]
DISALLOWED_SUBSTRINGS = [
    "github",
    "github copilot coding agent",
]
ALLOWED_HINTS = [
    "visual studio code",
    "copilot",
]


def write_jsonl(log_path: Path, obj: dict) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def write_error_event(root: Path, type_: str, message: str, data: dict) -> None:
    try:
        err_dir = root / "logs" / "errors"
        err_dir.mkdir(parents=True, exist_ok=True)
        with (err_dir / "events.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                "source": "observe_and_react.py",
                "type": type_,
                "message": message,
                "data": data,
            }) + "\n")
    except Exception:
        pass


def should_close(title: str, cls: str) -> bool:
    t = title.lower()
    c = cls.lower()
    if any(h in t for h in ALLOWED_HINTS):
        return False
    if any(k in t for k in DISALLOWED_TITLES):
        return True
    if any(s in t for s in DISALLOWED_SUBSTRINGS):
        # heuristic: VS Code also shows github in title if a GH file open; require browser-ish hint
        if "edge" in t or " - microsoft edge" in t:
            return True
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description="Observe foreground and react: close disallowed windows.")
    ap.add_argument("--ticks", type=int, default=20, help="Number of observation ticks")
    ap.add_argument("--interval-ms", type=int, default=500, help="Interval between ticks")
    ap.add_argument("--log", type=str, default="logs/tests/observe_react.jsonl", help="JSONL log path")
    args = ap.parse_args()

    root = Path(__file__).resolve().parent.parent
    log_path = Path(args.log)
    win = WindowsManager()

    for i in range(max(1, args.ticks)):
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        hwnd = win.get_foreground()
        info = {"hwnd": 0, "title": "", "class": ""}
        if hwnd:
            info = win.get_window_info(hwnd)
        title = info.get("title", "")
        cls = info.get("class", "")
        action = "none"
        reason = ""
        if hwnd and title:
            if should_close(title, cls):
                try:
                    ok = win.close_hwnd(int(info.get("hwnd", 0)))
                except Exception:
                    ok = False
                action = "close" if ok else "close_failed"
                reason = f"Disallowed foreground: title='{title}', class='{cls}'"
                write_error_event(root, "browser_foreground_closed" if ok else "browser_close_failed", reason, {"title": title, "class": cls})
        write_jsonl(log_path, {
            "timestamp": ts,
            "tick": i+1,
            "foreground": info,
            "action": action,
            "reason": reason,
        })
        time.sleep(max(0, args.interval_ms) / 1000.0)

    print(f"Observer finished. Log: {log_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
