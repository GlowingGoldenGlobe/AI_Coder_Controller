# Action Safety & Architecture Plan

This document consolidates Copilot’s recommendations into an actionable plan: a unified safety facade, a formal `Action` abstraction, error-rate brakes, and clearer subsystem boundaries.

## Goals
- Unify safety gates into a single facade invoked by all external-effect actions.
- Make `emergency_stop` global, persistent, and honored by all loops.
- Adopt a formal `Action` schema for planner/policy/executor interoperability.
- Use error-rate thresholds to auto-pause or disable automation safely.
- Clarify subsystem boundaries; treat logging/rate-limiting as cross-cutting services.

## Subsystems
- Core Runtime: `main`, messaging, policy, planner, `phi4_client`
- Control/UI: `control`, `control_state`, `agent_terminal`, `ui`
- Vision/OCR: `capture`, `ocr`, `ocr_observer`, `image_compose`
- Introspection/Maintenance: `self_improve`, `cleanup`
- Cross-cutting: `jsonlog`, `utils`, ErrorService, ActionService

## Immediate Deliverables
- `src/actions/action_schema.py`: Dataclass defining the unified `Action` model.
- `src/safety/action_safety.py`: Safety facade interface for gates + emergency-stop persistence.
- Roadmap to introduce ErrorService and ActionService (consolidating logging/rate limits).

## Action Schema (initial)
```python
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
import time

@dataclass
class Action:
    type: str
    params: Dict[str, Any] = field(default_factory=dict)
    origin: str = "unknown"  # e.g., planner|policy|runtime|user
    timestamp: float = field(default_factory=lambda: time.time())
    safety_requirements: List[str] = field(default_factory=list)
    rate_limit_key: Optional[str] = None
    tags: List[str] = field(default_factory=list)
    id: Optional[str] = None
```

## Safety Facade Responsibilities
- Composite gate: controls owner, keyboard/mouse allowed, pause state, rate-limits.
- Emergency stop: global, persisted (disk), checked by all loops and effectful functions.
- Error-rate brakes: tie into `jsonlog` metrics to auto-pause/disable when thresholds exceed.
- Lease freshness: ensure control state is fresh (timestamps, owner, window ID).

## Error-Rate Brakes (policy hooks)
- `ocr.error_rate_threshold`: pause OCR observer and effectful actions.
- `ui.failure_rate_threshold`: pause control actions.
- `planner.error_rate_threshold`: disable planner path.

## Capability Manifest (future)
- Machine-readable manifest of callable functions, parameters, safety tags, rate-limit keys.
- Used by planner/policy to validate and discover capabilities safely.

## Roadmap
1) Integrate `Action` into planner/policy/executor call sites.
2) Wrap effectful operations with `ActionSafety.check_allowed(action)`.
3) Introduce ErrorService + ActionService to centralize logging and rate-limits.
4) Persist and enforce `emergency_stop` across restarts; unify shutdown/pause states.
5) Add capability manifest generator and adopt in planner/policy.

## Acceptance Criteria
- All external-effect functions call the safety facade before executing.
- `emergency_stop` stops loops immediately and persists to disk; restart is safe by default.
- Error-rate thresholds auto-pause or disable relevant subsystems with logged reasons.
- Planner/policy/executor operate on the shared `Action` model.
