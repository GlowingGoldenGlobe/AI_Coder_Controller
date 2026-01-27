from __future__ import annotations
import json
import subprocess
import sys
import time
import atexit
import os
from pathlib import Path
import glob


def run_cmd(cmd: list[str], cwd: Path) -> dict:
    t0 = time.time()
    try:
        proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, check=False)
        return {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "stdout": proc.stdout[-4000:],
            "stderr": proc.stderr[-4000:],
            "seconds": round(time.time() - t0, 2),
            "cmd": cmd,
        }
    except KeyboardInterrupt:
        # Important: this script runs GUI automation subprocesses which can
        # misdirect keystrokes into the terminal. If a stray Ctrl+C hits this
        # process, capture it as a failed step rather than aborting silently.
        return {
            "ok": False,
            "returncode": 130,
            "stderr": "KeyboardInterrupt (SIGINT)",
            "seconds": round(time.time() - t0, 2),
            "cmd": cmd,
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "seconds": round(time.time() - t0, 2), "cmd": cmd}


def main():
    root = Path(__file__).resolve().parent.parent
    py = str(root / "Scripts" / "python.exe")
    logs = root / "logs" / "tests"
    logs.mkdir(parents=True, exist_ok=True)

    # Shared controls gating: only run the workflow when no other
    # owner is active, and mark ourselves as the temporary owner.
    orig_owner: str | None = None
    try:
        from src.control_state import get_controls_state, set_controls_owner  # type: ignore
    except Exception:
        get_controls_state = None  # type: ignore
        set_controls_owner = None  # type: ignore

    if get_controls_state is not None and set_controls_owner is not None:
        try:
            st = get_controls_state(root) or {}
            owner = str(st.get("owner", "") or "")
            orig_owner = owner or None
            if owner and owner != "workflow_test":
                print("Controls owned by another workflow; skipping workflow_test_gather_assess run.")
                return 0
            set_controls_owner(root, "workflow_test")
        except Exception:
            orig_owner = None
    # Optional policy/config gating for expensive or environment-specific steps
    rules = {}
    try:
        rules_path = root / "config" / "policy_rules.json"
        rules = json.loads(rules_path.read_text(encoding="utf-8"))
    except Exception:
        rules = {}
    workflow_cfg = (rules.get("workflow") or {}) if isinstance(rules, dict) else {}
    env_flag = os.environ.get("AI_CONTROLLER_ENABLE_COPILOT_APP_INTERACTION")
    if env_flag is not None:
        enable_copilot_app_step = env_flag.strip().lower() in {"1", "true", "yes"}
    else:
        enable_copilot_app_step = bool(workflow_cfg.get("enable_copilot_app_interaction", False))
    start_ts = time.time()
    # Start cleanup daemon for run window
    cleanup_daemon = subprocess.Popen([py, "Scripts/cleanup_daemon.py", "--interval", "5", "--retain", "5"], cwd=str(root))
    summary = {
        "started": time.strftime("%Y-%m-%d %H:%M:%S"),
        "steps": [],
        "artifacts": [],
        "pass": False,
        "successes": [],
        "errors": [],
        "warnings": []
    }

    finalized = False

    interrupted = False

    def _finalize(exit_code: int) -> int:
        nonlocal finalized
        summary["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
        if interrupted:
            summary["warnings"].append({"name": "workflow_interrupted", "note": "KeyboardInterrupt observed during workflow"})
        out = logs / f"workflow_summary_{time.strftime('%Y%m%d_%H%M%S')}.json"
        out.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        print(f"Workflow summary: {out}")
        print("OVERALL:", "PASS" if summary["pass"] else "FAIL")
        try:
            cleanup_daemon.terminate()
            try:
                cleanup_daemon.wait(timeout=3)
            except Exception:
                pass
        except Exception:
            pass
        finalized = True
        return exit_code

    def _atexit_finalize() -> None:
        # Best-effort: if something (like stray Ctrl+C) interrupts the workflow,
        # ensure we still emit a summary and stop the cleanup daemon.
        nonlocal finalized
        if finalized:
            return
        try:
            summary.setdefault("pass", False)
            if "finished" not in summary:
                summary["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
            out = logs / f"workflow_summary_{time.strftime('%Y%m%d_%H%M%S')}.json"
            out.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        except Exception:
            pass
        try:
            cleanup_daemon.terminate()
        except Exception:
            pass
        finalized = True

    atexit.register(_atexit_finalize)
    # Ensure we restore the original owner on normal exit as well.
    def _restore_owner() -> None:
        try:
            if 'set_controls_owner' in globals() and set_controls_owner is not None:
                set_controls_owner(root, orig_owner)
        except Exception:
            pass
    atexit.register(_restore_owner)

    # 1) Navigation Test
    step_nav = run_cmd([py, "scripts/navigation_test.py"], root)
    step_nav_pass = bool(step_nav.get("ok", False))
    summary["steps"].append({"name": "navigation_test", "result": step_nav, "pass": step_nav_pass})
    if step_nav_pass:
        summary["successes"].append({"name": "navigation_test", "seconds": step_nav.get("seconds")})
    else:
        summary["errors"].append({"name": "navigation_test", "returncode": step_nav.get("returncode"), "stderr_tail": (step_nav.get("stderr") or "")[-800:]})

    # Attach latest navigation report artifact (if produced)
    try:
        nav_reports = sorted(glob.glob(str(root / "logs/tests/navigation_test_*.json")))
        if nav_reports:
            latest_nav = Path(nav_reports[-1])
            summary["artifacts"].append(str(latest_nav.relative_to(root)))
            try:
                nav_obj = json.loads(latest_nav.read_text(encoding="utf-8"))
                nav_ok = bool(((nav_obj.get("summary") or {}).get("ok")))
                if not nav_ok and step_nav_pass:
                    summary["errors"].append({"name": "navigation_test_soft_fail", "note": "nav report indicates failed steps", "file": str(latest_nav.relative_to(root))})
            except Exception:
                pass
    except Exception:
        pass

    # 2) OCR Commit Test (captures and appends to improvements.md)
    step_ocr = run_cmd([py, "Scripts/ocr_commit_test.py"], root)
    # consider PASS if command ok and a report exists
    reports = sorted(glob.glob(str(root / "logs/tests/ocr_commit_test_*.json")))
    pass_ocr = bool(step_ocr.get("ok", False) and len(reports) > 0)
    summary["steps"].append({"name": "ocr_commit_test", "result": step_ocr, "pass": pass_ocr, "reports": [str(Path(r).relative_to(root)) for r in reports[-3:]]})
    if pass_ocr:
        summary["successes"].append({"name": "ocr_commit_test", "reports": [str(Path(r).relative_to(root)) for r in reports[-1:]]})
    else:
        summary["errors"].append({"name": "ocr_commit_test", "returncode": step_ocr.get("returncode"), "stderr_tail": (step_ocr.get("stderr") or "")[-800:]})
    summary["artifacts"].append("projects/Self-Improve/improvements.md")

    # 2b) Gather chat evidence (explicit proof artifact)
    step_evd = run_cmd([py, "Scripts/gather_chat_evidence.py"], root)
    pass_evd = bool(step_evd.get("ok", False))
    summary["steps"].append({"name": "gather_chat_evidence", "result": step_evd, "pass": pass_evd})
    if pass_evd:
        # Locate most recent evidence file
        import glob as _glob
        evs = sorted(_glob.glob(str(root / "logs/tests/chat_evidence_*.json")))
        if evs:
            summary["successes"].append({"name": "gather_chat_evidence", "file": str(Path(evs[-1]).relative_to(root))})
            summary["artifacts"].append(str(Path(evs[-1]).relative_to(root)))
    else:
        summary["errors"].append({"name": "gather_chat_evidence", "returncode": step_evd.get("returncode"), "stderr_tail": (step_evd.get("stderr") or "")[-800:]})

    # 2c) VS Code chat keepalive (mouse/keyboard-driven button click probe)
    # This exercises the MultiWindowChatKeepalive + ChatButtonAnalyzer path,
    # which focuses VS Code windows, captures the chat ROI, and may move/click
    # the mouse on actionable chat buttons. This ensures the workflow includes
    # real input actions against Copilot UI elements, not just OCR reads.
    step_keepalive = run_cmd([py, "Scripts/vscode_multi_keepalive_daemon.py", "--interval-s", "4", "--max-cycles", "1"], root)
    pass_keepalive = bool(step_keepalive.get("ok", False))
    summary["steps"].append({"name": "vscode_multi_keepalive", "result": step_keepalive, "pass": pass_keepalive})
    if pass_keepalive:
        summary["successes"].append({"name": "vscode_multi_keepalive"})
    else:
        summary["errors"].append({"name": "vscode_multi_keepalive", "returncode": step_keepalive.get("returncode"), "stderr_tail": (step_keepalive.get("stderr") or "")[-800:]})

    # 2d) Copilot app interaction test (foreground, useful prompt, OCR verify)
    if enable_copilot_app_step:
        step_app = run_cmd([py, "Scripts/copilot_app_interaction_test.py"], root)
        pass_app = bool(step_app.get("ok", False) and step_app.get("returncode") == 0)
        summary["steps"].append({"name": "copilot_app_interaction", "result": step_app, "pass": pass_app})
        if not pass_app:
            summary["errors"].append({"name": "copilot_app_interaction", "returncode": step_app.get("returncode"), "stderr_tail": (step_app.get("stderr") or "")[-800:]})
    else:
        # When disabled via config or environment, record a skipped-but-pass step;
        # this keeps the workflow usable on machines without the Copilot app.
        summary["steps"].append({
            "name": "copilot_app_interaction",
            "pass": True,
            "skipped": True,
            "reason": "disabled via policy_rules.workflow.enable_copilot_app_interaction or AI_CONTROLLER_ENABLE_COPILOT_APP_INTERACTION",
        })
        summary["warnings"].append({"name": "copilot_app_interaction_skipped"})

    # 3) Observe & React (short)
    obs_log = str(Path("logs/tests/observe_react_workflow.jsonl"))
    step_obs = run_cmd([py, "Scripts/observe_and_react.py", "--ticks", "12", "--interval-ms", "400", "--log", obs_log], root)
    pass_obs = bool(step_obs.get("ok", False) and (root / obs_log).exists())
    summary["steps"].append({"name": "observe_and_react", "result": step_obs, "pass": pass_obs})
    if pass_obs:
        summary["successes"].append({"name": "observe_and_react", "log": obs_log})
    else:
        summary["errors"].append({"name": "observe_and_react", "returncode": step_obs.get("returncode"), "stderr_tail": (step_obs.get("stderr") or "")[-800:]})
    summary["artifacts"].append(obs_log)

    # 3b) Scan error events and remediate if needed
    def read_error_events_since(root: Path, since_ts: float) -> list[dict]:
        import json, time
        p = root / "logs" / "errors" / "events.jsonl"
        ev = []
        if not p.exists():
            return ev
        try:
            with p.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                        ts_raw = obj.get("ts")
                        ts_num = None
                        # 1) epoch float/int (some scripts log time.time())
                        if isinstance(ts_raw, (int, float)):
                            ts_num = float(ts_raw)
                        # 2) ISO string with optional milliseconds + Z
                        if ts_num is None:
                            ts_str = str(ts_raw or "").strip().replace("T", " ")
                            # take first 19 chars YYYY-MM-DD HH:MM:SS
                            if len(ts_str) >= 19:
                                ts_core = ts_str[:19]
                                try:
                                    ttuple = time.strptime(ts_core, "%Y-%m-%d %H:%M:%S")
                                    ts_num = time.mktime(ttuple)
                                except Exception:
                                    ts_num = None
                        if ts_num is None:
                            continue
                        if ts_num >= since_ts:
                            ev.append(obj)
                    except Exception:
                        pass
        except Exception:
            return ev
        return ev

    errs_before = read_error_events_since(root, start_ts)
    summary["steps"].append({"name": "scan_error_events_before", "count": len(errs_before), "samples": errs_before[-3:]})
    if errs_before:
        # Attempt remediation by running observe_and_react again briefly
        step_fix = run_cmd([py, "Scripts/observe_and_react.py", "--ticks", "6", "--interval-ms", "400", "--log", obs_log], root)
        summary["steps"].append({"name": "remediate_errors", "result": step_fix, "pass": bool(step_fix.get("ok", False))})
    errs_after = read_error_events_since(root, start_ts)
    summary["steps"].append({"name": "scan_error_events_after", "count": len(errs_after), "samples": errs_after[-3:]})

    # 3b-2) Classify error-like events (not all events are failures)
    error_like = {
        "terminal_focus_failed",
        "terminal_type_failed",
        "text_input_wrong_field",
        "input_aborted_focus_changed",
        "input_aborted_not_ready",
        "vscode_chat_type_failed",
        "vscode_chat_enter_failed",
        "copilot_app_send_blocked",
        "copilot_app_send_misdirected",
        "copilot_app_verify_failed",
        "copilot_app_focus_failed_foreground",
        "copilot_app_type_failed",
        "copilot_app_enter_failed",
        "focus_thrash_detected",
        "navigation_step_failed",
    }
    warn_like = {
        "copilot_app_not_foreground_when_read",
    }
    err_like = []
    warn_hits = []
    for e in errs_after:
        et = (e.get("event") or e.get("type") or "").lower()
        if et in error_like:
            err_like.append(e)
        elif et in warn_like:
            warn_hits.append(e)
    summary["steps"].append({"name": "scan_error_events_errorlike", "count": len(err_like), "samples": err_like[-3:]})
    if err_like:
        summary["errors"].append({"name": "error_events_detected", "count": len(err_like), "samples": err_like[-3:]})
    summary["steps"].append({"name": "scan_error_events_warnlike", "count": len(warn_hits), "samples": warn_hits[-3:]})
    if warn_hits:
        summary["warnings"].append({"name": "warning_events_detected", "count": len(warn_hits), "samples": warn_hits[-3:]})

    # 3c) Assert no palette bypass/repeat events occurred this run
    def count_events(evts: list[dict], names: set[str]) -> int:
        n = 0
        for e in evts:
            et = (e.get("type") or e.get("event") or "").lower()
            if et in names:
                n += 1
        return n
    bad_events = {"palette_command_bypassed", "palette_command_repeated"}
    bad_count = count_events(errs_after, bad_events)
    summary["steps"].append({"name": "assert_palette_events_clean", "count": bad_count, "pass": bad_count == 0})
    if bad_count > 0:
        summary["errors"].append({"name": "assert_palette_events_clean", "bad_count": bad_count})

    # 4) Short Recording (auto-mark assessed)
    out_vid = str(Path("logs/screens/workflow_out.mp4"))
    rec = run_cmd([py, "scripts/monitor_live.py", "--seconds", "3", "--fps", "10", "--out", out_vid, "--backend", "mss", "--mark-assessed"], root)
    out_abs = root / out_vid
    marker_abs = root / (out_vid + ".assessed")
    made_video = out_abs.exists()
    made_marker = marker_abs.exists()
    step_rec_pass = bool(rec.get("ok", False) and made_video and made_marker)
    summary["steps"].append({"name": "monitor_live", "result": rec, "pass": step_rec_pass})
    if step_rec_pass:
        summary["successes"].append({"name": "monitor_live", "out": out_vid, "marker": out_vid + ".assessed"})
    else:
        summary["errors"].append({"name": "monitor_live", "returncode": rec.get("returncode"), "stderr_tail": (rec.get("stderr") or "")[-800:]})
    summary["artifacts"].append(out_vid)
    summary["artifacts"].append(out_vid + ".assessed")

    # 5) Cleanup pass (enforces 5s-if-assessed for logs/screens)
    # wait a moment to exceed retention
    try:
        time.sleep(6)
    except KeyboardInterrupt:
        interrupted = True
        summary["pass"] = False
        summary["errors"].append({"name": "workflow_keyboardinterrupt", "note": "KeyboardInterrupt during retention sleep"})
        return _finalize(1)
    step_cleanup = run_cmd([py, "Scripts/cleanup_run.py"], root)
    deleted_video = not out_abs.exists()
    deleted_marker = not marker_abs.exists()
    pass_cleanup = bool(step_cleanup.get("ok", False) and deleted_video)
    summary["steps"].append({"name": "cleanup", "result": step_cleanup, "pass": pass_cleanup, "deleted_video": deleted_video, "deleted_marker": deleted_marker})
    if pass_cleanup:
        summary["successes"].append({"name": "cleanup", "deleted_video": deleted_video, "deleted_marker": deleted_marker})
    else:
        summary["errors"].append({"name": "cleanup", "returncode": step_cleanup.get("returncode"), "stderr_tail": (step_cleanup.get("stderr") or "")[-800:]})

    # Final foreground check: ensure no disallowed windows remain (close if found)
    final_log = str(Path("logs/tests/observe_react_final.jsonl"))
    step_final = run_cmd([py, "Scripts/observe_and_react.py", "--ticks", "6", "--interval-ms", "350", "--log", final_log], root)
    summary["steps"].append({"name": "final_foreground_check", "result": step_final, "pass": bool(step_final.get("ok", False))})
    summary["artifacts"].append(final_log)
    # Parse final log for close actions
    try:
        import json as _json
        p = root / final_log
        closes = 0
        close_failed = 0
        if p.exists():
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = _json.loads(line)
                    act = (obj.get("action") or "").lower()
                    if act == "close":
                        closes += 1
                    elif act == "close_failed":
                        close_failed += 1
                except Exception:
                    pass
        summary["steps"].append({"name": "final_close_summary", "closes": closes, "close_failed": close_failed, "pass": close_failed == 0})
        if close_failed > 0:
            summary["errors"].append({"name": "final_close_summary", "close_failed": close_failed})
    except Exception:
        pass

    # Context pack: generate a Copilot-ready summary of project, objectives, and this run
    step_ctx = run_cmd([py, "Scripts/prepare_context_pack.py"], root)
    summary["steps"].append({"name": "prepare_context_pack", "result": step_ctx, "pass": bool(step_ctx.get("ok", False))})
    if step_ctx.get("ok", False):
        summary["successes"].append({"name": "prepare_context_pack"})
        summary["artifacts"].append("Copilot_Attachments/ContextPack_Current.md")
    else:
        summary["errors"].append({"name": "prepare_context_pack", "returncode": step_ctx.get("returncode"), "stderr_tail": (step_ctx.get("stderr") or "")[-800:]})

    # Learn phase: persist lessons/solutions from this run
    step_learn = run_cmd([py, "Scripts/learn_from_run.py", "--since-ts", summary["started"]], root)
    summary["steps"].append({"name": "learn_from_run", "result": step_learn, "pass": bool(step_learn.get("ok", False))})
    if step_learn.get("ok", False):
        summary["successes"].append({"name": "learn_from_run"})
        summary["artifacts"].append("projects/Self-Improve/lessons.jsonl")
        summary["artifacts"].append("projects/Self-Improve/solutions.jsonl")
        if (root / "projects" / "Self-Improve" / "error_commands.json").exists():
            summary["artifacts"].append("projects/Self-Improve/error_commands.json")
    else:
        summary["errors"].append({"name": "learn_from_run", "returncode": step_learn.get("returncode"), "stderr_tail": (step_learn.get("stderr") or "")[-800:]})

    # Improve phase: apply module improvements based on policy (remove banned commands from source)
    step_improve = run_cmd([py, "Scripts/improve_modules_from_policy.py"], root)
    summary["steps"].append({"name": "improve_modules", "result": step_improve, "pass": bool(step_improve.get("ok", False))})
    if step_improve.get("ok", False):
        summary["successes"].append({"name": "improve_modules"})
    else:
        summary["errors"].append({"name": "improve_modules", "returncode": step_improve.get("returncode"), "stderr_tail": (step_improve.get("stderr") or "")[-800:]})

    # Verify phase: re-run navigation and assert no palette errors since improvement
    verify_start = time.time()
    step_nav_verify = run_cmd([py, "scripts/navigation_test.py"], root)
    summary["steps"].append({"name": "navigation_test_verify", "result": step_nav_verify, "pass": bool(step_nav_verify.get("ok", False))})
    errs_verify = []
    try:
        errs_verify = read_error_events_since(root, verify_start)
    except Exception:
        errs_verify = []
    bad_verify = 0
    for e in errs_verify:
        et = (e.get("type") or e.get("event") or "").lower()
        if et in {"palette_command_bypassed", "palette_command_repeated"}:
            bad_verify += 1
    summary["steps"].append({"name": "assert_palette_events_clean_verify", "count": bad_verify, "pass": bad_verify == 0})
    if bad_verify > 0:
        summary["errors"].append({"name": "assert_palette_events_clean_verify", "bad_count": bad_verify})

    # Final pass computation (after Learn/Improve/Verify)
    explicit_pass = [s for s in summary["steps"] if "pass" in s]
    steps_ok = all(bool(s.get("pass", False)) for s in explicit_pass) if explicit_pass else True
    no_errors = True
    for s in summary["steps"]:
        if s.get("name") == "scan_error_events_errorlike" and int(s.get("count") or 0) > 0:
            no_errors = False
            break
    for s in summary["steps"]:
        if s.get("name") == "assert_palette_events_clean" and not bool(s.get("pass", False)):
            no_errors = False
            break
    for s in summary["steps"]:
        if s.get("name") == "assert_palette_events_clean_verify" and not bool(s.get("pass", False)):
            no_errors = False
            break
    summary["pass"] = bool(steps_ok and no_errors)

    # exit non-zero on fail for CI/task visibility
    return _finalize(0 if summary["pass"] else 1)


if __name__ == "__main__":
    sys.exit(main())
