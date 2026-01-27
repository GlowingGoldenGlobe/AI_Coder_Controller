from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, MutableMapping, Optional, Sequence

from .errors import ModuleError
from .interfaces import Module, RunContext, ensure_result_shape


logger = logging.getLogger("ai_coder_controller.orchestrator")


@dataclass
class TickResult:
    ok: bool
    data: Dict[str, Any]
    module_results: List[Dict[str, Any]]


def init_all(modules: Sequence[Module], ctx: RunContext) -> None:
    for module in modules:
        module.init(ctx)


def shutdown_all(modules: Sequence[Module], ctx: RunContext) -> None:
    for module in reversed(modules):
        try:
            module.shutdown(ctx)
        except Exception:
            logger.exception("module shutdown failed", extra={"module_name": getattr(module, "name", "?")})


def run_once(
    modules: Sequence[Module],
    ctx: RunContext,
    input_data: Optional[MutableMapping[str, Any]] = None,
) -> TickResult:
    data: Dict[str, Any] = dict(input_data or {})
    module_results: List[Dict[str, Any]] = []

    for module in modules:
        module_name = getattr(module, "name", "?")
        started = time.perf_counter()
        try:
            raw = module.run_once(data, ctx)
        except ModuleError as exc:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            logger.error(
                "module raised ModuleError",
                extra={
                    "module_name": module_name,
                    "code": exc.code,
                    "elapsed_ms": elapsed_ms,
                    "details": exc.details,
                },
            )
            module_results.append(
                {
                    "status": "error",
                    "payload": {},
                    "meta": {
                        "module": module_name,
                        "elapsed_ms": elapsed_ms,
                        "error": {"code": exc.code, "message": exc.message, "details": exc.details},
                    },
                }
            )
            return TickResult(ok=False, data=data, module_results=module_results)
        except Exception as exc:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            logger.exception("module crashed", extra={"module_name": module_name, "elapsed_ms": elapsed_ms})
            module_results.append(
                {
                    "status": "error",
                    "payload": {},
                    "meta": {
                        "module": module_name,
                        "elapsed_ms": elapsed_ms,
                        "error": {"code": "exception", "message": str(exc)},
                    },
                }
            )
            return TickResult(ok=False, data=data, module_results=module_results)

        # Contract violations should be loud.
        res = ensure_result_shape(raw, module_name)
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        res_meta = dict(res.get("meta") or {})
        res_meta.setdefault("elapsed_ms", elapsed_ms)
        res_meta.setdefault("module", module_name)
        res["meta"] = res_meta

        module_results.append(res)
        if res["status"] == "ok":
            data.update(res.get("payload", {}))
        elif res["status"] == "error":
            logger.warning("module reported error", extra={"module_name": module_name, "meta": res_meta})
            return TickResult(ok=False, data=data, module_results=module_results)
        else:
            logger.debug("module skipped", extra={"module_name": module_name, "meta": res_meta})

    return TickResult(ok=True, data=data, module_results=module_results)


def run_loop(
    modules: Sequence[Module],
    ctx: RunContext,
    *,
    max_iterations: Optional[int] = None,
    interval_s: float = 0.0,
) -> Iterable[TickResult]:
    i = 0
    while max_iterations is None or i < max_iterations:
        ctx.tick = i
        yield run_once(modules, ctx)
        i += 1
        if interval_s > 0:
            time.sleep(interval_s)
