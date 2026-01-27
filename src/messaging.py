from __future__ import annotations
from pathlib import Path
from typing import Dict, Any, Optional, List
import time

from src.phi4_planner import compose_prompt_and_contingencies


class CopilotMessenger:
    def __init__(self, root: Path, vsbridge, ctrl, ocr, log_send, log_plan, rules: Dict[str, Any], ui_state: Dict[str, Any], phi4_client: Optional[object] = None):
        self.root = root
        self.vs = vsbridge
        self.ctrl = ctrl
        self.ocr = ocr
        self.log_send = log_send
        self.log_plan = log_plan
        self.rules = rules or {}
        self.ui_state = ui_state or {}
        self.phi4_client = phi4_client

        self.cfg = (self.rules.get("copilot") or {})
        self.send_if = (self.cfg.get("send_if") or {})
        self.read_wait_ms = int(self.cfg.get("read_wait_ms", 1500))

        # Default routing target
        self.prefer_app = bool(self.cfg.get("prefer_app", False))

    def _preconditions(self, target: str = "vscode") -> Dict[str, Any]:
        # controls must be allowed
        controls_active = self.ctrl.is_controls_allowed()
        # focus (best effort)
        focused = False
        try:
            if target == "app":
                focused = bool(self.vs.focus_copilot_app())
            else:
                focused = bool(self.vs.focus_vscode_window())
        except Exception:
            focused = False
        # dry-run flag
        dry_run = bool(getattr(self.vs, "dry_run", True))
        # ocr available
        try:
            ok = hasattr(self.ocr, "capture_chat_text") and self.ocr is not None
        except Exception:
            ok = False
        return {
            "controls_active": controls_active,
            "focus": focused,
            "dry_run": dry_run,
            "ocr_available": ok,
        }

    def send_or_plan(self, question: str, *, force_target: Optional[str] = None) -> Dict[str, Any]:
        target = (force_target or ("app" if self.prefer_app else "vscode")).strip().lower()
        if target not in {"app", "vscode"}:
            target = "vscode"

        pre = self._preconditions(target=target)
        # Historically this setting was VS Code-specific; keep it but only apply to VS Code target.
        require_focus = bool(self.send_if.get("require_vscode_focus", True)) and (target == "vscode")
        require_controls = bool(self.send_if.get("require_controls_active", True))
        allow_dry = bool(self.send_if.get("allow_dry_run_send", False))
        require_ocr = bool(self.send_if.get("require_ocr_for_read", False))

        can_send = True
        if require_focus and not pre["focus"]:
            can_send = False
        if require_controls and not pre["controls_active"]:
            can_send = False
        if not allow_dry and pre["dry_run"]:
            can_send = False

        ui_files: List[str] = [str(p) for p in self.ui_state.get("files", [])] if isinstance(self.ui_state.get("files"), list) else self.ui_state.get("files", [])
        ctx = {
            "files": ui_files,
            "project": self.ui_state.get("project"),
            "delay_ms": int(getattr(self.vs, "delay", 0) * 1000),
            "allow_dry_run_send": allow_dry,
            "require_ocr_for_read": require_ocr,
            "target": target,
            "preconditions": pre,
        }

        if not can_send:
            # Prefer remote PHI-4 when configured; fall back to local stub
            if self.phi4_client is not None:
                try:
                    plan = self.phi4_client.compose(question, ctx)
                except Exception as e:
                    self.log_plan(f"PHI-4 client error: {e}; falling back to local stub")
                    plan = compose_prompt_and_contingencies(question, ctx)
            else:
                plan = compose_prompt_and_contingencies(question, ctx)
            self.log_plan(f"PHI-4 plan created; reasons: {', '.join(plan['reasons']) if plan['reasons'] else 'none'}")
            # Persist plan under Self-Improve/improvements.md
            try:
                imp = self.root / "projects" / "Self-Improve" / "improvements.md"
                imp.parent.mkdir(parents=True, exist_ok=True)
                with open(imp, "a", encoding="utf-8") as f:
                    f.write("\n\n## PHI-4 Contingency Plan\n\n")
                    if plan.get("prompt"):
                        f.write(plan["prompt"] + "\n")
                    f.write("\nContingencies:\n")
                    for item in plan["plan"]:
                        f.write(f"- {item}\n")
            except Exception:
                pass
            return {"sent": False, "planned": True, "plan": plan}

        # Send the question via VSBridge
        sent_ok = True
        if target == "app":
            try:
                sent_ok = bool(self.vs.ask_copilot_app(question))
            except Exception:
                sent_ok = False
            if not sent_ok:
                # One retry: Win+C / foreground gating can be flaky on first attempt
                try:
                    self.log_send("Copilot app send failed; retrying once")
                except Exception:
                    pass
                try:
                    time.sleep(1.0)
                    sent_ok = bool(self.vs.ask_copilot_app(question))
                except Exception:
                    sent_ok = False
            if not sent_ok:
                # Fallback to VS Code Chat if app send fails
                try:
                    self.vs.ask_copilot(question)
                    sent_ok = True
                    target = "vscode"
                except Exception:
                    sent_ok = False
        else:
            try:
                self.vs.ask_copilot(question)
                sent_ok = True
            except Exception:
                sent_ok = False

        self.log_send(f"Copilot message sent ({'app' if target == 'app' else 'vscode_chat'})" if sent_ok else "Copilot message send failed")
        if not sent_ok:
            return {"sent": False, "planned": False, "error": "send_failed", "target": target}
        # Optionally wait and read OCR
        if require_ocr and not pre["ocr_available"]:
            self.log_plan("Skipping OCR read (required but unavailable)")
            return {"sent": True, "read": False}
        # Wait then try OCR read (VSBridge returns plain text)
        time.sleep(max(0, self.read_wait_ms) / 1000.0)
        try:
            if target == "app":
                txt = self.vs.read_copilot_app_text(self.ocr, save_dir=self.root / "logs" / "ocr")
            else:
                txt = self.vs.read_copilot_chat_text(self.ocr, save_dir=self.root / "logs" / "ocr")
            if isinstance(txt, str) and txt.strip():
                text = txt.strip()
                imp = self.root / "projects" / "Self-Improve" / "improvements.md"
                try:
                    with open(imp, "a", encoding="utf-8") as f:
                        f.write("\n\n## Copilot Readback\n\n")
                        f.write(text + "\n")
                except Exception:
                    pass
                return {"sent": True, "read": True, "text": text, "target": target}
            return {"sent": True, "read": False, "target": target}
        except Exception as e:
            self.log_plan(f"OCR read failed: {e}")
            return {"sent": True, "read": False, "target": target}
