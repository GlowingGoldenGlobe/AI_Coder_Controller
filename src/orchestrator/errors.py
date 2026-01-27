from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional


@dataclass
class ModuleError(Exception):
    """Raised when a module cannot complete its work.

    Modules should raise this (instead of arbitrary exceptions) for expected,
    recoverable failures.
    """

    module: str
    code: str = "error"
    message: str = ""
    details: Optional[Mapping[str, Any]] = None

    def __str__(self) -> str:  # pragma: no cover
        base = f"{self.module}:{self.code}"
        if self.message:
            base += f": {self.message}"
        return base
