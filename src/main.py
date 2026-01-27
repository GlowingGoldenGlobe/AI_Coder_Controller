import json
import subprocess
import sys
import time
import hashlib
import argparse
import signal
import atexit
from pathlib import Path
from typing import Any, List
from rich.console import Console

from src.capture import ScreenCapture, SegmentedScreenCapture
from src.control import Controller, SafetyLimits
from src.policy import Policy, Action
from src.ui import Hotkeys, AppUI, interactive_banner
from src.vsbridge import VSBridge
from src.windows import WindowsManager
from src.ocr import CopilotOCR
from src.self_improve import write_metadata_file
from src.jsonlog import JsonActionLogger
from src.messaging import CopilotMessenger
from src.agent_terminal import TerminalAgent
from src.phi4_client import Phi4Client
from src.ocr_observer import OcrObserver
from src.cleanup import FileCleaner
from src.control_state import get_controls_state, set_controls_owner, update_control_window

console = Console()


def ensure_dirs(root: Path):
    for p in ["config", "logs", "recordings", "src", "projects", "projects/Self-Improve"]:
        d = root / p
        d.mkdir(parents=True, exist_ok=True)


def timestamp():
    return time.strftime("%Y-%m-%d %H:%M:%S")


class Logger:
    def __init__(self, logfile: Path):
        self.logfile = logfile
        self.logfile.parent.mkdir(parents=True, exist_ok=True)

    def __call__(self, msg: str):
        line = f"[{timestamp()}] {msg}\n"
        try:
            with open(self.logfile, "a", encoding="utf-8") as f:
                f.write(line)
        except Exception:
            pass
        console.log(msg)


