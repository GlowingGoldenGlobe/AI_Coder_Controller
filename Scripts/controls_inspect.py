from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict

from src.control_state import get_controls_state, is_state_stale


def _format_state(state: Dict[str, Any]) -> str:
    if not state:
        return "<no controls_state.json found or empty>"
    return json.dumps(state, indent=2, sort_keys=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect shared controls_state.json.")
    parser.add_argument(
        "--root",
        type=str,
        default=".",
        help="Workspace root (defaults to current directory)",
    )
    parser.add_argument(
        "--stale-seconds",
        type=float,
        default=0.0,
        help=(
            "Optional max age in seconds to consider the state stale. "
            "0 disables staleness check."
        ),
    )
    args = parser.parse_args()

    root = Path(args.root).resolve()

    state = get_controls_state(root)
    print("Controls state (raw):")
    print(_format_state(state))

    if args.stale_seconds > 0:
        stale = is_state_stale(state, args.stale_seconds)
        print()
        print(f"Stale check (> {args.stale_seconds:.1f}s): {'STALE' if stale else 'fresh'}")

    owner = str(state.get("owner", "") or "") if state else ""
    if owner:
        print()
        print(f"Current owner: {owner!r}")
    else:
        print()
        print("Current owner: <none>")

    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
