from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, MutableMapping, Optional

from ..interfaces import Module, RunContext


def _get_match_center(data: MutableMapping[str, Any]) -> Optional[tuple[int, int]]:
    match = data.get("match")
    if not isinstance(match, dict) or not match.get("ok"):
        return None
    try:
        return int(match.get("center_x")), int(match.get("center_y"))
    except Exception:
        return None


@dataclass
class ClickMatchModule(Module):
    """Clicks the matched template center via src.control.Controller.

    Safety:
    - Does nothing in dry_run mode.
    - Respects shared controls_state.json: pauses, ownership, and staleness.

    Config section: ctx.config["act_click"]
      - enabled: bool (default True)
      - owner: str (default "orchestrator_cli")
      - stale_after_s: float (default 10.0)
      - state_file: path (default config/controls_state.json)
      - button: "left" | "right" (default "left")
    """

    name: str = "act_click"

    _owner: str = ""
    _state_file: Optional[Path] = None

    def init(self, ctx: RunContext) -> None:
        cfg = dict(ctx.config.get("act_click") or {})
        self._owner = str(cfg.get("owner") or "orchestrator_cli")
        self._state_file = Path(str(cfg.get("state_file") or "config/controls_state.json"))

    def _controls_free_or_ours(self, root: Path, stale_after_s: float) -> bool:
        from src.control_state import get_controls_state, is_state_stale, set_controls_owner

        st = get_controls_state(root) or {}
        paused = bool(st.get("paused", False))
        if paused:
            return False

        # Respect the controller's intermittent control windows when state is fresh.
        # If stale/missing, do not block (fail-open) to avoid deadlocks.
        try:
            if ("in_control_window" in st) and (not is_state_stale(st, stale_after_s)):
                if not bool(st.get("in_control_window", True)):
                    return False
        except Exception:
            pass

        owner = str(st.get("owner", "") or "")
        if owner and owner != self._owner and not is_state_stale(st, stale_after_s):
            return False

        # Best-effort acquire
        set_controls_owner(root, self._owner)
        return True

    def run_once(self, data: MutableMapping[str, Any], ctx: RunContext) -> Dict[str, Any]:
        cfg = dict(ctx.config.get("act_click") or {})
        if not bool(cfg.get("enabled", True)):
            return {"status": "skip", "payload": {}, "meta": {"reason": "disabled"}}

        center = _get_match_center(data)
        if center is None:
            return {"status": "skip", "payload": {}, "meta": {"reason": "no_match"}}

        x, y = center
        button = str(cfg.get("button") or "left")
        stale_after_s = float(cfg.get("stale_after_s", 10.0))

        if ctx.dry_run:
            return {"status": "ok", "payload": {"click": {"would_click": True, "x": x, "y": y, "button": button}}, "meta": {"dry_run": True}}

        root = Path(str(ctx.config.get("root") or ".")).resolve()
        if not self._controls_free_or_ours(root, stale_after_s):
            return {"status": "skip", "payload": {"click": {"would_click": True, "x": x, "y": y, "button": button}}, "meta": {"reason": "controls_busy_or_paused"}}

        # Construct controller with shared state file so pause is respected.
        from src.control import Controller

        ctl = Controller(state_file=(root / self._state_file))
        ok = bool(ctl.click_at(x, y, button=button))
        return {"status": "ok" if ok else "error", "payload": {"click": {"ok": ok, "x": x, "y": y, "button": button}}, "meta": {}}

    def shutdown(self, ctx: RunContext) -> None:
        # Best-effort release if we own it.
        try:
            from src.control_state import set_controls_owner

            root = Path(str(ctx.config.get("root") or ".")).resolve()
            set_controls_owner(root, "")
        except Exception:
            pass