def run(
    root: Path,
    fps: int = 20,
    headless: bool = False,
    headless_duration_s: int | None = None,
    *,
    headless_agent_mode: bool = False,
    objectives: list[str] | None = None,
):
    ensure_dirs(root)

    rules = json.loads((root / "config/policy_rules.json").read_text(encoding="utf-8"))

    def _validate_config(log_run: Logger, action_log: JsonActionLogger) -> None:
        """Log configuration issues early (best-effort; never raises)."""
        issues: list[dict] = []
        try:
            cfg_dir = root / "config"
            for rel in ["policy_rules.json", "objectives.md", "instructions.md", "ocr.json"]:
                p = cfg_dir / rel
                if not p.exists():
                    issues.append({"level": "warn", "kind": "missing_file", "path": str(p)})

            # OCR config
            try:
                ocr_cfg_path = root / "config" / "ocr.json"
                if ocr_cfg_path.exists():
                    ocr_cfg = json.loads(ocr_cfg_path.read_text(encoding="utf-8"))
                    if bool(ocr_cfg.get("enabled", True)):
                        tcmd = str(ocr_cfg.get("tesseract_cmd", "")).strip()
                        if tcmd and not Path(tcmd).exists():
                            issues.append({"level": "warn", "kind": "tesseract_missing", "tesseract_cmd": tcmd})
                        timeout_s = ocr_cfg.get("tesseract_timeout_s", 3)
                        try:
                            if float(timeout_s) <= 0:
                                issues.append({"level": "warn", "kind": "tesseract_timeout_disabled", "tesseract_timeout_s": timeout_s})
                        except Exception:
                            issues.append({"level": "warn", "kind": "tesseract_timeout_invalid", "tesseract_timeout_s": timeout_s})
            except Exception as e:
                issues.append({"level": "warn", "kind": "ocr_cfg_parse_failed", "error": str(e)})

            # Copilot routing
            try:
                cp = (rules.get("copilot") or {})
                if bool(cp.get("prefer_app", False)):
                    issues.append({"level": "info", "kind": "copilot_prefer_app", "note": "Will attempt Win+C and OCR copilot_app ROI"})
            except Exception:
                pass
        except Exception:
            issues = []

        try:
            for it in issues:
                action_log.log("config", **it)
                log_run(f"Config {it.get('level','info')}: {it.get('kind')} {it}")
        except Exception:
            pass
    hotkeys = Hotkeys(root / "config/policy_rules.json")
    limits = SafetyLimits(
        max_clicks_per_min=rules.get("bounds", {}).get("max_clicks_per_min", 60),
        max_keys_per_min=rules.get("bounds", {}).get("max_keys_per_min", 120),
    )
    mi = rules.get("mouse_intervals", {})
    ctrl = Controller(
        mouse_speed=rules.get("bounds", {}).get("mouse_speed", 0.3),
        limits=limits,
        mouse_control_seconds=int(mi.get("control_seconds", 10)),
        mouse_release_seconds=int(mi.get("release_seconds", 5)),
        state_file=root / "config" / "controls_state.json",
    )
    log_run = Logger(root / "logs/run.log")
    action_log = JsonActionLogger(root / "logs/actions/actions.jsonl")
    log_improve = Logger(root / "logs/self_improve.log")

    # Graceful shutdown state
    _shutdown_state = {"requested": False, "cap": None, "ctrl": None}
    
    def _save_shutdown_state():
        """Save critical state on shutdown."""
        try:
            # Log shutdown event
            action_log.log("shutdown", reason="signal_or_exit", graceful=True)
            # Stop capture if running
            if _shutdown_state.get("cap"):
                try:
                    _shutdown_state["cap"].stop()
                except Exception:
                    pass
            # Save pause state for next startup
            if _shutdown_state.get("ctrl"):
                try:
                    ctrl_state = {
                        "paused": bool(_shutdown_state["ctrl"]._controls_paused),
                        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    }
                    state_file = root / "config" / "controls_state.json"
                    state_file.write_text(json.dumps(ctrl_state), encoding="utf-8")
                except Exception:
                    pass
            log_run("Graceful shutdown complete")
        except Exception:
            pass
    
    def _signal_handler(signum, frame):
        """Handle SIGTERM/SIGINT for graceful shutdown."""
        if _shutdown_state["requested"]:
            # Second signal - force exit
            sys.exit(1)
        _shutdown_state["requested"] = True
        console.log(f"Interrupted by user (Ctrl+C). Exiting cleanly.")
        _save_shutdown_state()
        sys.exit(0)
    
    # Register signal handlers (Windows has limited signal support)
    try:
        signal.signal(signal.SIGINT, _signal_handler)
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, _signal_handler)
    except Exception:
        pass
    
    # Register atexit handler as fallback
    atexit.register(_save_shutdown_state)

    _validate_config(log_run, action_log)

    rec_cfg = (rules.get("recording") or {})
    if bool(rec_cfg.get("segmented", False)):
        seg_dir = root / str(rec_cfg.get("segments_dir", "recordings/segments"))
        seg_len = int(rec_cfg.get("segment_seconds", 60))
        cap = SegmentedScreenCapture(seg_dir, fps=fps, monitor_index=1, segment_seconds=seg_len)
    else:
        cap = ScreenCapture(root / "recordings/recording.mp4", fps=fps)
    
    # Update shutdown state with references to resources
    _shutdown_state["cap"] = cap
    _shutdown_state["ctrl"] = ctrl
    
    policy = Policy(rules)
    winman = WindowsManager()
    vs_cfg = rules.get("vsbridge", {})
    vs = VSBridge(
        ctrl,
        log_run,
        winman,
        delay_ms=int(vs_cfg.get("delay_ms", 300)),
        dry_run=bool(vs_cfg.get("dry_run", True)),
    )
    # Optional typing interval (ms)
    try:
        kb_cfg = rules.get("keyboard", {}) or {}
        ti_ms = float(kb_cfg.get("type_interval_ms", kb_cfg.get("typing_interval_ms", 10)))
        ctrl.type_interval = max(0.0, ti_ms / 1000.0)
    except Exception:
        pass
    # Terminal Agent (single gateway for all terminal executions)
    term_agent = TerminalAgent(root, vs, action_log, python_exe=(root / "Scripts" / "python.exe"))

    # Optional PHI-4 client
    phi_cfg = rules.get("phi4", {}) or {}
    phi_client = None
    try:
        if bool(phi_cfg.get("enabled", False)) and str(phi_cfg.get("endpoint", "")).strip():
            phi_client = Phi4Client(
                endpoint=str(phi_cfg.get("endpoint", "")),
                api_key=str(phi_cfg.get("api_key", "")),
                model=str(phi_cfg.get("model", "phi-4-mini")),
                timeout_ms=int(phi_cfg.get("timeout_ms", 15000)),
                health_path=str(phi_cfg.get("health_path", "/health")),
            )
            log_run("PHI-4 client initialized")
    except Exception as e:
        log_run(f"PHI-4 client init failed: {e}")

    # OCR / image-analysis configuration
    ocr_cfg_path = root / "config/ocr.json"
    ocr_cfg = {}
    if ocr_cfg_path.exists():
        try:
            ocr_cfg = json.loads(ocr_cfg_path.read_text(encoding="utf-8"))
        except Exception:
            ocr_cfg = {}
    ocr_debug = root / "logs/ocr"
    ocr = CopilotOCR(ocr_cfg, log=log_run, debug_dir=ocr_debug)
    # Optional continuous OCR observer ("movie")
    ocr_obs = None
    try:
        ocr_rules = rules.get("ocr", {}) or {}
        if bool(ocr_rules.get("observe_stream", False)):
            ocr_obs = OcrObserver(
                ocr,
                action_log,
                stream_dir=ocr_debug,
                interval_ms=int(ocr_rules.get("stream_interval_ms", 800)),
            )
            log_run("OCR observer enabled")
    except Exception:
        ocr_obs = None

    # Cleanup scheduler (deletes debug pics/movies after a retention window)
    cleanup_cfg = (rules.get("cleanup") or {})
    cleaner = None
    last_cleanup_t = {"t": 0.0}
    try:
        if bool(cleanup_cfg.get("enabled", True)):
            cleaner = FileCleaner(
                base=root,
                dirs=cleanup_cfg.get("dirs", ["logs/ocr"]),
                patterns=cleanup_cfg.get("patterns", ["*.png", "*.jpg"]),
                retain_seconds=int(cleanup_cfg.get("retain_seconds", 30)),
                logger=action_log,
                rules=cleanup_cfg.get("rules"),
            )
            log_run("Cleanup scheduler initialized")
    except Exception:
        cleaner = None

    # Optional VS Code multi-window chat orchestrator (keepalive across windows)
    orchestrator_cfg = (rules.get("orchestrator") or {})
    keepalive = None
    last_keepalive_t = {"t": 0.0}
    try:
        from vscode_automation import MultiWindowChatKeepalive  # type: ignore

        keepalive = MultiWindowChatKeepalive(ctrl=ctrl, ocr=ocr, winman=winman)
        log_run("VS Code multi-window orchestrator initialized")
    except Exception as e:
        # Safe to run without orchestrator; log once.
        keepalive = None
        try:
            action_log.log("orchestrator", op="init_failed", error=str(e))
        except Exception:
            pass

    state = {"running": False, "paused": False, "stop": False, "objectives": [], "headless": False, "agent_mode": False}
    last_ocr_hash = {"value": None}
    metadata_written_once: dict[str, bool] = {"ok": False}
    metadata_sent_once: dict[str, bool] = {"ok": False}
    executed_recent: dict[str, float] = {}
    exec_cooldown_s = float(rules.get("runtime", {}).get("exec_cooldown_s", 3))
    objective_state: dict[str, dict] = {}
    max_attempts = int(rules.get("runtime", {}).get("max_objective_attempts", 3))
    # Target window selection state
    target_map: dict[str, dict] = {}
    selected_target_name: dict[str, str | None] = {"name": None}

    # Image-analysis / measurement configuration (templates + thresholds)
    measurement_cfg = (rules.get("measurement") or {}) if isinstance(rules, dict) else {}
    meas_threshold = float(measurement_cfg.get("threshold", 0.85))
    meas_retry_attempts = int(measurement_cfg.get("retry_attempts", 2))
    meas_backoff_ms = int(measurement_cfg.get("backoff_ms", 400))

    templates_cfg: dict[str, Any] = {}
    chat_templates: list[Path] = []
    try:
        tpl_path = root / "config" / "templates.json"
        if tpl_path.exists():
            templates_cfg = json.loads(tpl_path.read_text(encoding="utf-8")) or {}
    except Exception:
        templates_cfg = {}
    try:
        rels = (templates_cfg.get("chat_input", {}) or {}).get("templates", []) or []
        for rel in rels:
            try:
                p = (root / str(rel)).resolve()
                if p.exists():
                    chat_templates.append(p)
            except Exception:
                continue
    except Exception:
        chat_templates = []

    def _best_template_match(image_path: Path, threshold: float) -> tuple[Path | None, float]:
        """Return (best_template, score) using OpenCV template matching.

        If OpenCV is unavailable or matching fails, returns (None, 0.0).
        """
        try:
            import cv2  # type: ignore
        except Exception:
            return None, 0.0
        if (not image_path) or (not image_path.exists()) or (not chat_templates):
            return None, 0.0
        try:
            img = cv2.imread(str(image_path), cv2.IMREAD_GRAYSCALE)
            if img is None:
                return None, 0.0
            best_tpl: Path | None = None
            best_score: float = 0.0
            for tpl in chat_templates:
                try:
                    tpl_img = cv2.imread(str(tpl), cv2.IMREAD_GRAYSCALE)
                    if tpl_img is None:
                        continue
                    res = cv2.matchTemplate(img, tpl_img, cv2.TM_CCOEFF_NORMED)
                    _min_val, max_val, _min_loc, _max_loc = cv2.minMaxLoc(res)
                    score = float(max_val or 0.0)
                    if score > best_score:
                        best_score = score
                        best_tpl = tpl
                except Exception:
                    continue
            return best_tpl, best_score
        except Exception:
            return None, 0.0

    def _capture_and_measure(phase: str, objective_key: str, extra: dict[str, Any] | None = None) -> None:
        """Capture chat-region image and run template-based readiness measurement.

        Logs an `image_analysis` event to actions.jsonl. This does not gate behaviour
        yet; it provides evidence that the UI matches expected templates.
        """
        if not chat_templates:
            return
        attempts = max(1, int(meas_retry_attempts))
        image_path: Path | None = None
        best_tpl: Path | None = None
        best_score: float = 0.0
        used_attempts = 0
        for i in range(attempts):
            used_attempts = i + 1
            try:
                res = ocr.capture_chat_text(save_dir=ocr_debug)
            except Exception as e:
                try:
                    action_log.log(
                        "image_analysis",
                        phase=phase,
                        objective=objective_key[:200],
                        ok=False,
                        error=f"capture_failed:{e}",
                    )
                except Exception:
                    pass
                return
            if not isinstance(res, dict):
                break
            img_str = str(res.get("image_path") or "")
            image_path = Path(img_str) if img_str else None
            if not image_path or (not image_path.exists()):
                if i + 1 < attempts:
                    time.sleep(max(0.0, float(meas_backoff_ms) / 1000.0))
                continue
            tpl, score = _best_template_match(image_path, threshold=meas_threshold)
            if score > best_score:
                best_score = score
                best_tpl = tpl
            # Early-exit if we have a confident match
            if best_score >= meas_threshold:
                break
            if i + 1 < attempts:
                time.sleep(max(0.0, float(meas_backoff_ms) / 1000.0))
        try:
            ready = bool(best_tpl is not None and best_score >= meas_threshold)
            payload: dict[str, Any] = {
                "phase": phase,
                "objective": objective_key[:200],
                "ok": bool(image_path and image_path.exists()),
                "ready": ready,
                "score": float(best_score),
                "threshold": float(meas_threshold),
                "attempts": int(used_attempts),
            }
            if image_path and image_path.exists():
                try:
                    payload["image"] = str(image_path.relative_to(root))
                except Exception:
                    payload["image"] = str(image_path)
            if best_tpl is not None:
                try:
                    payload["template"] = str(best_tpl.relative_to(root))
                except Exception:
                    payload["template"] = str(best_tpl)
            if extra:
                payload.update(extra)
            action_log.log("image_analysis", **payload)
        except Exception:
            pass

    def _safe_workspace_path(path_str: str) -> Path | None:
        """Resolve a user-provided path to a workspace-local path, or return None."""
        if not path_str:
            return None
        try:
            # Treat as workspace-relative unless absolute
            candidate = Path(path_str)
            if not candidate.is_absolute():
                candidate = (root / candidate)
            resolved = candidate.resolve()
            root_resolved = root.resolve()
            if str(resolved).lower().startswith(str(root_resolved).lower() + "\\") or str(resolved).lower() == str(root_resolved).lower():
                return resolved
        except Exception:
            return None
        return None

    def _append_summary_to_file(target_path: str, kind: str, text: str) -> bool:
        if not text:
            return False
        p = _safe_workspace_path(target_path)
        if p is None:
            action_log.log("copilot", op="insert_summary_into_file", ok=False, error="path_not_in_workspace", path=target_path)
            return False
        if p.suffix.lower() not in {".md", ".txt"}:
            action_log.log("copilot", op="insert_summary_into_file", ok=False, error="unsupported_extension", path=str(p))
            return False
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "a", encoding="utf-8") as f:
                f.write(f"\n\n## Copilot {kind} ({timestamp()})\n\n")
                f.write(text + "\n")
            return True
        except Exception as e:
            action_log.log("copilot", op="insert_summary_into_file", ok=False, error=str(e), path=str(p))
            return False

    def _append_copilot_text(kind: str, text: str) -> bool:
        """Append Copilot OCR text to improvements.md if new (de-dup by hash)."""
        if not text:
            return False
        try:
            h = hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()
        except Exception:
            h = None
        if last_ocr_hash["value"] and h and last_ocr_hash["value"] == h:
            log_improve("Skipped append (duplicate OCR content)")
            return False
        imp = root / "projects" / "Self-Improve" / "improvements.md"
        imp.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(imp, "a", encoding="utf-8") as f:
                f.write(f"\n\n## Copilot {kind} ({timestamp()})\n\n")
                f.write(text + "\n")
            if h:
                last_ocr_hash["value"] = h
            log_improve(f"Appended Copilot {kind} (OCR)")
            return True
        except Exception as e:
            log_improve(f"Failed to append OCR text: {e}")
            return False

    # UI state persistence
    ui_state_path = root / "config" / "ui_state.json"

    def load_ui_state():
        try:
            if ui_state_path.exists():
                return json.loads(ui_state_path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        return {}

    def save_ui_state(data: dict):
        try:
            ui_state_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception:
            pass
    ui_state = load_ui_state()

    def on_run():
        state["running"] = True
        state["paused"] = False
        log_run("Run requested")
        action_log.log("run", status="requested")
        # Mark controller/agent as current owner of automation controls.
        try:
            set_controls_owner(root, "agent")
        except Exception:
            pass
        if rules.get("auto_record_on_run", False):
            try:
                if cap.start():
                    hotkeys.state["recording"] = True
                    log_run("Recording started")
                    action_log.log("recording", action="start", ok=True)
            except Exception:
                pass

    def on_pause():
        state["paused"] = True
        log_run("Paused")
        action_log.log("run", status="paused")

    def on_resume():
        state["paused"] = False
        log_run("Resumed")
        action_log.log("run", status="resumed")

    pending_copilot: list[dict] = []

    def on_stop():
        state["stop"] = True
        log_run("Stop requested")
        try:
            cap.stop()
        except Exception:
            pass
        # Send any queued Copilot messages first to avoid losing intent
        try:
            if pending_copilot:
                action_log.log("copilot", op="commit_pending_on_stop", count=len(pending_copilot))
                for item in list(pending_copilot):
                    kind = item.get("kind")
                    q = item.get("q", "")
                    if kind == "app":
                        vs.ask_copilot_app(q)
                    else:
                        messenger = CopilotMessenger(root, vs, ctrl, ocr, log_run, log_improve, rules, ui_state, phi4_client=phi_client)
                        messenger.send_or_plan(q)
                pending_copilot.clear()
        except Exception:
            pass
        # Commit any pending terminal sends (press Enter once stop occurs)
        try:
            if term_agent and term_agent.has_pending():
                action_log.log("agent_terminal", action="commit_pending_on_stop")
                term_agent.commit_pending()
        except Exception:
            pass
        # Optional: auto-start external Copilot commit loop after stop
        try:
            cp_cfg = (rules.get("copilot") or {})
            if bool(cp_cfg.get("auto_commit_after_stop", False)):
                start_after = int(cp_cfg.get("auto_commit_start_after_s", 7))
                repeat_s = int(cp_cfg.get("auto_commit_repeat_s", 10))
                repeat_n = int(cp_cfg.get("auto_commit_repeat_count", 0))
                msg = str(cp_cfg.get("auto_commit_message", "Auto message from powershell — see projects/Self-Improve/next_steps.md"))
                title = str(cp_cfg.get("auto_commit_title", "Copilot"))
                log_path = str(cp_cfg.get("auto_commit_log", "logs/actions/copilot_commit.log"))
                cmd = [
                    "powershell",
                    "-NoProfile",
                    "-ExecutionPolicy","Bypass",
                    "-File", str(root / "scripts" / "copilot_commit_start.ps1"),
                    "-Mode","app",
                    "-StartAfterSeconds", str(start_after),
                    "-RepeatSeconds", str(repeat_s),
                    "-RepeatCount", str(repeat_n),
                    "-Message", msg,
                    "-Title", title,
                    "-LogPath", str(log_path),
                ]
                subprocess.Popen(cmd, cwd=str(root))
                action_log.log("copilot", op="auto_commit_after_stop", start_after_s=start_after, repeat_s=repeat_s, repeat_count=repeat_n)
        except Exception as e:
            action_log.log("copilot", op="auto_commit_after_stop", error=str(e))
        action_log.log("run", status="stopped")

        # Release ownership of automation controls.
        try:
            set_controls_owner(root, None)
        except Exception:
            pass

    def on_user_msg(content: str):
        log_run(f"User message: {content[:80]}")
        action_log.log("user_message", preview=content[:200])

    def on_upload_files(paths: List[Path]):
        state["objectives"].extend(paths)
        log_run(f"Files uploaded: {', '.join(str(p) for p in paths)}")
        # persist
        try:
            data = load_ui_state()
        except Exception:
            data = {}
        data["files"] = [str(p) for p in state["objectives"]]
        save_ui_state(data)
        action_log.log("files_uploaded", files=[str(p) for p in paths])

    def on_select_project(proj_dir: Path, files: List[Path]):
        state["objectives"] = files
        log_run(f"Project selected: {proj_dir}")
        try:
            data = load_ui_state()
        except Exception:
            data = {}
        data["project"] = str(proj_dir)
        data["files"] = [str(p) for p in files]
        save_ui_state(data)
        action_log.log("project_selected", project=str(proj_dir), files=[str(p) for p in files])

    def on_focus_vscode():
        action_log.log("focus", target="vscode")
        vs.focus_vscode_window()

    def on_focus_terminal():
        action_log.log("focus", target="terminal")
        vs.focus_terminal()

    def on_toggle_controls():
        paused = ctrl.toggle_controls_paused()
        action_log.log("controls_toggle", paused=paused)
        return paused

    def on_send_metadata():
        try:
            outp = write_metadata_file(root)
            messenger = CopilotMessenger(root, vs, ctrl, ocr, log_run, log_improve, rules, ui_state, phi4_client=phi_client)
            header = (
                "Analyze this architecture and suggest improvements for modularity, safety, and automation.\n"
                "Summarize risks and concrete steps, then propose a migration plan.\n\n"
            )
            body = outp.read_text(encoding="utf-8")[:4000]
            messenger.send_or_plan(header + body)
            log_improve(f"Sent (or planned) metadata to Copilot from {outp}")
            action_log.log("copilot", op="send_metadata", ok=True, path=str(outp))
        except Exception as e:
            log_improve(f"Send metadata failed: {e}")
            action_log.log("copilot", op="send_metadata", ok=False, error=str(e))

    def _execute_objectives_once(max_tasks: int = 10) -> tuple[int, bool]:
        """Run up to max_tasks objective lines once. Returns (executed_count, performed_non_copilot)."""
        tasks = policy.parse_objectives([Path(p) if isinstance(p, str) else p for p in state.get("objectives", [])])
        executed_this_tick = 0
        performed_non_copilot = False
        last_non_copilot_key: str | None = None
        for t in tasks[: max(0, int(max_tasks))]:
            # Skip if recently executed (avoid re-running same objective each tick)
            try:
                line_no = int(t.get("line") or 0)
            except Exception:
                line_no = 0
            key_src = f"{t.get('file','')}|{line_no}|{t.get('text','')[:200]}"
            st = objective_state.get(key_src) or {"attempts": 0, "done": False, "last_error": None}
            if bool(st.get("done")):
                continue
            now_t = time.time()
            last_t = executed_recent.get(key_src, 0)
            if (now_t - last_t) < max(0.5, exec_cooldown_s):
                continue

            act = policy.decide(t["text"])  # type: ignore
            executed_recent[key_src] = now_t
            executed_this_tick += 1
            ok = True

            if act.kind == "vscode":
                performed_non_copilot = True
                last_non_copilot_key = key_src
                op = act.params.get("op")
                if op == "record_toggle_on":
                    if not hotkeys.state["recording"]:
                        ok = bool(cap.start())
                        if ok:
                            hotkeys.state["recording"] = True
                            log_run("Recording toggled on")
                            action_log.log("recording", action="start", ok=True)
                elif op == "open_vscode":
                    action_log.log("vscode", op="open_vscode")
                    try:
                        vs.open_vscode()
                        _capture_and_measure("post_nav_open_vscode", key_src, {"op": op})
                    except Exception as e:
                        ok = False
                        st["last_error"] = str(e)
                elif op == "open_folder":
                    action_log.log("vscode", op="open_folder", path=act.params.get("path", ""))
                    try:
                        vs.open_folder(act.params.get("path", ""))
                        _capture_and_measure("post_nav_open_folder", key_src, {"op": op, "path": act.params.get("path", "")})
                    except Exception as e:
                        ok = False
                        st["last_error"] = str(e)
                elif op == "open_file":
                    action_log.log("vscode", op="open_file", path=act.params.get("path", ""))
                    try:
                        vs.open_file_quick(act.params.get("path", ""))
                        _capture_and_measure("post_nav_open_file", key_src, {"op": op, "path": act.params.get("path", "")})
                    except Exception as e:
                        ok = False
                        st["last_error"] = str(e)
                elif op == "focus_vscode":
                    action_log.log("focus", target="vscode")
                    try:
                        ok = bool(vs.focus_vscode_window())
                        if ok:
                            _capture_and_measure("post_nav_focus_vscode", key_src, {"op": op})
                    except Exception as e:
                        ok = False
                        st["last_error"] = str(e)
                elif op == "focus_terminal":
                    action_log.log("focus", target="terminal")
                    try:
                        vs.focus_terminal()
                        _capture_and_measure("post_nav_focus_terminal", key_src, {"op": op})
                    except Exception as e:
                        ok = False
                        st["last_error"] = str(e)
                elif op == "stop":
                    action_log.log("run", status="stopping")
                    on_stop()

            elif act.kind == "terminal":
                performed_non_copilot = True
                last_non_copilot_key = key_src
                op = act.params.get("op")
                if op == "run":
                    cmd = act.params.get("cmd", "")
                    action_log.log("terminal", op="run", cmd_preview=cmd[:160])
                    ok = bool(term_agent.run_command(cmd))
                    action_log.log("terminal", op="run", ok=ok)
                elif op == "queue_after_stop":
                    cmd = act.params.get("cmd", "")
                    action_log.log("terminal", op="queue_after_stop", cmd_preview=cmd[:160])
                    ok = bool(term_agent.queue_post_stop_send(cmd))
                    action_log.log("terminal", op="queue_after_stop", ok=ok)

            elif act.kind == "agent":
                performed_non_copilot = True
                last_non_copilot_key = key_src
                op = act.params.get("op")
                if op == "launch_ui":
                    action_log.log("agent", op="launch_ui")
                    ok = bool(term_agent.launch_ui())
                    action_log.log("agent", op="launch_ui", ok=ok)
                elif op == "terminal":
                    cmd = act.params.get("cmd", "")
                    action_log.log("agent", op="terminal", cmd_preview=cmd[:160])
                    ok = bool(term_agent.run_command(cmd))
                    action_log.log("agent", op="terminal", ok=ok)
                elif op == "run_module":
                    mod = act.params.get("module", "")
                    action_log.log("agent", op="run_module", module=mod)
                    ok = bool(term_agent.run_python_module(mod))
                    action_log.log("agent", op="run_module", module=mod, ok=ok)

            elif act.kind == "copilot":
                op = act.params.get("op")
                if op == "ask":
                    question = act.params.get("question", "")
                    action_log.log("copilot", op="ask", preview=question[:160])
                    prefer_app = bool((rules.get("copilot") or {}).get("prefer_app", False))
                    cp_cfg = (rules.get("copilot") or {})
                    defer_busy = bool(cp_cfg.get("defer_when_busy", True))
                    quiet_idle_ms = int(cp_cfg.get("quiet_idle_ms", 600))
                    must_defer = defer_busy and (performed_non_copilot or (ctrl.idle_seconds() < max(0, quiet_idle_ms) / 1000.0))
                    try:
                        if state.get("agent_mode"):
                            must_defer = False
                    except Exception:
                        pass
                    # Agent override: if we just performed navigation for THIS objective,
                    # allow immediate commit (don't defer) when configured.
                    try:
                        agent_cfg = (rules.get("agent") or {})
                        commit_after_nav = bool(agent_cfg.get("commit_after_nav", True))
                        st_nav_steps = int(st.get("nav_steps", 0) or 0)
                        if commit_after_nav and st_nav_steps > 0 and last_non_copilot_key == key_src:
                            must_defer = False
                            action_log.log("agent", op="commit_after_nav_override", objective=key_src[:200])
                    except Exception:
                        pass
                    if must_defer:
                        pending_copilot.append({"kind": ("app" if prefer_app else "vscode"), "q": question})
                        action_log.log("copilot", op="deferred", reason="busy_or_not_idle")
                        ok = True
                    else:
                        messenger = CopilotMessenger(root, vs, ctrl, ocr, log_run, log_improve, rules, ui_state, phi4_client=phi_client)
                        res = messenger.send_or_plan(question, force_target=("app" if prefer_app else "vscode"))
                        ok = bool(res.get("sent", False))
                elif op == "focus_app":
                    action_log.log("copilot", op="focus_app")
                    try:
                        ok = bool(vs.focus_copilot_app())
                    except Exception:
                        ok = False
                elif op == "ask_app":
                    question = act.params.get("question", "")
                    action_log.log("copilot", op="ask_app", preview=question[:160])
                    cp_cfg = (rules.get("copilot") or {})
                    defer_busy = bool(cp_cfg.get("defer_when_busy", True))
                    quiet_idle_ms = int(cp_cfg.get("quiet_idle_ms", 600))
                    must_defer = defer_busy and (performed_non_copilot or (ctrl.idle_seconds() < max(0, quiet_idle_ms) / 1000.0))
                    try:
                        if state.get("agent_mode"):
                            must_defer = False
                    except Exception:
                        pass
                    if must_defer:
                        pending_copilot.append({"kind": "app", "q": question})
                        action_log.log("copilot", op="deferred", reason="busy_or_not_idle")
                        ok = True
                    else:
                        messenger = CopilotMessenger(root, vs, ctrl, ocr, log_run, log_improve, rules, ui_state, phi4_client=phi_client)
                        res = messenger.send_or_plan(question, force_target="app")
                        ok = bool(res.get("sent", False))
                elif op == "ask_after_stop":
                    question = act.params.get("question", "")
                    action_log.log("copilot", op="ask_after_stop", preview=question[:160])
                    pending_copilot.append({"kind": "vscode", "q": question})
                    ok = True
                elif op == "ask_app_after_stop":
                    question = act.params.get("question", "")
                    action_log.log("copilot", op="ask_app_after_stop", preview=question[:160])
                    pending_copilot.append({"kind": "app", "q": question})
                    ok = True
                elif op == "insert_summary":
                    action_log.log("copilot", op="insert_summary", step="ocr_capture")
                    text = vs.read_copilot_chat_text(ocr, save_dir=ocr_debug)
                    appended = _append_copilot_text("Summary", text)
                    action_log.log("copilot", op="insert_summary", step="append", appended=appended, chars=len(text or ""))
                    ok = bool(appended)
                elif op == "insert_summary_app":
                    action_log.log("copilot", op="insert_summary_app", step="ocr_capture")
                    text = vs.read_copilot_app_text(ocr, save_dir=ocr_debug)
                    appended = _append_copilot_text("App Summary", text)
                    action_log.log("copilot", op="insert_summary_app", step="append", appended=appended, chars=len(text or ""))
                    ok = bool(appended)
                elif op == "insert_summary_into_file":
                    target = str(act.params.get("path", "")).strip()
                    action_log.log("copilot", op="insert_summary_into_file", step="ocr_capture", path=target)
                    text = vs.read_copilot_chat_text(ocr, save_dir=ocr_debug)
                    ok = _append_summary_to_file(target, "Summary", text)
                    action_log.log("copilot", op="insert_summary_into_file", step="append", ok=ok, chars=len(text or ""), path=target)
                elif op == "insert_summary_app_into_file":
                    target = str(act.params.get("path", "")).strip()
                    action_log.log("copilot", op="insert_summary_app_into_file", step="ocr_capture", path=target)
                    text = vs.read_copilot_app_text(ocr, save_dir=ocr_debug)
                    ok = _append_summary_to_file(target, "App Summary", text)
                    action_log.log("copilot", op="insert_summary_app_into_file", step="append", ok=ok, chars=len(text or ""), path=target)
                elif op == "scroll_chat":
                    direction = act.params.get("direction", "down")
                    steps = int(act.params.get("steps", 3))
                    action_log.log("copilot", op="scroll_chat", direction=direction, steps=steps)
                    ok = bool(vs.scroll_chat(direction=direction, steps=steps))
                    action_log.log("copilot", op="scroll_chat", direction=direction, steps=steps, ok=ok)
                    if ok:
                        _capture_and_measure("post_scroll_chat", key_src, {"direction": direction, "steps": steps})

            # Self-Improve trigger: generate metadata file
            txt = str(t.get("text", "")).lower()
            if "create a txt file listing modules" in txt or "generate a txt file listing modules" in txt:
                if not metadata_written_once.get("ok", False):
                    outp = write_metadata_file(root)
                    metadata_written_once["ok"] = True
                    log_improve(f"Generated metadata at {outp}")
                    action_log.log("self_improve", op="write_metadata", path=str(outp))
                    try:
                        vs.compose_message_vscode_chat(outp.read_text(encoding="utf-8")[:4000])
                    except Exception:
                        pass

            # Self-Improve trigger: send metadata file contents to Copilot
            if ("upload this txt file" in txt or "upload the txt file" in txt) and not metadata_sent_once.get("ok", False):
                try:
                    meta_path = root / "projects" / "Self-Improve" / "metadata.txt"
                    if meta_path.exists():
                        payload = meta_path.read_text(encoding="utf-8")
                        messenger = CopilotMessenger(root, vs, ctrl, ocr, log_run, log_improve, rules, ui_state, phi4_client=phi_client)
                        messenger.send_or_plan(
                            "Analyze this metadata summary and propose concrete improvements (safety, robustness, architecture).\n\n" + payload[:4000]
                        )
                        metadata_sent_once["ok"] = True
                        action_log.log("self_improve", op="send_metadata_to_copilot", ok=True, path=str(meta_path))
                except Exception as e:
                    action_log.log("self_improve", op="send_metadata_to_copilot", ok=False, error=str(e))

            # update per-objective navigation counters / last action
            try:
                last_kind = act.kind
                st["last_action_kind"] = last_kind
                st["last_action_op"] = act.params.get("op")
                nav_ops = {"open_vscode", "open_folder", "open_file", "focus_vscode", "focus_terminal", "scroll_chat"}
                if last_kind == "vscode" and str(st.get("last_action_op", "")) in nav_ops:
                    st["nav_steps"] = int(st.get("nav_steps", 0)) + 1
                else:
                    # reset nav counter when a non-navigation or a commit action occurs
                    if last_kind in {"copilot", "terminal", "agent"}:
                        st["nav_steps"] = 0
            except Exception:
                pass

            # Objective completion tracking
            if ok:
                st["done"] = True
            else:
                st["attempts"] = int(st.get("attempts", 0)) + 1
                if int(st["attempts"]) >= max(1, max_attempts):
                    st["done"] = True
                    action_log.log(
                        "objective",
                        op="give_up",
                        key=key_src[:200],
                        attempts=int(st["attempts"]),
                        error=st.get("last_error"),
                    )
            objective_state[key_src] = st

        return executed_this_tick, performed_non_copilot

    # If headless, skip UI setup and run a minimal loop
    if headless or bool(rules.get("headless_start", False) or rules.get("runtime", {}).get("headless_start", False)):
        log_run("Starting in headless mode")
        # Optionally run Agent Mode without UI
        try:
            if bool(headless_agent_mode) or bool((rules.get("runtime", {}) or {}).get("agent_mode_headless", False)):
                state["agent_mode"] = True
        except Exception:
            pass

        # Seed objectives (CLI -> ui_state -> default)
        try:
            seeded: list[Path] = []
            if objectives:
                for p in objectives:
                    if not p:
                        continue
                    seeded.append(Path(p) if Path(p).is_absolute() else (root / p))
            elif isinstance(ui_state.get("files"), list) and ui_state.get("files"):
                for p in ui_state.get("files"):
                    try:
                        seeded.append(Path(p))
                    except Exception:
                        pass
            else:
                default_obj = root / "config" / "objectives.md"
                if default_obj.exists():
                    seeded.append(default_obj)
            if seeded:
                state["objectives"] = seeded
        except Exception:
            pass

        # Start run loop if in Agent Mode
        try:
            if state.get("agent_mode"):
                log_run("Headless Agent Mode enabled")
                action_log.log("agent_mode", action="headless_start")
                on_run()
        except Exception:
            pass

        # Auto-start recording if configured
        try:
            if (not state.get("agent_mode")) and rules.get("auto_record_on_run", False):
                if cap.start():
                    hotkeys.state["recording"] = True
                    log_run("Recording started (headless)")
                    action_log.log("recording", action="start", ok=True)
        except Exception:
            pass

        try:
            start = time.time()
            while True:
                cap.grab_frame()
                executed_this_tick = 0
                performed_non_copilot = False
                if state.get("agent_mode") and state.get("running") and (not state.get("paused")):
                    try:
                        executed_this_tick, performed_non_copilot = _execute_objectives_once(max_tasks=10)
                    except Exception:
                        executed_this_tick, performed_non_copilot = 0, False

                # Update shared control-window info (headless)
                try:
                    in_ctrl, elapsed, total = ctrl.mouse_window_state()
                    remaining = max(0.0, float(total) - float(elapsed))
                    update_control_window(root, bool(in_ctrl), remaining)
                except Exception:
                    pass

                # OCR observer polling
                try:
                    if ocr_obs is not None:
                        ocr_obs.poll()
                except Exception:
                    pass

                # VS Code multi-window orchestrator tick (headless)
                try:
                    if keepalive is not None:
                        interval = float(orchestrator_cfg.get("interval_s", 6.0))
                        if interval > 0:
                            now_t = time.time()
                            if now_t - last_keepalive_t["t"] >= interval:
                                summary = keepalive.cycle_once()
                                last_keepalive_t["t"] = now_t
                                try:
                                    action_log.log(
                                        "orchestrator",
                                        op="multi_window_keepalive",
                                        mode="headless",
                                        windows=int(summary.get("windows_scanned", 0)),
                                        actions=int(summary.get("actions_taken", 0)),
                                    )
                                except Exception:
                                    pass
                except Exception:
                    pass

                # Quiet-send deferred messages when idle
                try:
                    cp_cfg = (rules.get("copilot") or {})
                    quiet_idle_ms = int(cp_cfg.get("quiet_idle_ms", 600))
                    if pending_copilot and (ctrl.idle_seconds() >= max(0, quiet_idle_ms) / 1000.0) and (executed_this_tick == 0):
                        item = pending_copilot.pop(0)
                        kind = item.get("kind")
                        q = item.get("q", "")
                        action_log.log("copilot", op="quiet_send", kind=kind, preview=q[:160])
                        if kind == "app":
                            vs.ask_copilot_app(q)
                        else:
                            messenger = CopilotMessenger(root, vs, ctrl, ocr, log_run, log_improve, rules, ui_state, phi4_client=phi_client)
                            messenger.send_or_plan(q)
                except Exception:
                    pass

                # Periodic cleanup of old frames/movies
                try:
                    if cleaner is not None:
                        interval = max(1, int(cleanup_cfg.get("interval_seconds", 5)))
                        now_t = time.time()
                        if now_t - last_cleanup_t["t"] >= interval:
                            cleaner.clean_once()
                            last_cleanup_t["t"] = now_t
                except Exception:
                    pass

                if headless_duration_s is not None and (time.time() - start) >= max(0, int(headless_duration_s)):
                    break
                time.sleep(max(0.001, 1.0 / max(1, int(fps))))
        except KeyboardInterrupt:
            log_run("Headless loop interrupted; stopping")
        finally:
            try:
                on_stop()
                action_log.log("recording", action="stop", ok=True)
            except Exception:
                pass
        return

    # Window listing/selection handlers for UI
    def format_entry(w: dict) -> str:
        title = (w.get("title") or "").strip()
        cls = (w.get("class") or "").strip()
        if title:
            disp = f"{title} [{cls}]" if cls else title
        else:
            disp = f"[{cls}]"
        return disp

    def on_list_windows_ui() -> list[str]:
        nonlocal target_map
        try:
            wins = winman.list_windows()
        except Exception:
            wins = []
        target_map = {format_entry(w): w for w in wins}
        out = ["(none)"] + list(target_map.keys())
        return out

    def on_select_target_ui(name: str):
        selected_target_name["name"] = name if name and name != "(none)" else None

    ui = AppUI(
        root,
        on_run,
        on_pause,
        on_resume,
        on_stop,
        on_user_msg,
        on_upload_files,
        on_select_project,
        on_focus_vscode,
        on_toggle_controls,
        on_list_windows=on_list_windows_ui,
        on_select_target=on_select_target_ui,
    )
    ui.on_focus_terminal = on_focus_terminal
    ui.on_send_metadata = on_send_metadata
    # Quick chat scrolling actions (UI)
    def on_scroll_chat_down(steps: int = 3):
        try:
            action_log.log("copilot", op="scroll_chat", source="ui", direction="down", steps=int(steps))
            vs.scroll_chat(direction="down", steps=int(steps))
        except Exception:
            pass
    def on_scroll_chat_up(steps: int = 3):
        try:
            action_log.log("copilot", op="scroll_chat", source="ui", direction="up", steps=int(steps))
            vs.scroll_chat(direction="up", steps=int(steps))
        except Exception:
            pass
    ui.on_scroll_chat_down = on_scroll_chat_down
    ui.on_scroll_chat_up = on_scroll_chat_up
    # Runtime automation toggle: flip VSBridge dry_run and persist to config
    def on_toggle_automation():
        try:
            # Flip state
            vs.dry_run = not bool(getattr(vs, "dry_run", False))
            # Persist to policy_rules.json
            try:
                data = json.loads((root / "config/policy_rules.json").read_text(encoding="utf-8"))
            except Exception:
                data = {}
            data.setdefault("vsbridge", {})["dry_run"] = bool(vs.dry_run)
            (root / "config/policy_rules.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
            log_run(f"Automation dry_run set to {vs.dry_run}")
            action_log.log("automation_toggle", dry_run=bool(vs.dry_run))
            return (not vs.dry_run)  # enabled if not dry_run
        except Exception as e:
            log_run(f"Toggle automation failed: {e}")
            action_log.log("automation_toggle", error=str(e))
            return (not bool(getattr(vs, "dry_run", True)))
    ui.on_toggle_automation = on_toggle_automation
    # Agent Mode toggle and persistence
    def on_toggle_agent():
        try:
            state["agent_mode"] = not bool(state.get("agent_mode", False))
            data = {}
            try:
                data = load_ui_state()
            except Exception:
                data = {}
            data["agent_mode"] = state["agent_mode"]
            save_ui_state(data)
            log_run(f"Agent Mode set to {state['agent_mode']}")
            action_log.log("agent_mode_toggle", enabled=bool(state["agent_mode"]))
            return state["agent_mode"]
        except Exception as e:
            action_log.log("agent_mode_toggle", error=str(e))
            return bool(state.get("agent_mode", False))
    ui.on_toggle_agent = on_toggle_agent
    
    # OCR toggle: enable/disable via config/ocr.json and live object
    def on_toggle_ocr():
        try:
            current = bool(getattr(ocr, "enabled", True))
            new_state = not current
            # persist
            cfg = {}
            try:
                if ocr_cfg_path.exists():
                    cfg = json.loads(ocr_cfg_path.read_text(encoding="utf-8"))
            except Exception:
                cfg = {}
            cfg["enabled"] = new_state
            ocr_cfg_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
            # live toggle
            ocr.enabled = new_state
            log_run(f"OCR enabled set to {new_state}")
            action_log.log("ocr_toggle", enabled=new_state)
            return new_state
        except Exception as e:
            log_run(f"Toggle OCR failed: {e}")
            return bool(getattr(ocr, "enabled", True))
    ui.on_toggle_ocr = on_toggle_ocr

    # Planner connectivity test
    def on_test_planner():
        if not phi_client:
            return False, "Planner not configured"
        try:
            res = phi_client.ping()
            ok = bool(res.get("ok", False))
            msg = ("Planner OK" if ok else f"Planner error: {res.get('error','unknown')}")
            log_run(msg)
            action_log.log("phi4_ping", ok=ok, detail=res)
            return ok, msg
        except Exception as e:
            log_run(f"Planner test failed: {e}")
            action_log.log("phi4_ping", ok=False, error=str(e))
            return False, str(e)
    ui.on_test_planner = on_test_planner
    interactive_banner()

    # Configure window gate in controller
    def window_gate() -> bool:
        name = selected_target_name.get("name")
        if not name:
            return True  # no gating
        try:
            fg = winman.get_foreground()
            if not fg:
                return False
            info = winman.get_window_info(fg)
            # match via substring against selected mapping
            target = target_map.get(name) or {}
            tsub = (target.get("title") or "").lower()
            csub = (target.get("class") or "").lower()
            tcur = (info.get("title") or "").lower()
            ccur = (info.get("class") or "").lower()
            # require both substrings if both present; otherwise match whichever provided
            ok_title = (not tsub) or (tsub in tcur)
            ok_class = (not csub) or (csub in ccur)
            return bool(ok_title and ok_class)
        except Exception:
            return True  # fail-open

    def controls_owner_gate() -> bool:
        """Additional gate: respect shared controls_state.json ownership.

        If another owner (e.g., user/script) has marked controls as in use,
        AI automation yields and does not send mouse/keyboard events.
        """
        try:
            st = get_controls_state(root) or {}
            owner = str(st.get("owner", "") or "")
            # When owner is empty or "agent", allow; otherwise yield.
            if not owner or owner == "agent":
                return True
            return False
        except Exception:
            # Fail-open on read errors to avoid deadlocking controls.
            return True

    def composite_gate() -> bool:
        return window_gate() and controls_owner_gate()

    ctrl.set_window_gate(composite_gate)

    # Hydrate UI with last files and populate app list
    try:
        last_files = ui_state.get("files", [])
        if last_files:
            for p in last_files:
                ui.files_list.insert(0, str(p))
            state["objectives"] = [Path(p) for p in last_files]
        else:
            # If no persisted objectives, default to config/objectives.md (README workflow)
            default_obj = root / "config" / "objectives.md"
            if default_obj.exists():
                ui.files_list.insert(0, str(default_obj))
                state["objectives"] = [default_obj]
        # Initialize automation button to reflect current dry_run
        ui.set_automation_state(enabled=(not bool(getattr(vs, "dry_run", False))))
        # Initialize OCR button state based on config
        try:
            ui.set_ocr_state(bool(getattr(ocr, "enabled", True)))
        except Exception:
            pass
        # Initialize Agent Mode
        try:
            state["agent_mode"] = bool(ui_state.get("agent_mode", False))
            ui.set_agent_state(state["agent_mode"])
        except Exception:
            pass
        # Populate initial window list
        try:
            ui.set_target_options(on_list_windows_ui())
        except Exception:
            pass
    except Exception:
        pass

    # Global ESC toggle for control pause (mouse+keyboard)
    try:
        from pynput import keyboard  # type: ignore

        def on_press(key):
            try:
                if key == keyboard.Key.esc:
                    paused = ctrl.toggle_controls_paused()
                    msg = "Controls paused (ESC)" if paused else "Controls resumed (ESC)"
                    log_run(msg)
                    action_log.log("controls_toggle", source="esc", paused=paused)
            except Exception:
                pass

        esc_listener = keyboard.Listener(on_press=on_press)
        esc_listener.daemon = True
        esc_listener.start()
    except Exception:
        log_run("ESC listener not available; pynput missing or failed to start")

    def tick():
        if not state["stop"]:
            # Auto-run when Agent Mode is enabled (unless paused or already running)
            try:
                if state.get("agent_mode") and not state.get("running") and not state.get("paused"):
                    # Ensure an objectives source exists when using Agent Mode
                    if not state.get("objectives"):
                        default_obj = root / "config" / "objectives.md"
                        if default_obj.exists():
                            state["objectives"] = [default_obj]
                    log_run("Agent Mode auto-run")
                    action_log.log("agent_mode", action="auto_run")
                    on_run()
            except Exception:
                pass
            if state["running"] and not state["paused"]:
                cap.grab_frame()
                executed_this_tick, performed_non_copilot = _execute_objectives_once(max_tasks=10)
            # Update status and small timer near button
            cycle_in_control, paused_controls, elapsed, total = ctrl.control_phase_info()
            remaining = max(0, int(total - elapsed))
            # Update shared control-window info (UI mode)
            try:
                update_control_window(root, bool(cycle_in_control and not paused_controls), float(remaining))
            except Exception:
                pass
            # Adaptive automation pacing: faster during control, slower during release
            try:
                active_ms = int(vs_cfg.get("delay_ms_active", vs_cfg.get("delay_ms", 300)))
                release_ms = int(vs_cfg.get("delay_ms_release", max(350, int(vs_cfg.get("delay_ms", 300)))))
                vs.delay = (active_ms if (cycle_in_control and not paused_controls) else release_ms) / 1000.0
            except Exception:
                pass
            if paused_controls:
                phase = "Controls: Paused"
                timer_text = "Paused"
                timer_color = "red"
            else:
                phase = "Controls: Active" if cycle_in_control else "Controls: Release"
                timer_text = (f"Active: {remaining}s" if cycle_in_control else f"Release: {remaining}s")
                timer_color = "green" if cycle_in_control else "orange"
            ui.status_var.set(f"Running - {phase} ({int(elapsed)}/{int(total)}s)")
            ui.set_controls_timer(timer_text, timer_color)
            # Foreground status label
            try:
                fg = winman.get_foreground()
                if fg:
                    info = winman.get_window_info(fg)
                    disp = (info.get("title") or "").strip() or "(untitled)"
                    cls = (info.get("class") or "").strip()
                    if cls:
                        disp = f"{disp} [{cls}]"
                    ui.set_foreground_status(f"Foreground: {disp}")
                else:
                    ui.set_foreground_status("Foreground: (unknown)")
            except Exception:
                pass
            # OCR observer polling ("movie")
            try:
                if ocr_obs is not None:
                    ocr_obs.poll()
            except Exception:
                pass

            # VS Code multi-window orchestrator tick (UI mode)
            try:
                if keepalive is not None:
                    interval = float(orchestrator_cfg.get("interval_s", 6.0))
                    if interval > 0:
                        now_t = time.time()
                        if now_t - last_keepalive_t["t"] >= interval:
                            summary = keepalive.cycle_once()
                            last_keepalive_t["t"] = now_t
                            try:
                                action_log.log(
                                    "orchestrator",
                                    op="multi_window_keepalive",
                                    mode="ui",
                                    windows=int(summary.get("windows_scanned", 0)),
                                    actions=int(summary.get("actions_taken", 0)),
                                )
                            except Exception:
                                pass
            except Exception:
                pass
            # Quiet-send any deferred Copilot messages when idle and no other work performed
            try:
                cp_cfg = (rules.get("copilot") or {})
                quiet_idle_ms = int(cp_cfg.get("quiet_idle_ms", 600))
                if pending_copilot and (ctrl.idle_seconds() >= max(0, quiet_idle_ms) / 1000.0) and (executed_this_tick == 0):
                    item = pending_copilot.pop(0)
                    kind = item.get("kind")
                    q = item.get("q", "")
                    action_log.log("copilot", op="quiet_send", kind=kind, preview=q[:160])
                    if kind == "app":
                        vs.ask_copilot_app(q)
                    else:
                        messenger = CopilotMessenger(root, vs, ctrl, ocr, log_run, log_improve, rules, ui_state, phi4_client=phi_client)
                        messenger.send_or_plan(q)
            except Exception:
                pass
            # Periodic cleanup of old frames/movies
            try:
                if cleaner is not None:
                    interval = max(1, int(cleanup_cfg.get("interval_seconds", 5)))
                    now_t = time.time()
                    if now_t - last_cleanup_t["t"] >= interval:
                        cleaner.clean_once()
                        last_cleanup_t["t"] = now_t
            except Exception:
                pass
            ui.tk.after(200, tick)
        else:
            cap.stop()
            action_log.log("recording", action="stop", ok=True)

    # Schedule periodic work via Tk event loop and start UI in main thread
    ui.tk.after(200, tick)
    ui.start()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="AI_Coder_Controller runner")
    parser.add_argument("--headless", action="store_true", help="Run without UI (record/log only)")
    parser.add_argument("--agent", action="store_true", help="In headless mode, execute objectives (Agent Mode)")
    parser.add_argument(
        "--objectives",
        action="append",
        default=None,
        help="Objective .md file path (repeatable). Defaults to config/objectives.md or ui_state.json files.",
    )
    parser.add_argument("--fps", type=int, default=20, help="Recording frames per second")
    parser.add_argument("--duration", type=int, default=None, help="Headless run duration in seconds (optional)")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    try:
        run(
            root,
            fps=int(args.fps),
            headless=bool(args.headless),
            headless_duration_s=args.duration,
            headless_agent_mode=bool(args.agent),
            objectives=args.objectives,
        )
    except KeyboardInterrupt:
        console.log("Interrupted by user (Ctrl+C). Exiting cleanly.")
        try:
            # Friendly exit code for user-initiated interrupt
            sys.exit(0)
        except SystemExit:
            pass
