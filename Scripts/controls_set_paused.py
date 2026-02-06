from __future__ import annotations

import argparse
import json
import time
from pathlib import Path


def _root() -> Path:
    return Path(__file__).resolve().parent.parent


def _as_bool(v: str) -> bool:
    s = str(v or "").strip().lower()
    if s in {"1", "true", "yes", "y", "on"}:
        return True
    if s in {"0", "false", "no", "n", "off"}:
        return False
    raise ValueError(f"Invalid boolean value: {v!r}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Set config/controls_state.json paused flag (and update ts).")
    ap.add_argument("--paused", required=True, help="true/false")
    args = ap.parse_args()

    paused = _as_bool(args.paused)
    root = _root()
    path = root / "config" / "controls_state.json"

    try:
        obj = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except Exception:
        obj = {}

    if not isinstance(obj, dict):
        obj = {}

    obj["paused"] = bool(paused)
    obj["ts"] = time.time()

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(obj, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"controls_set_paused: failed to write {path}: {e}")
        return 1

    print(f"controls_set_paused: paused={paused} ({path})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
