from pathlib import Path
from src.control import Controller, SafetyLimits
from src.vsbridge import VSBridge
from src.windows import WindowsManager
from src.messaging import CopilotMessenger


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
    log_send = lambda m: print("SEND:", m)
    log_plan = lambda m: print("PLAN:", m)
    win = WindowsManager()
    # Use dry_run True so we don't inject keys; this will likely trigger planning (local stub)
    vs = VSBridge(ctrl, log_send, winman=win, delay_ms=400, dry_run=True)
    rules_path = root / "config" / "policy_rules.json"
    rules = {}
    try:
        import json
        rules = json.loads(rules_path.read_text(encoding="utf-8"))
    except Exception:
        rules = {"copilot": {"send_if": {"require_vscode_focus": True, "require_controls_active": True, "allow_dry_run_send": True, "require_ocr_for_read": False}}}
    ui_state = {"files": []}
    m = CopilotMessenger(root, vs, ctrl, ocr=None, log_send=log_send, log_plan=log_plan, rules=rules, ui_state=ui_state, phi4_client=None)
    res = m.send_or_plan("Hello from messenger smoke test")
    print("Result:", res)
    imp = root / "projects" / "Self-Improve" / "improvements.md"
    if imp.exists():
        print("improvements.md updated at:", imp)


if __name__ == "__main__":
    main()
