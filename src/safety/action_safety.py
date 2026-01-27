from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
import json
import time

from src.control_state import get_controls_state, is_state_stale


@dataclass
class SafetyDecision:
    allowed: bool
    reason: str


class ActionSafety:
    """Facade for safety gating and emergency stop persistence.

    Integrate this facade at all external-effect call sites (typing, clicks,
    window focus, file writes, network calls). This is a minimal stub to
    establish the interface; production implementations should wire in
    error-rate tracking and lease freshness checks.
    """

    def __init__(self, root: Path | str | None = None) -> None:
        # Root is used to resolve config paths (so callers don't depend on CWD).
        self._root = Path(root).resolve() if root is not None else Path(".").resolve()

    def _controls_state_path(self) -> Path:
        return self._root / "config" / "controls_state.json"

    def _emergency_stop_path(self) -> Path:
        return self._root / "config" / "emergency_stop.json"

    # --- Emergency Stop -------------------------------------------------
    def is_emergency_stop(self) -> bool:
        try:
            p = self._emergency_stop_path()
            if not p.exists():
                return False
            data = json.loads(p.read_text(encoding="utf-8"))
            return bool(data.get("stopped", False))
        except Exception:
            return False

    def set_emergency_stop(self, stopped: bool, reason: str = "") -> None:
        data = {
            "stopped": bool(stopped),
            "reason": reason,
            "timestamp": time.time(),
        }
        p = self._emergency_stop_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, indent=2), encoding="utf-8")

    # --- Controls State -------------------------------------------------
    def composite_gate(self, *, owner: str | None = None, stale_after_s: float = 10.0) -> SafetyDecision:
        """Composite safety gate.

        - Blocks when emergency stop is set.
        - Blocks when controls are paused.
        - Blocks when another owner holds controls, unless that state is stale.
        """

        if self.is_emergency_stop():
            return SafetyDecision(False, "emergency_stop")

        cs = get_controls_state(self._root) or {}

        # paused is stored as a real bool in this repo; tolerate strings too.
        paused_val: Any = cs.get("paused", False)
        paused = bool(paused_val) if not isinstance(paused_val, str) else (paused_val.strip().lower() == "true")
        if paused:
            return SafetyDecision(False, "controls_paused")

        current_owner = str(cs.get("owner", "") or "")
        if not current_owner:
            return SafetyDecision(True, "ok")

        if owner and current_owner == owner:
            return SafetyDecision(True, "ok")

        # If the ownership snapshot looks stale, fail-open to avoid deadlocks.
        try:
            if is_state_stale(cs, float(stale_after_s)):
                return SafetyDecision(True, f"controls_owner_stale:{current_owner}")
        except Exception:
            pass

        return SafetyDecision(False, f"controls_owned_by:{current_owner}")

    # --- Public API -----------------------------------------------------
    def check_allowed(self) -> SafetyDecision:
        return self.composite_gate()

    def pause_controls(self) -> None:
        self._mutate_controls_state(paused=True)

    def resume_controls(self) -> None:
        self._mutate_controls_state(paused=False)

    def _mutate_controls_state(self, *, paused: Optional[bool] = None) -> None:
        try:
            cs = get_controls_state(self._root) or {}
        except Exception:
            cs = {}

        if paused is not None:
            cs["paused"] = bool(paused)
            cs["ts"] = time.time()

        p = self._controls_state_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(cs, indent=2), encoding="utf-8")
