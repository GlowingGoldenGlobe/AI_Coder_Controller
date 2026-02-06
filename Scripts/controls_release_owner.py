from __future__ import annotations

import argparse
import time
from pathlib import Path


def _root() -> Path:
    return Path(__file__).resolve().parent.parent


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Safely clear config/controls_state.json owner (does not change paused state)."
    )
    ap.add_argument(
        "--if-owner",
        default="workflow_test",
        help="Only clear when current owner matches this string",
    )
    ap.add_argument("--force", action="store_true", help="Clear regardless of current owner")
    ap.add_argument(
        "--stale-seconds",
        type=float,
        default=0.0,
        help="Only clear if state snapshot is older than this many seconds (0 disables)",
    )
    args = ap.parse_args()

    root = _root()
    try:
        from src.control_state import get_controls_state, is_state_stale, set_controls_owner  # type: ignore
    except Exception as e:
        print(f"controls_release_owner: cannot import control_state: {e}")
        return 1

    st = get_controls_state(root) or {}
    owner = str(st.get("owner", "") or "")

    stale_ok = True
    if args.stale_seconds and args.stale_seconds > 0:
        try:
            stale_ok = bool(is_state_stale(st, float(args.stale_seconds)))
        except Exception:
            stale_ok = True

    if not stale_ok:
        print("controls_release_owner: not stale; no change")
        return 0

    if not args.force:
        if owner != str(args.if_owner):
            print(f"controls_release_owner: owner='{owner}' does not match --if-owner; no change")
            return 0

    set_controls_owner(root, None)
    print(
        f"controls_release_owner: cleared owner (was '{owner}') at {time.strftime('%Y-%m-%d %H:%M:%S')}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
