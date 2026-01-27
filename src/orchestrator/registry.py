from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Mapping

from .interfaces import Module


Factory = Callable[[], Module]


@dataclass
class Registry:
    """Simple module registry.

    This exists so production modules and test mocks can be swapped easily.
    """

    _factories: Dict[str, Factory]

    def __init__(self) -> None:
        self._factories = {}

    def register(self, name: str, factory: Factory) -> None:
        if not name:
            raise ValueError("name must be non-empty")
        if name in self._factories:
            raise ValueError(f"module already registered: {name}")
        self._factories[name] = factory

    def create(self, name: str) -> Module:
        try:
            factory = self._factories[name]
        except KeyError as exc:
            raise KeyError(f"unknown module: {name}") from exc
        module = factory()
        if getattr(module, "name", None) != name:
            raise ValueError(
                f"registry mismatch: requested {name!r} but module.name is {getattr(module, 'name', None)!r}"
            )
        return module

    def create_many(self, names: Iterable[str]) -> List[Module]:
        return [self.create(n) for n in names]


def build_from_config(cfg: Mapping[str, Any], registry: Registry) -> List[Module]:
    pipeline = cfg.get("pipeline")
    if pipeline is None:
        raise ValueError("config missing 'pipeline'")
    if not isinstance(pipeline, list) or not all(isinstance(x, str) for x in pipeline):
        raise ValueError("config 'pipeline' must be a list[str]")
    return registry.create_many(pipeline)
