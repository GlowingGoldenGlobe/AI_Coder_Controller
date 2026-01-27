from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, MutableMapping, Optional


JsonDict = Dict[str, Any]


@dataclass
class RunContext:
    """Shared run context passed to every module.

    Keep this small and stable; it is the primary contract surface.
    """

    dry_run: bool = True
    tick: int = 0
    config: Mapping[str, Any] = field(default_factory=dict)


class Module(ABC):
    """A single pipeline step.

    Contract:
    - init() is called once.
    - run_once() is called each tick.
    - shutdown() is called once.

    Modules should be deterministic in dry_run mode and must not perform side
    effects when ctx.dry_run is True.
    """

    name: str

    @abstractmethod
    def init(self, ctx: RunContext) -> None:
        raise NotImplementedError

    @abstractmethod
    def run_once(self, data: MutableMapping[str, Any], ctx: RunContext) -> JsonDict:
        """Process input `data` and return a standard result.

        Expected result shape:
        {
          "status": "ok" | "skip" | "error",
          "payload": { ... },
          "meta": { ... }
        }
        """

        raise NotImplementedError

    @abstractmethod
    def shutdown(self, ctx: RunContext) -> None:
        raise NotImplementedError


def ensure_result_shape(result: Mapping[str, Any], module_name: str) -> JsonDict:
    status = result.get("status")
    if status not in {"ok", "skip", "error"}:
        raise ValueError(f"{module_name}: invalid status {status!r}")

    payload = result.get("payload")
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise ValueError(f"{module_name}: payload must be a dict")

    meta = result.get("meta")
    if meta is None:
        meta = {}
    if not isinstance(meta, dict):
        raise ValueError(f"{module_name}: meta must be a dict")

    return {"status": status, "payload": payload, "meta": meta}
