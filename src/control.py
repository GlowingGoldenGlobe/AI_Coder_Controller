import time
import json
from dataclasses import dataclass
from typing import List, Tuple, Optional, Callable
from pathlib import Path
from collections import deque

try:
    import pyautogui  # type: ignore
    pyautogui.FAILSAFE = True
except Exception:
    pyautogui = None


@dataclass
class SafetyLimits:
    max_clicks_per_min: int = 60
    max_keys_per_min: int = 120
    max_total_actions_per_min: int = 120  # Global rate limit for all actions


class Controller:
    def __init__(self, mouse_speed: float = 0.3, limits: SafetyLimits = SafetyLimits(),
                 mouse_control_seconds: int = 10, mouse_release_seconds: int = 5,
                 state_file: Optional[Path] = None):
        self.mouse_speed = mouse_speed
        self.limits = limits
        self._clicks = 0
        self._keys = 0
        self._window_t = time.time()
        self._last_action_ts = time.time()
        # Global action rate limiter (sliding window)
        self._action_times: deque = deque()
        # Intermittent control cycle (applies to mouse and keyboard)
        self._mouse_control_s = max(0, int(mouse_control_seconds))
        self._mouse_release_s = max(0, int(mouse_release_seconds))
        self._mouse_cycle_start = time.time()
        self._mouse_in_control = True  # start in control window by default
        self._controls_paused = False  # manual pause (e.g., via ESC)
        # Optional callable to enforce foreground window gating
        self._window_gate = None  # type: Optional[Callable[[], bool]]
        # Grace period for window gate (prevents rapid focus changes from blocking input)
        self._window_gate_last_ok_ts = 0.0
        self._window_gate_grace_s = 1.5  # Accept input for this long after gate was last true
        # Type interval for keyboard input
        self.type_interval = 0.01
        # State persistence file (optional)
        self._state_file = state_file
        # Load persisted pause state if available
        self._load_pause_state()

    def _window_reset_if_needed(self) -> None:
        if time.time() - self._window_t > 60:
            self._window_t = time.time()
            self._clicks = 0
            self._keys = 0

    def _check_global_rate_limit(self) -> bool:
        """Check if action is allowed under global rate limit. Returns True if allowed."""
        now = time.time()
        cutoff = now - 60.0
        # Prune old entries
        while self._action_times and self._action_times[0] < cutoff:
            self._action_times.popleft()
        return len(self._action_times) < self.limits.max_total_actions_per_min
    
    def _record_action(self) -> None:
        """Record that an action was taken for rate limiting."""
        self._action_times.append(time.time())
        self._last_action_ts = time.time()

    def move_mouse(self, x: int, y: int) -> bool:
        if pyautogui is None:
            return False
        if not self.is_controls_allowed():
            return False
        if not self._check_global_rate_limit():
            return False
        pyautogui.moveTo(x, y, duration=self.mouse_speed)
        self._record_action()
        return True

    def click_at(self, x: int, y: int, button: str = "left") -> bool:
        """Click at specific screen coordinates."""
        self._window_reset_if_needed()
        if self._clicks >= self.limits.max_clicks_per_min:
            return False
        if not self._check_global_rate_limit():
            return False
        if pyautogui is None:
            return False
        if not self.is_controls_allowed():
            return False
        try:
            pyautogui.click(x=x, y=y, button=button)
            self._clicks += 1
            self._record_action()
            return True
        except Exception:
            return False

    def click(self, button: str = "left") -> bool:
        self._window_reset_if_needed()
        if self._clicks >= self.limits.max_clicks_per_min:
            return False
        if not self._check_global_rate_limit():
            return False
        if pyautogui is None:
            return False
        if not self.is_controls_allowed():
            return False
        pyautogui.click(button=button)
        self._clicks += 1
        self._record_action()
        return True

    def type_text(self, text: str, retry_count: int = 2) -> bool:
        """Type text using pyautogui with retry logic for window gate issues.
        
        Args:
            text: The text to type
            retry_count: Number of retry attempts if gate check fails initially
        """
        self._window_reset_if_needed()
        if self._keys + len(text) >= self.limits.max_keys_per_min:
            return False
        if not self._check_global_rate_limit():
            return False
        if pyautogui is None:
            return False
        
        # Retry logic for cases where window gate temporarily fails
        for attempt in range(max(1, retry_count)):
            if not self.is_keyboard_allowed():
                if attempt < retry_count - 1:
                    time.sleep(0.15)  # Small delay before retry
                    continue
                return False
            break
        
        interval = float(getattr(self, "type_interval", 0.01))
        try:
            # Use write() for Unicode support (typewrite only handles ASCII)
            pyautogui.write(text, interval=interval)
        except Exception:
            # Fallback to typewrite for ASCII-only text
            try:
                pyautogui.typewrite(text, interval=interval)
            except Exception:
                return False
        self._keys += len(text)
        self._record_action()
        return True

    def press_keys(self, keys: List[str]) -> bool:
        self._window_reset_if_needed()
        if self._keys + len(keys) >= self.limits.max_keys_per_min:
            return False
        if not self._check_global_rate_limit():
            return False
        if pyautogui is None:
            return False
        try:
            # Keyboard input should not be blocked by the intermittent mouse cycle.
            if not self.is_keyboard_allowed():
                return False
            if len(keys) == 1:
                pyautogui.press(keys[0])
            else:
                pyautogui.hotkey(*keys)
            self._keys += len(keys)
            self._record_action()
            return True
        except Exception:
            return False

    # Intermittent mouse control helpers
    def _update_mouse_cycle(self) -> None:
        if self._mouse_control_s == 0 and self._mouse_release_s == 0:
            self._mouse_in_control = True
            return
        now = time.time()
        elapsed = now - self._mouse_cycle_start
        if self._mouse_in_control:
            if elapsed >= self._mouse_control_s:
                # switch to release phase
                self._mouse_in_control = False
                self._mouse_cycle_start = now
        else:
            if elapsed >= self._mouse_release_s:
                # switch back to control phase
                self._mouse_in_control = True
                self._mouse_cycle_start = now

    def is_controls_allowed(self) -> bool:
        """Mouse/controls gate: respects pause, mouse cycle, and window gate with grace period."""
        self._update_mouse_cycle()
        if self._controls_paused:
            return False
        if not self._mouse_in_control:
            return False
        if self._window_gate is not None:
            try:
                now = time.time()
                gate_ok = bool(self._window_gate())
                if gate_ok:
                    self._window_gate_last_ok_ts = now
                    return True
                # Allow within grace period
                if (now - self._window_gate_last_ok_ts) <= self._window_gate_grace_s:
                    return True
                return False
            except Exception:
                # Fail-closed on gate errors, but honor grace period from last-known-good.
                now = time.time()
                return (now - self._window_gate_last_ok_ts) <= self._window_gate_grace_s
        return True

    def is_keyboard_allowed(self) -> bool:
        """Keyboard gate: respects pause + optional window gate with grace period."""
        if self._controls_paused:
            return False
        if self._window_gate is not None:
            try:
                now = time.time()
                gate_ok = bool(self._window_gate())
                if gate_ok:
                    # Update the last-ok timestamp when gate passes
                    self._window_gate_last_ok_ts = now
                    return True
                # Allow input within grace period after gate was last true
                # This handles rapid focus changes during automation
                if (now - self._window_gate_last_ok_ts) <= self._window_gate_grace_s:
                    return True
                return False
            except Exception:
                # Fail-closed on gate errors, but honor grace period from last-known-good.
                now = time.time()
                return (now - self._window_gate_last_ok_ts) <= self._window_gate_grace_s
        return True

    def mouse_window_state(self) -> Tuple[bool, float, float]:
        """Return (in_control, seconds_into_phase, seconds_total_phase)."""
        self._update_mouse_cycle()
        now = time.time()
        elapsed = now - self._mouse_cycle_start
        total = self._mouse_control_s if self._mouse_in_control else self._mouse_release_s
        return self._mouse_in_control and not self._controls_paused, max(0.0, elapsed), float(total)

    def _load_pause_state(self) -> None:
        """Load persisted pause state from file if available."""
        if not self._state_file:
            return
        try:
            if self._state_file.exists():
                data = json.loads(self._state_file.read_text(encoding="utf-8"))
                if not bool(data.get("paused", False)):
                    return

                # Only restore pause if it was set within the last 24 hours.
                ts_val = data.get("ts", None)
                age_s: float | None = None
                try:
                    if isinstance(ts_val, (int, float)):
                        age_s = time.time() - float(ts_val)
                except Exception:
                    age_s = None

                # Back-compat: older versions stored ISO timestamps.
                if age_s is None and isinstance(ts_val, str) and ts_val:
                    try:
                        import datetime

                        saved_time = datetime.datetime.fromisoformat(ts_val)
                        age_s = (datetime.datetime.now() - saved_time).total_seconds()
                    except Exception:
                        age_s = None

                if age_s is not None and age_s < 24 * 3600:
                    self._controls_paused = True
        except Exception:
            pass
    
    def _save_pause_state(self) -> None:
        """Persist current pause state to file."""
        if not self._state_file:
            return
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            # Preserve shared controls state fields (owner, in_control_window, etc.).
            data = {}
            try:
                if self._state_file.exists():
                    data = json.loads(self._state_file.read_text(encoding="utf-8")) or {}
            except Exception:
                data = {}
            data["paused"] = bool(self._controls_paused)
            data["ts"] = time.time()
            self._state_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception:
            pass

    # Manual pause/resume of controls (e.g., via ESC)
    def set_controls_paused(self, paused: bool) -> None:
        self._controls_paused = bool(paused)
        self._save_pause_state()

    def toggle_controls_paused(self) -> bool:
        self._controls_paused = not self._controls_paused
        self._save_pause_state()
        return self._controls_paused

    # Extra status helpers for UI rendering convenience
    def control_phase_info(self) -> Tuple[bool, bool, float, float]:
        """Return (cycle_in_control, controls_paused, seconds_into_phase, seconds_total_phase)."""
        self._update_mouse_cycle()
        now = time.time()
        elapsed = now - self._mouse_cycle_start
        total = self._mouse_control_s if self._mouse_in_control else self._mouse_release_s
        return self._mouse_in_control, self._controls_paused, max(0.0, elapsed), float(total)
    
    def rate_limit_info(self) -> dict:
        """Return rate limit status for UI/monitoring."""
        used = self.actions_in_window()
        limit = self.limits.max_total_actions_per_min
        return {
            "actions_used": used,
            "actions_limit": limit,
            "headroom": limit - used,
            "at_limit": used >= limit,
        }

    # Window gating
    def set_window_gate(self, fn: Optional[Callable[[], bool]]):
        self._window_gate = fn

    # Idle time helper
    def idle_seconds(self) -> float:
        try:
            return max(0.0, time.time() - float(self._last_action_ts))
        except Exception:
            return 0.0

    def actions_in_window(self) -> int:
        """Return number of actions taken in the last 60 seconds."""
        now = time.time()
        cutoff = now - 60.0
        while self._action_times and self._action_times[0] < cutoff:
            self._action_times.popleft()
        return len(self._action_times)
    
    def rate_limit_headroom(self) -> int:
        """Return how many more actions are allowed before rate limit kicks in."""
        return max(0, self.limits.max_total_actions_per_min - self.actions_in_window())
