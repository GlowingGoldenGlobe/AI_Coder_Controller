from __future__ import annotations
import argparse
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional


def _parse_line_ts(line: str) -> Optional[float]:
    """Extract ISO-like timestamp between [ ] and return as POSIX seconds."""
    try:
        start = line.find("[")
        end = line.find("]", start + 1)
        if start == -1 or end == -1:
            return None
        ts_str = line[start + 1 : end].strip()
        if not ts_str:
            return None
        dt = datetime.fromisoformat(ts_str)
        return dt.timestamp()
    except Exception:
        return None


def summarize_log(path: Path, since_ts: Optional[float] = None) -> Dict[str, Any]:
    data: Dict[str, Any] = {
        "path": str(path),
        "exists": path.exists(),
        "lines": 0,
        "scanned_lines": 0,
        "pass": 0,
        "fail": 0,
    }
    if not path.exists():
        return data
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return data
    lines = text.splitlines()
    data["lines"] = len(lines)
    for line in lines:
        ts = _parse_line_ts(line)
        if since_ts is not None:
            if ts is None or ts < since_ts:
                continue
        data["scanned_lines"] += 1
        if "VERIFY PASS" in line:
            data["pass"] += 1
        elif "VERIFY FAIL" in line:
            data["fail"] += 1
    total = data["pass"] + data.get("fail", 0)
    data["total"] = total
    if total > 0:
        data["success_rate"] = data["pass"] / float(total)
    return data


def main(argv: List[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Summarize commit+verify logs (pass/fail counts) without driving UI.",
    )
    ap.add_argument(
        "--log",
        action="append",
        dest="logs",
        default=None,
        help="Path to a commit_verify_*.log file (can be repeated).",
    )
    ap.add_argument(
        "--out",
        type=str,
        default="",
        help="Optional JSON output path; defaults to logs/tests/commit_verify_summary_YYYYMMDD_HHMMSS.json",
    )
    ap.add_argument(
        "--hours",
        type=float,
        default=0.0,
        help="If > 0, only include log lines from approximately the last N hours.",
    )
    args = ap.parse_args(argv)

    root = Path(__file__).resolve().parent.parent
    logs: List[Path]
    if args.logs:
        logs = [root / Path(p) for p in args.logs]
    else:
        # Reasonable defaults if no explicit logs are provided.
        candidates = [
            root / "logs" / "actions" / "commit_verify_2plus2.log",
            root / "logs" / "actions" / "commit_verify_stability.log",
            root / "logs" / "actions" / "commit_verify_token.log",
        ]
        logs = candidates

    since_ts: Optional[float]
    if args.hours and args.hours > 0:
        since_ts = time.time() - float(args.hours) * 3600.0
    else:
        since_ts = None

    per_log: List[Dict[str, Any]] = []
    for lp in logs:
        per_log.append(summarize_log(lp, since_ts=since_ts))

    overall_pass = sum(item.get("pass", 0) for item in per_log)
    overall_fail = sum(item.get("fail", 0) for item in per_log)
    overall_total = overall_pass + overall_fail
    overall: Dict[str, Any] = {
        "pass": overall_pass,
        "fail": overall_fail,
        "total": overall_total,
    }
    if overall_total > 0:
        overall["success_rate"] = overall_pass / float(overall_total)

    summary: Dict[str, Any] = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "hours": float(args.hours) if args.hours and args.hours > 0 else 0.0,
        "logs": per_log,
        "overall": overall,
    }

    out_path: Path
    if args.out:
        out_path = root / args.out
    else:
        out_dir = root / "logs" / "tests"
        out_dir.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        out_path = out_dir / f"commit_verify_summary_{ts}.json"

    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"Wrote summary to {out_path}")
    except Exception as e:
        print(f"Failed to write summary: {e}")
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        return 1

    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
