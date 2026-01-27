from __future__ import annotations
import json
import hashlib
import re
import time
import os
import atexit
import subprocess
import sys
from pathlib import Path
from typing import Any

from src.jsonlog import JsonActionLogger
from ocr_guard import InputGuard, OCREngine


def _logger() -> JsonActionLogger:
    root = Path(__file__).resolve().parent.parent
    return JsonActionLogger(root / "logs" / "errors" / "events.jsonl")


def _load_cfg() -> dict:
    root = Path(__file__).resolve().parent.parent
    cfg_path = root / "config" / "ocr.json"
    cfg = {}
    if cfg_path.exists():
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        except Exception:
            cfg = {}
    return cfg


def _make_ocr(log_fn) -> Any:
    from src.ocr import CopilotOCR  # type: ignore
    root = Path(__file__).resolve().parent.parent
    debug_dir = root / "logs" / "ocr"
    return CopilotOCR(_load_cfg(), log=log_fn, debug_dir=debug_dir)


def _maybe_run_cleanup(root: Path) -> None:
    """Run a single cleanup pass based on config/policy_rules.json.

    This keeps artifact growth bounded when running standalone scripts.
    Disable via env AI_CONTROLLER_RUN_CLEANUP=0.
    """
    attach_failed_soft = False
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
        res = cleaner.clean_once()
        try:
            _logger().log(
                "post_run_cleanup",
                scanned=int(res.get("scanned") or 0),
                deleted=int(len(res.get("deleted") or [])),
            )
        except Exception:
            pass
    except Exception:
        return


