from pathlib import Path

from src.control import Controller, SafetyLimits
from src.vsbridge import VSBridge
from src.windows import WindowsManager
from src.jsonlog import JsonActionLogger
from src.agent_terminal import TerminalAgent


def main():
    root = Path(__file__).resolve().parent.parent
    limits = SafetyLimits(max_clicks_per_min=60, max_keys_per_min=120)
    ctrl = Controller(mouse_speed=0.3, limits=limits, mouse_control_seconds=10, mouse_release_seconds=5)

    # Only act when no other workflow owns controls.
    try:
        from src.control_state import get_controls_state  # type: ignore
    except Exception:
        get_controls_state = None  # type: ignore
    if get_controls_state is not None:
        def _controls_gate() -> bool:
            try:
                st = get_controls_state(root) or {}
                owner = str(st.get("owner", "") or "")
                return not owner
            except Exception:
                return True

        ctrl.set_window_gate(_controls_gate)
    log = lambda m: print(m)
    win = WindowsManager()
    vs = VSBridge(ctrl, log, winman=win, delay_ms=400, dry_run=False)
    action_log = JsonActionLogger(root / "logs" / "actions" / "agent_run_module_smoke.jsonl")
    term = TerminalAgent(root, vs, action_log)

    print("Running scripts.test_module via TerminalAgent in VS Code terminal...")
    ok = term.run_python_module("scripts.test_module")
    action_log.log("smoke_run_module", module="scripts.test_module", ok=ok)
    print(f"Run module OK: {ok}")


if __name__ == "__main__":
    main()
