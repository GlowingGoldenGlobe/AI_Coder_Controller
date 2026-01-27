from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, MutableMapping

from ..errors import ModuleError
from ..interfaces import Module, RunContext


@dataclass
class CounterCapture(Module):
    """Demo capture module that produces an incrementing integer."""

    name: str = "capture_counter"
    _start: int = 0

    def init(self, ctx: RunContext) -> None:
        self._start = int(ctx.config.get("start", 0))

    def run_once(self, data: MutableMapping[str, Any], ctx: RunContext) -> Dict[str, Any]:
        value = self._start + ctx.tick
        return {"status": "ok", "payload": {"value": value}, "meta": {"tick": ctx.tick}}

    def shutdown(self, ctx: RunContext) -> None:
        return None


@dataclass
class DoublerAnalyze(Module):
    """Demo analyze module that requires `value` and computes `double`."""

    name: str = "analyze_double"

    def init(self, ctx: RunContext) -> None:
        return None

    def run_once(self, data: MutableMapping[str, Any], ctx: RunContext) -> Dict[str, Any]:
        if "value" not in data:
            raise ModuleError(self.name, code="missing_input", message="no 'value' in data")
        return {"status": "ok", "payload": {"double": int(data["value"]) * 2}, "meta": {}}

    def shutdown(self, ctx: RunContext) -> None:
        return None


@dataclass
class PrintAct(Module):
    """Demo action module.

    In live mode, it still only reports the action (no real side effects).
    """

    name: str = "act_print"

    def init(self, ctx: RunContext) -> None:
        return None

    def run_once(self, data: MutableMapping[str, Any], ctx: RunContext) -> Dict[str, Any]:
        action = {"type": "print", "message": f"double={data.get('double')}"}
        return {"status": "ok", "payload": {"last_action": action}, "meta": {"dry_run": ctx.dry_run}}

    def shutdown(self, ctx: RunContext) -> None:
        return None