class _CleanupDaemon:
    def __init__(self, root: Path):
        self.root = root
        self.proc: subprocess.Popen | None = None
        self.log_path: Path | None = None

    def start(self) -> bool:
        """Start Scripts/cleanup_daemon.py in the background.

        Disable via env AI_CONTROLLER_CLEANUP_DAEMON=0.
        """
        try:
            if str(os.environ.get("AI_CONTROLLER_CLEANUP_DAEMON", "1")).strip().lower() in {"0", "false", "no"}:
                return False
            if self.proc and self.proc.poll() is None:
                return True

            interval_s = float(os.environ.get("AI_CONTROLLER_CLEANUP_DAEMON_INTERVAL", "5"))
            retain_s = int(os.environ.get("AI_CONTROLLER_CLEANUP_DAEMON_RETAIN", "5"))
            actions_dir = self.root / "logs" / "actions"
            actions_dir.mkdir(parents=True, exist_ok=True)
            ts = time.strftime("%Y%m%d_%H%M%S")
            self.log_path = actions_dir / f"cleanup_daemon_{ts}.log"

            script_path = self.root / "Scripts" / "cleanup_daemon.py"
            cmd = [
                str(sys.executable),
                str(script_path),
                "--interval",
                str(interval_s),
                "--retain",
                str(retain_s),
            ]
            flags = int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
            with open(self.log_path, "a", encoding="utf-8") as fp:
                fp.write("CMD: " + " ".join(cmd) + "\n")
                fp.flush()
                self.proc = subprocess.Popen(
                    cmd,
                    cwd=str(self.root),
                    stdout=fp,
                    stderr=fp,
                    creationflags=flags,
                )
            _logger().log(
                "cleanup_daemon_started",
                ok=True,
                pid=int(self.proc.pid) if self.proc else 0,
                interval_s=float(interval_s),
                retain_s=int(retain_s),
                log_path=str(self.log_path) if self.log_path else "",
                command=" ".join(cmd),
            )
            return True
        except Exception as e:
            try:
                _logger().log("cleanup_daemon_started", ok=False, error=str(e))
            except Exception:
                pass
            return False

    def stop(self) -> bool:
        """Stop the background cleanup daemon."""
        try:
            if not self.proc:
                return True
            if self.proc.poll() is not None:
                return True
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3.0)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
            ok = self.proc.poll() is not None
            _logger().log(
                "cleanup_daemon_stopped",
                ok=bool(ok),
                pid=int(self.proc.pid) if self.proc else 0,
                returncode=int(self.proc.returncode) if (self.proc and self.proc.returncode is not None) else None,
            )
            return bool(ok)
        except Exception as e:
            try:
                _logger().log("cleanup_daemon_stopped", ok=False, error=str(e))
            except Exception:
                pass
            return False


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    out_dir = root / "logs" / "tests"
    out_dir.mkdir(parents=True, exist_ok=True)

    # Shared controls gate: if another workflow owns controls, yield.
    try:
        from src.control_state import get_controls_state, set_controls_owner  # type: ignore
    except Exception:
        get_controls_state = None  # type: ignore
        set_controls_owner = None  # type: ignore

    orig_owner: str | None = None
    if get_controls_state is not None and set_controls_owner is not None:
        try:
            st = get_controls_state(root) or {}
            owner = str(st.get("owner", "") or "")
            orig_owner = owner or None
            if owner and owner != "copilot_app_test":
                # Another workflow (e.g., Agent Mode) currently owns controls; soft-yield.
                try:
                    print("Controls owned by another workflow; skipping Copilot app interaction test.", flush=True)
                except Exception:
                    pass
                return 0
            # Mark ourselves as the temporary owner for the duration of this test.
            set_controls_owner(root, "copilot_app_test")
        except Exception:
            orig_owner = None

    # Ensure we restore the original owner on normal/abrupt exit as well.
    def _restore_owner() -> None:
        try:
            if 'set_controls_owner' in globals() and set_controls_owner is not None:
                set_controls_owner(root, orig_owner)
        except Exception:
            pass
    atexit.register(_restore_owner)

    try:
        print("Starting Copilot app interaction test...", flush=True)
    except Exception:
        pass

    # Policy toggles
    # By default, exercise the full workflow:
    # - attach/upload a txt file
    # - send a prompt
    # - wait for response
    # - navigate to and copy the Copilot reply.
    # These can be disabled via environment variables when needed.
    do_attach = str(os.environ.get("COPILOT_APP_ATTACH_MODULES", "1")).strip().lower() in {"1", "true", "yes"}
    send_prompt = str(os.environ.get("COPILOT_APP_SEND_PROMPT", "1")).strip().lower() in {"1", "true", "yes"}

    # Compose and log the intended objective for this run (goal-driven navigation).
    try:
        steps: list[str] = ["open_most_recent_conversation"]
        if do_attach:
            steps.append("upload_txt_attachment")
        if send_prompt:
            steps.extend(["type_message", "press_enter_send", "wait_for_response", "navigate_to_copy_button"])
        _logger().log(
            "workflow_intent",
            reason="gather_info",
            objective="copilot_app_conversation_workflow",
            steps=steps,
        )
    except Exception:
        pass

    def _step_begin(step: str, **kw) -> None:
        try:
            print(f"STEP: {step}", flush=True)
        except Exception:
            pass
        try:
            _logger().log("workflow_step_begin", step=step, **kw)
        except Exception:
            pass

    def _step_end(step: str, ok: bool, **kw) -> None:
        try:
            _logger().log("workflow_step_end", step=step, ok=bool(ok), **kw)
        except Exception:
            pass

    # Start cleanup daemon early to prevent OCR/screenshot buildup during long runs.
    cleanup_daemon = _CleanupDaemon(root)
    cleanup_daemon.start()
    atexit.register(lambda: cleanup_daemon.stop())

    # Proactively cleanup at start to avoid picture buildup across repeated runs.
    # Disable via env AI_CONTROLLER_RUN_CLEANUP_START=0 (and/or AI_CONTROLLER_RUN_CLEANUP=0).
    try:
        if str(os.environ.get("AI_CONTROLLER_RUN_CLEANUP_START", "1")).strip().lower() not in {"0", "false", "no"}:
            _maybe_run_cleanup(root)
    except Exception:
        pass

    # Ensure we don't accumulate OCR/screen artifacts when running this script directly.
    atexit.register(lambda: _maybe_run_cleanup(root))

    # Defaults tuned for "Shift+Tab a couple times then Enter" copy workflow.
    os.environ.setdefault("COPILOT_USE_SENDKEYS", "1")
    os.environ.setdefault("COPILOT_COPY_SHIFT_TAB", "2")
    os.environ.setdefault("COPILOT_COPY_TAB", "0")
    os.environ.setdefault("COPILOT_COPY_USE_ENTER", "1")
    os.environ.setdefault("COPILOT_COPY_USE_UIA", "1")
    os.environ.setdefault("COPILOT_COPY_PREFER_UI_COPY", "1")
    os.environ.setdefault("COPILOT_COPY_SMART_NAV", "1")
    os.environ.setdefault("COPILOT_COPY_SMART_STEPS", "60")
    os.environ.setdefault("COPILOT_COPY_ARROW_DOWN_TO_MESSAGES", "2")
    os.environ.setdefault("COPILOT_COPY_ITEM_ARROW_RIGHT", "1")
    os.environ.setdefault("COPILOT_COPY_ITEM_THEN_TAB", "1")
    os.environ.setdefault("COPILOT_COPY_ITEM_ARROW_DOWN", "1")
    os.environ.setdefault("COPILOT_COPY_ACTION_TAB_STEPS", "6")
    os.environ.setdefault("COPILOT_COPY_USE_ARROWS", "1")
    os.environ.setdefault("COPILOT_COPY_ARROW_RIGHT", "2")
    os.environ.setdefault("COPILOT_COPY_ARROW_LEFT", "1")
    os.environ.setdefault("COPILOT_COPY_ARROW_DOWN", "2")
    os.environ.setdefault("COPILOT_COPY_ARROW_UP", "0")
    os.environ.setdefault("COPILOT_COPY_ARROW_MAX_WALK", "10")
    os.environ.setdefault("COPILOT_COPY_TOOLTIP_MS", "450")
    os.environ.setdefault("COPILOT_APP_SETTLE_MS", "200")

    # Build minimal control/winman/log interfaces expected by VSBridge
    from src.control import Controller, SafetyLimits  # type: ignore
    from src.windows import WindowsManager  # type: ignore
    limits = SafetyLimits(max_clicks_per_min=120, max_keys_per_min=240)
    # Disable intermittent control cycling for this deterministic interaction test.
    ctrl = Controller(mouse_speed=0.25, limits=limits, mouse_control_seconds=0, mouse_release_seconds=0)
    winman = WindowsManager()

    def _log(msg: str) -> None:
        print(msg)

    from src.vsbridge import VSBridge
    vb = VSBridge(ctrl=ctrl, logger=_log, winman=winman, delay_ms=400, dry_run=False)
    guard = InputGuard(OCREngine(), root / "logs" / "events.jsonl")

    # Create a fresh module_composer summary and (optionally) attach it to Copilot.
    modules_attachment: Path | None = None
    attach_failed_soft = False
    try:
        import sys

        try:
            print("Building ModulesList attachment...", flush=True)
        except Exception:
            pass

        # Allow importing module_composer.py from repo root.
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        import module_composer  # type: ignore

        ts = time.strftime("%Y%m%d_%H%M%S")
        modules_attachment = out_dir / f"ModulesIndex_agent_{ts}.txt"
        include_glob = [
            "README*",
            "src/*.py",
            "config/*",
            "docs/*.md",
            "Scripts/*.py",
            "Scripts/*.ps1",
        ]
        module_composer.compose_modules_index_json(
            str(modules_attachment),
            str(root),
            include_globs=include_glob,
        )
        _logger().log(
            "copilot_app_modules_list_built",
            path=str(modules_attachment),
            bytes=int(modules_attachment.stat().st_size) if modules_attachment.exists() else 0,
        )
        try:
            print(f"Attachment ready: {modules_attachment}", flush=True)
        except Exception:
            pass
    except Exception as e:
        modules_attachment = None
        _logger().log("copilot_app_modules_list_build_failed", error=str(e))
        try:
            print(f"Attachment build failed: {e}", flush=True)
        except Exception:
            pass

    # Respect safety policy: do not send unrelated prompts when the primary attach workflow fails.
    do_attach = str(os.environ.get("COPILOT_APP_ATTACH_MODULES", "1")).strip().lower() in {"1", "true", "yes"}
    send_prompt = str(os.environ.get("COPILOT_APP_SEND_PROMPT", "0")).strip().lower() in {"1", "true", "yes"}
    if do_attach and (modules_attachment is None or (not modules_attachment.exists())):
        _logger().log("workflow_abort", reason="attachment_build_failed")
        return 1

    # Focus Copilot app and send a useful prompt with a verification token.
    # To prove we received a *reply* (not just our own prompt echoed in OCR),
    # ask Copilot to compute a hash of the token and return it.
    token = f"PROMPT_TOKEN_{int(time.time())}"
    expected = hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]

    # If requested, attach the freshly generated module list before sending.
    # This also exercises the app navigation on a "real" self-improve artifact.
    try:
        if do_attach and modules_attachment and modules_attachment.exists():
            _step_begin("open_most_recent_conversation", phase="before_attach")
            try:
                vb.focus_copilot_app()
                # Ensure the app is in a sendable state (conversation open if needed).
                try:
                    ok_prep = bool(vb._copilot_app_prepare_for_send())  # type: ignore[attr-defined]
                    _logger().log("workflow_step", step="open_most_recent_conversation", ok=bool(ok_prep))
                    if not ok_prep:
                        _logger().log("workflow_abort", reason="prepare_for_send_failed_before_attach")
                        _step_end("open_most_recent_conversation", False)
                        return 1
                    _step_end("open_most_recent_conversation", True)
                except Exception:
                    pass
            except Exception:
                pass

            _step_begin("upload_txt_attachment", path=str(modules_attachment))
            attached_ok = bool(
                vb.attach_file_to_copilot_app(
                    str(modules_attachment),
                    # Newer Copilot layouts reach the attach gateway in fewer Tab steps.
                    # Use a conservative default (4) but allow override via env.
                    tab_count=int(os.environ.get("COPILOT_APP_ATTACH_TAB_COUNT", "4")),
                    down_count=1,
                    ocr=_make_ocr(_log),
                    save_dir=root / "logs" / "ocr",
                )
            )
            _logger().log("copilot_app_attach_modules", ok=bool(attached_ok), path=str(modules_attachment))
            _logger().log("workflow_step", step="upload_txt_attachment", ok=bool(attached_ok))
            if not attached_ok:
                # Soft-fail: in newer Copilot layouts the attach entry point may not
                # open a classic file picker. Record the failure but continue with
                # the send/wait/copy workflow so the rest of the pipeline is still
                # exercised under control.
                attach_failed_soft = True
                _logger().log("workflow_warn", reason="attachment_failed_soft")
                _step_end("upload_txt_attachment", False)
            else:
                _step_end("upload_txt_attachment", True)
    except Exception as e:
        _logger().log("copilot_app_attach_modules_error", error=str(e))
        if do_attach:
            # Also treat unexpected exceptions as soft failures so that the
            # remainder of the workflow (send/wait/copy) can still be validated.
            attach_failed_soft = True
            _logger().log("workflow_warn", reason="attachment_exception_soft")

    if not send_prompt:
        _logger().log("workflow_end", note="COPILOT_APP_SEND_PROMPT disabled; attach-only run")
        return 0

    prompt = (
        "Testing: AI_Coder_Controller Self-Improve workflow (this is a test). "
        "Compute SHA256 of the following token. "
        "On the FIRST line of your reply, output ONLY the first 12 hex characters in the format: XXXX XXXX XXXX (3 groups of 4). "
        f"TOKEN: {token}. "
        "Then provide 3 concrete suggestions to improve the AI_Coder_Controller self-improve workflow. "
        "If an attachment is present with a module/file list, base your suggestions on what you see there."
    )

    # Foreground gate: Copilot app must be active and an OCR pre-observe must succeed.
    # NOTE: Copilot window titles may be conversation names (e.g. "SHA256 Hash Request"),
    # so we validate by process name as well.
    def _is_copilot_foreground() -> tuple[bool, dict]:
        fg_hwnd = winman.get_foreground()
        fg_info = winman.get_window_info(fg_hwnd) if fg_hwnd else {}
        title_l = (fg_info.get("title") or "").lower()
        proc_l = (fg_info.get("process") or "").lower()
        # Fail-closed: VS Code and other Electron windows can contain the word "Copilot".
        if proc_l and (proc_l == "code.exe" or proc_l.startswith("code")):
            return False, fg_info
        ok_fg = ("copilot" in proc_l) or ("copilot" in title_l)
        return ok_fg, fg_info

    ok_fg, fg_info = _is_copilot_foreground()
    # Retry focus and verify foreground up to 3 times
    attempts = 0
    while (not ok_fg) and attempts < 3:
        attempts += 1
        _logger().log(
            "copilot_focus_attempt",
            attempt=attempts,
            title=(fg_info.get("title") or ""),
            cls=(fg_info.get("class") or ""),
            process=(fg_info.get("process") or ""),
        )
        try:
            vb.focus_copilot_app()
            time.sleep(0.8)
        except Exception as e:
            _logger().log("copilot_focus_attempt_error", attempt=attempts, error=str(e))
            time.sleep(0.5)
        ok_fg, fg_info = _is_copilot_foreground()

    if not ok_fg:
        _logger().log(
            "copilot_app_not_foreground_when_send",
            title=(fg_info.get("title") or ""),
            cls=(fg_info.get("class") or ""),
            process=(fg_info.get("process") or ""),
        )
        return 1
    # 1. OCR observe cursor location
    if not guard.before_text():
        _logger().log("ocr_precheck_failed_before_send", phase="copilot_app_send")
        return 1
    _logger().log("ocr_cursor_observed_before_send")
    # 2. OCR observe picture of text input (save via app OCR with input ROI if available)
    _logger().log("copilot_app_input_picture_observe_begin")
    _ = vb.read_copilot_app_text(_make_ocr(_log), save_dir=root / "logs" / "ocr")
    _logger().log("copilot_app_input_picture_observe_done")
    # Baseline OCR before send
    ocr = _make_ocr(_log)
    baseline_app = vb.read_copilot_app_text(ocr, save_dir=root / "logs" / "ocr") or ""
    _logger().log("copilot_app_baseline", chars=len(baseline_app))

    _logger().log("copilot_app_send_begin", prompt_chars=len(prompt), expected_hash=expected)
    # Ensure Copilot app is truly foreground before send (Win+C can open in background).
    if not vb.focus_copilot_app():
        fg = winman.get_foreground()
        info = winman.get_window_info(fg) if fg else {}
        _logger().log(
            "copilot_app_not_foreground_when_send",
            title=(info.get("title") or ""),
            cls=(info.get("class") or ""),
            note="focus_copilot_app() failed strict foreground acquisition",
        )
        return 1

    _step_begin("type_message", prompt_chars=len(prompt))
    _step_begin("press_enter_send")
    ok = vb.ask_copilot_app(prompt)
    _logger().log("copilot_app_send_result", ok=bool(ok))
    _step_end("press_enter_send", bool(ok))
    _step_end("type_message", bool(ok))
    time.sleep(1.5)

    # Detect misdirection without thrashing focus: avoid switching to VS Code mid-run.
    # We'll do a VS Code OCR check only at the end if verification fails.
    try:
        app_after = vb.read_copilot_app_text(ocr, save_dir=root / "logs" / "ocr") or ""
    except Exception:
        app_after = ""

    token_in_app = token in (app_after or "")
    token_in_vscode = False

    # Response loop with retries. We require the expected hash to appear in OCR.
    _step_begin("wait_for_response", expected_hash=expected)
    _logger().log("copilot_app_read_begin", expected_hash=expected)
    # Wait long enough for Copilot to respond; observe via OCR; react periodically.
    text = vb.wait_for_copilot_app_reply(
        ocr,
        expect_substring=expected,
        timeout_s=50.0,
        interval_s=2.0,
        react_every=3,
        save_dir=root / "logs" / "ocr",
    ) or ""
    def _best_hash_match(ocr_text: str, expected_hex12: str) -> tuple[bool, str | None, int | None]:
        raw = ocr_text or ""
        # Accept either a contiguous 12-hex string or a grouped 4-4-4 hex pattern.
        candidates: list[str] = []
        candidates.extend(re.findall(r"\b[0-9a-fA-F]{12}\b", raw))
        grouped = re.findall(r"\b[0-9a-fA-F]{4}\s+[0-9a-fA-F]{4}\s+[0-9a-fA-F]{4}\b", raw)
        candidates.extend([re.sub(r"\s+", "", g) for g in grouped])
        if not candidates:
            return False, None, None

        expected_norm = expected_hex12.lower()
        best_candidate: str | None = None
        best_distance: int | None = None
        for c in candidates:
            c_norm = c.lower()
            if len(c_norm) != len(expected_norm):
                continue
            dist = sum(1 for a, b in zip(c_norm, expected_norm) if a != b)
            if best_distance is None or dist < best_distance:
                best_distance = dist
                best_candidate = c
                if best_distance == 0:
                    break
        ok = best_distance is not None and best_distance <= 1
        return ok, best_candidate, best_distance

    saw_token = token in (text or "")
    saw_expected_exact = expected in (text or "")
    saw_expected_fuzzy, best_candidate, best_distance = _best_hash_match(text or "", expected)
    saw_expected = saw_expected_exact or saw_expected_fuzzy
    _logger().log("copilot_app_read_result", chars=len(text or ""), saw_token=bool(saw_token), saw_expected=bool(saw_expected))
    _step_end("wait_for_response", bool(saw_expected), chars=len(text or ""))

    # Optional stronger evidence: select a message (PageDown), OCR-confirm expected hash is visible, then copy.
    clipboard_ok = False
    clipboard_copy = None
    clipboard_copy_generic = None
    clipboard_path = None
    if text:
        try:
            _step_begin("navigate_to_copy_button")
            clipboard_copy = vb.copy_copilot_app_selected_message(
                ocr,
                # Prefer expected hash as the gating substring; the token line can be OCR-flaky.
                expect_substring=expected,
                save_dir=root / "logs" / "ocr",
                max_page_down=12,
                # Tunables: Tab / Shift+Tab can navigate to message focus.
                tab_count=int(os.environ.get("COPILOT_COPY_TAB", "6")),
                shift_tab_count=int(os.environ.get("COPILOT_COPY_SHIFT_TAB", "0")),
                tab_cycle_limit=int(os.environ.get("COPILOT_COPY_TAB_CYCLE", "12")),
                max_focus_walk=int(os.environ.get("COPILOT_COPY_MAX_WALK", "40")),
                use_enter_copy_button=str(os.environ.get("COPILOT_COPY_USE_ENTER", "1")).strip() in {"1", "true", "yes"},
                copy_retries=2,
            )

            # If the expected-hash-gated copy fails because the expected substring isn't visible,
            # do a generic copy attempt to still exercise the Shift+Tab→Enter Copy workflow and
            # capture clipboard evidence.
            try:
                err = str((clipboard_copy or {}).get("error") or "")
            except Exception:
                err = ""
            if err in {"expected_not_observed_before_copy", "lost_expected_before_copy"}:
                clipboard_copy_generic = vb.copy_copilot_app_selected_message(
                    ocr,
                    expect_substring="",
                    save_dir=root / "logs" / "ocr",
                    max_page_down=12,
                    tab_count=int(os.environ.get("COPILOT_COPY_TAB", "0")),
                    shift_tab_count=int(os.environ.get("COPILOT_COPY_SHIFT_TAB", "2")),
                    tab_cycle_limit=int(os.environ.get("COPILOT_COPY_TAB_CYCLE", "12")),
                    max_focus_walk=int(os.environ.get("COPILOT_COPY_MAX_WALK", "40")),
                    use_enter_copy_button=str(os.environ.get("COPILOT_COPY_USE_ENTER", "1")).strip() in {"1", "true", "yes"},
                    copy_retries=2,
                )

            # Re-read full clipboard (preview is truncated); use winman for full check.
            full_clip = ""
            try:
                full_clip = winman.get_clipboard_text(timeout_s=0.9) or ""
            except Exception:
                full_clip = ""

            # Accept clipboard verification if it includes the expected hash.
            # (Token presence is not required; the token line is sometimes missing in OCR/UI.)
            clip_norm = (full_clip or "")
            expected_norm = expected.lower()
            clip_hex12 = re.sub(r"[^0-9a-fA-F]", "", clip_norm).lower()
            clipboard_ok = (expected_norm in clip_norm.lower()) or (expected_norm in clip_hex12)

            if clipboard_ok:
                try:
                    out_dir.mkdir(parents=True, exist_ok=True)
                    clipboard_path = out_dir / f"copilot_clipboard_{time.strftime('%Y%m%d_%H%M%S')}.txt"
                    clipboard_path.write_text(full_clip or "", encoding="utf-8", errors="replace")
                except Exception:
                    clipboard_path = None
            _logger().log(
                "copilot_app_clipboard_check",
                ok=bool(clipboard_ok),
                chars=len(full_clip or ""),
            )
            _step_end("navigate_to_copy_button", bool(clipboard_ok), clipboard_chars=len(full_clip or ""))
        except Exception as e:
            _logger().log("copilot_app_clipboard_error", error=str(e))
            clipboard_ok = False
            _step_end("navigate_to_copy_button", False, error=str(e))

    # OCR + evidence workflow finished; stop daemon now (atexit is a backstop).
    try:
        cleanup_daemon.stop()
    except Exception:
        pass
    # Release shared controls ownership (best-effort).
    try:
        if set_controls_owner is not None:
            set_controls_owner(root, orig_owner)
    except Exception:
        pass

    passed = bool(text and (saw_expected or clipboard_ok))

    # Save a small report
    report = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "ok": bool(text != ""),
        "token": token,
        "token_found": token in (text or ""),
        "expected_hash": expected,
        "expected_found": saw_expected_exact,
        "expected_found_fuzzy": bool(saw_expected_fuzzy and not saw_expected_exact),
        "expected_best_candidate": best_candidate,
        "expected_best_distance": best_distance,
        "clipboard_ok": bool(clipboard_ok),
        "clipboard_path": str(clipboard_path) if clipboard_path else None,
        "clipboard_copy": clipboard_copy or None,
        "clipboard_copy_generic": clipboard_copy_generic or None,
        "baseline_chars": len(baseline_app or ""),
        "token_in_app_after_send": bool(token_in_app),
        "token_in_vscode_after_send": bool(token_in_vscode),
        "attach_failed_soft": bool(attach_failed_soft),
        "chars": len(text or ""),
        "preview": (text or "")[:400],
    }
    out = out_dir / f"copilot_app_interaction_{time.strftime('%Y%m%d_%H%M%S')}.json"
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"Copilot app interaction report: {out}")

    # On failure, do a one-shot VS Code chat OCR check to see if we sent to ourselves.
    if not passed:
        try:
            vscode_after = vb.read_copilot_chat_text(ocr, save_dir=root / "logs" / "ocr") or ""
            token_in_vscode = token in (vscode_after or "")
        except Exception:
            token_in_vscode = False
        if token_in_vscode and (not token_in_app):
            _logger().log(
                "copilot_app_send_misdirected",
                expected_target="copilot_app",
                observed_target="vscode_chat",
                token=token,
                note="Token found in VS Code chat OCR but not in Copilot app OCR",
            )
            _logger().log(
                "copilot_app_verify_failed",
                reason="misdirected_to_vscode_chat",
                expected_target="copilot_app",
                token=token,
            )
            return 1
        _logger().log(
            "copilot_app_verify_failed",
            reason="expected_hash_missing",
            expected_hash=expected,
            best_candidate=best_candidate,
            best_distance=best_distance,
            saw_token=saw_token,
            chars=len(text or ""),
        )
        return 1
    _logger().log("copilot_app_gathered", token=token, chars=len(text or ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
