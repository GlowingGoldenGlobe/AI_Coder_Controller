from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
import time


@dataclass
class Action:
    """Unified action abstraction used by planner, policy, and executor.

    Fields are intentionally generic and extensible. Concrete executors can
    enforce stricter schemas for specific action types.
    """

    type: str
    params: Dict[str, Any] = field(default_factory=dict)
    origin: str = "unknown"  # e.g., planner|policy|runtime|user
    timestamp: float = field(default_factory=lambda: time.time())
    safety_requirements: List[str] = field(default_factory=list)
    rate_limit_key: Optional[str] = None
    tags: List[str] = field(default_factory=list)
    id: Optional[str] = None

    def require(self, *requirements: str) -> None:
        self.safety_requirements.extend(r for r in requirements if r not in self.safety_requirements)
