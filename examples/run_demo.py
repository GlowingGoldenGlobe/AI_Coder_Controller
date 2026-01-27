from __future__ import annotations

import argparse
import json
import logging

from src.orchestrator import Registry, RunContext, init_all, run_once, shutdown_all
from src.orchestrator.modules import CounterCapture, DoublerAnalyze, PrintAct


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def build_demo_registry() -> Registry:
    reg = Registry()
    reg.register("capture_counter", lambda: CounterCapture())
    reg.register("analyze_double", lambda: DoublerAnalyze())
    reg.register("act_print", lambda: PrintAct())
    return reg


def main() -> int:
    parser = argparse.ArgumentParser(description="AI_Coder_Controller orchestrator demo (mock pipeline)")
    parser.add_argument("--dry-run", action="store_true", default=True, help="Do not perform side effects (default)")
    parser.add_argument("--live", action="store_true", help="Disable dry-run (allows side effects)")
    parser.add_argument("--max-iterations", type=int, default=1)
    parser.add_argument("--start", type=int, default=0)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    dry_run = args.dry_run and not args.live
    _setup_logging(args.verbose)

    registry = build_demo_registry()
    modules = registry.create_many(["capture_counter", "analyze_double", "act_print"])

    ctx = RunContext(dry_run=dry_run, config={"start": args.start})
    init_all(modules, ctx)
    try:
        last = None
        for _ in range(args.max_iterations):
            last = run_once(modules, ctx)
            ctx.tick += 1

        out = {
            "ok": bool(last.ok if last else False),
            "dry_run": dry_run,
            "data": last.data if last else {},
            "module_results": last.module_results if last else [],
        }
        print(json.dumps(out, indent=2, sort_keys=True))
        return 0 if out["ok"] else 2
    finally:
        shutdown_all(modules, ctx)


if __name__ == "__main__":
    raise SystemExit(main())
