from __future__ import annotations

from dataclasses import dataclass
from typing import Optional
import json
import os
import time

CONTROLS_STATE_PATH = os.path.join("config", "controls_state.json")
EMERGENCY_STOP_PATH = os.path.join("config", "emergency_stop.json")


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

    def __init__(self) -> None:
        self._last_state_load_s = 0.0
        self._controls_state = None

    # --- Emergency Stop -------------------------------------------------
    def is_emergency_stop(self) -> bool:
        try:
            if not os.path.exists(EMERGENCY_STOP_PATH):
                return False
            with open(EMERGENCY_STOP_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            return bool(data.get("stopped", False))
        except Exception:
            return False

    def set_emergency_stop(self, stopped: bool, reason: str = "") -> None:
        data = {
            "stopped": bool(stopped),
            "reason": reason,
            "timestamp": time.time(),
        }
        os.makedirs(os.path.dirname(EMERGENCY_STOP_PATH), exist_ok=True)
        with open(EMERGENCY_STOP_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    # --- Controls State -------------------------------------------------
    def _load_controls_state(self) -> None:
        now = time.time()
        if self._controls_state is not None and (now - self._last_state_load_s) < 1.0:
            return
        try:
            with open(CONTROLS_STATE_PATH, "r", encoding="utf-8") as f:
                self._controls_state = json.load(f)
            self._last_state_load_s = now
        except Exception:
            self._controls_state = None
            self._last_state_load_s = now

    def composite_gate(self) -> SafetyDecision:
        if self.is_emergency_stop():
            return SafetyDecision(False, "emergency_stop")
        self._load_controls_state()
        cs = self._controls_state or {}
        if str(cs.get("paused", "")).lower() == "true":
            return SafetyDecision(False, "controls_paused")
        owner = cs.get("owner")
        if owner and owner not in ("agent", "workflow_test", "orchestrator"):
            return SafetyDecision(False, f"controls_owned_by:{owner}")
        return SafetyDecision(True, "ok")

    # --- Public API -----------------------------------------------------
    def check_allowed(self) -> SafetyDecision:
        return self.composite_gate()

    def pause_controls(self) -> None:
        self._mutate_controls_state(paused=True)

    def resume_controls(self) -> None:
        self._mutate_controls_state(paused=False)

    def _mutate_controls_state(self, *, paused: Optional[bool] = None) -> None:
        cs = {}
        try:
            if os.path.exists(CONTROLS_STATE_PATH):
                with open(CONTROLS_STATE_PATH, "r", encoding="utf-8") as f:
                    cs = json.load(f)
        except Exception:
            cs = {}
        if paused is not None:
            cs["paused"] = bool(paused)
        os.makedirs(os.path.dirname(CONTROLS_STATE_PATH), exist_ok=True)
        with open(CONTROLS_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(cs, f, indent=2)
