from __future__ import annotations

import json
import os
import re
import time
import atexit
from pathlib import Path

from src.control import Controller, SafetyLimits
from src.windows import WindowsManager
from src.vsbridge import VSBridge


def _make_ocr(log_fn):
    from src.ocr import CopilotOCR  # type: ignore

    root = Path(__file__).resolve().parent.parent
    cfg_path = root / "config" / "ocr.json"
    cfg = {}
    if cfg_path.exists():
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        except Exception:
            cfg = {}
    debug_dir = root / "logs" / "ocr"
    return CopilotOCR(cfg, log=log_fn, debug_dir=debug_dir)


def _maybe_run_cleanup(root: Path) -> None:
    """Run a single cleanup pass based on config/policy_rules.json.

    Disable via env AI_CONTROLLER_RUN_CLEANUP=0.
    """
    try:
        if str(os.environ.get("AI_CONTROLLER_RUN_CLEANUP", "1")).strip().lower() in {"0", "false", "no"}:
            return
        cfg_path = root / "config" / "policy_rules.json"
        cfg = {}
        try:
            if cfg_path.exists():
                cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        except Exception:
            cfg = {}

        cleanup_cfg = (cfg.get("cleanup") or {}) if isinstance(cfg, dict) else {}
        if not bool(cleanup_cfg.get("enabled", True)):
            return

        from src.cleanup import FileCleaner  # type: ignore

        cleaner = FileCleaner(
            base=root,
            dirs=cleanup_cfg.get("dirs", ["logs/ocr"]),
            patterns=cleanup_cfg.get("patterns", ["*.png", "*.jpg"]),
            retain_seconds=int(cleanup_cfg.get("retain_seconds", 30)),
            logger=None,
            rules=cleanup_cfg.get("rules"),
        )
        cleaner.clean_once()
    except Exception:
        return


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    out_dir = root / "logs" / "tests"
    out_dir.mkdir(parents=True, exist_ok=True)

    # Proactively cleanup at start to avoid picture buildup across repeated runs.
    # Disable via env AI_CONTROLLER_RUN_CLEANUP_START=0 (and/or AI_CONTROLLER_RUN_CLEANUP=0).
    try:
        if str(os.environ.get("AI_CONTROLLER_RUN_CLEANUP_START", "1")).strip().lower() not in {"0", "false", "no"}:
            _maybe_run_cleanup(root)
    except Exception:
        pass

    atexit.register(lambda: _maybe_run_cleanup(root))

    # Make env-tunables visible in the report
    use_sendkeys = os.environ.get("COPILOT_USE_SENDKEYS", "0")
    tab = int(os.environ.get("COPILOT_COPY_TAB", "6"))
    shift_tab = int(os.environ.get("COPILOT_COPY_SHIFT_TAB", "0"))
    tab_cycle = int(os.environ.get("COPILOT_COPY_TAB_CYCLE", "12"))
    max_walk = int(os.environ.get("COPILOT_COPY_MAX_WALK", "40"))

    limits = SafetyLimits(max_clicks_per_min=120, max_keys_per_min=240)
    ctrl = Controller(mouse_speed=0.25, limits=limits, mouse_control_seconds=0, mouse_release_seconds=0)

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
    winman = WindowsManager()

    def _log(msg: str) -> None:
        print(msg)

    vb = VSBridge(ctrl=ctrl, logger=_log, winman=winman, delay_ms=300, dry_run=False)
    ocr = _make_ocr(_log)

    # Read what's currently visible in Copilot app and pick a target substring.
    cap = vb.read_copilot_app_text(ocr, save_dir=root / "logs" / "ocr", return_meta=True) or {}
    elems = (cap.get("elements") or []) if isinstance(cap, dict) else []
    # We no longer rely on OCR text to select targets; fall back to generic copy mode.
    token = ""
    hex12 = ""

    target = token or hex12
    # If we can't find a target substring, still exercise the copy workflow in
    # "generic copy" mode (expect_substring=''), so we actually perform Shift+Tab→Enter.
    if not target:
        print("No PROMPT_TOKEN_* or 12-hex candidate found. Running generic copy attempt.")
    else:
        print(f"Target substring: {target}")
    copy_res = vb.copy_copilot_app_selected_message(
        ocr,
        expect_substring=(target or ""),
        save_dir=root / "logs" / "ocr",
        max_page_down=12,
        tab_count=tab,
        shift_tab_count=shift_tab,
        tab_cycle_limit=tab_cycle,
        max_focus_walk=max_walk,
        use_enter_copy_button=str(os.environ.get("COPILOT_COPY_USE_ENTER", "1")).strip() in {"1", "true", "yes"},
        copy_retries=2,
    )

    # Collect clipboard contents for debugging (truncated)
    clip = ""
    try:
        clip = winman.get_clipboard_text(timeout_s=0.9) or ""
    except Exception:
        clip = ""

    clipboard_path = None
    if clip and (target in (clip or "") if target else True):
        try:
            clipboard_path = out_dir / f"copilot_clipboard_{time.strftime('%Y%m%d_%H%M%S')}.txt"
            clipboard_path.write_text(clip or "", encoding="utf-8", errors="replace")
        except Exception:
            clipboard_path = None

    report = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "ok": bool(copy_res.get("ok")),
        "reason": None if target else "generic_copy_no_target",
        "token": token,
        "hex12": hex12,
        "target": target,
        "use_sendkeys": use_sendkeys,
        "tab": tab,
        "shift_tab": shift_tab,
        "tab_cycle": tab_cycle,
        "max_walk": max_walk,
        "baseline": {
            "chars": len(text),
            "preview": text[:300],
            "image_path": (cap.get("image_path") or "") if isinstance(cap, dict) else "",
            "elements_count": len((cap.get("elements") or [])) if isinstance(cap, dict) else 0,
            "method": (cap.get("method") or "") if isinstance(cap, dict) else "",
        },
        "copy": copy_res,
        "clipboard_path": str(clipboard_path) if clipboard_path else None,
        "clipboard": {
            "chars": len(clip),
            "preview": clip[:500],
            "contains_target": bool(target and target in clip),
        },
    }

    out = out_dir / f"copilot_copy_smoke_{time.strftime('%Y%m%d_%H%M%S')}.json"
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"Wrote: {out}")

    return 0 if report["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
