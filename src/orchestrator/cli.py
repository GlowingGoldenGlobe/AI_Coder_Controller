from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Any, Dict

from .default_registry import build_default_registry
from .interfaces import RunContext
from .registry import build_from_config
from .runner import init_all, run_loop, shutdown_all


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s %(message)s")


def _load_config(path: Path) -> Dict[str, Any]:
    raw = path.read_text(encoding="utf-8")
    try:
        return json.loads(raw)
    except Exception as exc:
        raise SystemExit(f"Invalid JSON config: {path} ({exc})")


def main() -> int:
    parser = argparse.ArgumentParser(description="Orchestrator pipeline runner (safe by default)")
    parser.add_argument("--config", type=str, default="config/orchestrator_pipeline_demo.json")
    parser.add_argument("--max-iterations", type=int, default=1)
    parser.add_argument("--interval-s", type=float, default=0.0)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Force dry-run mode (safe). This is already the default unless --live is set.",
    )
    parser.add_argument("--live", action="store_true", help="Enable live actions (disables dry-run)")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    _setup_logging(args.verbose)

    cfg_path = Path(args.config)
    cfg = _load_config(cfg_path)

    if args.live and args.dry_run:
        raise SystemExit("Invalid flags: --live and --dry-run are mutually exclusive")

    # Safe by default: dry-run unless explicitly live.
    dry_run = not bool(args.live)

    registry = build_default_registry()
    modules = build_from_config(cfg, registry)

    ctx = RunContext(dry_run=dry_run, config=cfg)

    init_all(modules, ctx)
    try:
        last = None
        for last in run_loop(modules, ctx, max_iterations=args.max_iterations, interval_s=args.interval_s):
            pass
        if last is None:
            return 0

        out = {
            "ok": bool(last.ok),
            "dry_run": dry_run,
            "data": last.data,
            "module_results": last.module_results,
        }
        print(json.dumps(out, indent=2, sort_keys=True))
        return 0 if out["ok"] else 2
    finally:
        shutdown_all(modules, ctx)


if __name__ == "__main__":
    raise SystemExit(main())
