from dataclasses import dataclass
from typing import Optional, Dict, Any, List
import re
from pathlib import Path

@dataclass
class Action:
    kind: str  # "move", "click", "type", "hotkey", "noop", "vscode", "copilot"
    params: Dict[str, Any]


class Policy:
    def __init__(self, rules: Dict[str, Any]):
        self.rules = rules
        self.enabled = rules.get("enabled", True)

    def parse_objectives(self, files: List[Path]) -> List[Dict[str, Any]]:
        tasks: List[Dict[str, Any]] = []
        for f in files:
            try:
                text = Path(f).read_text(encoding="utf-8")
            except Exception:
                continue
            for idx, line in enumerate(text.splitlines()):
                s = line.strip()
                if not s:
                    continue
                if s.startswith("#"):
                    continue
                tasks.append({"text": s, "file": str(f), "line": idx + 1})
        return tasks

    def decide(self, objective_text: str) -> Optional[Action]:
        if not self.enabled:
            return Action("noop", {})
        s = objective_text.strip()
        # Normalize common list prefixes like "1. " or "2) " so
        # objectives can be written as numbered lists without
        # breaking pattern matching.
        try:
            s = re.sub(r"^\s*\d+[\.\)]\s*", "", s)
        except Exception:
            pass
        sl = s.lower()
        if "start screen recording" in sl:
            return Action("vscode", {"op": "record_toggle_on"})
        if sl.startswith("open vscode"):
            return Action("vscode", {"op": "open_vscode"})
        if "focus vscode" in sl or "focus vs code" in sl:
            return Action("vscode", {"op": "focus_vscode"})
        if "focus terminal" in sl or "open terminal" in sl:
            return Action("vscode", {"op": "focus_terminal"})
        if sl.startswith("open folder:"):
            path = s.split(":", 1)[1].strip()
            return Action("vscode", {"op": "open_folder", "path": path})
        if sl.startswith("open file:"):
            path = s.split(":", 1)[1].strip()
            return Action("vscode", {"op": "open_file", "path": path})
        if ("ask copilot" in sl) or ("copilot" in sl and "clarify" in sl):
            q = s.split(":", 1)[-1].strip() if ":" in s else s
            return Action("copilot", {"op": "ask", "question": q})
        # Windows Copilot app directives
        if ("focus copilot app" in sl) or ("focus windows copilot" in sl):
            return Action("copilot", {"op": "focus_app"})
        if sl.startswith("ask copilot app:") or sl.startswith("copilot app:"):
            q = s.split(":", 1)[-1].strip()
            return Action("copilot", {"op": "ask_app", "question": q})
        # Deferred send (queue until Stop) to avoid interference during active work
        if sl.startswith("ask copilot after stop:"):
            q = s.split(":", 1)[-1].strip()
            return Action("copilot", {"op": "ask_after_stop", "question": q})
        if sl.startswith("ask copilot app after stop:"):
            q = s.split(":", 1)[-1].strip()
            return Action("copilot", {"op": "ask_app_after_stop", "question": q})
        # Specific: insert summary from Copilot app (Windows)
        m = re.search(r"\binsert\b.*\bsummary\b.*\bapp\b\s+into\s+(.+)$", s, flags=re.IGNORECASE)
        if m:
            path = (m.group(1) or "").strip().strip('"')
            return Action("copilot", {"op": "insert_summary_app_into_file", "path": path})

        m = re.search(r"\binsert\b.*\bsummary\b\s+into\s+(.+)$", s, flags=re.IGNORECASE)
        if m:
            path = (m.group(1) or "").strip().strip('"')
            return Action("copilot", {"op": "insert_summary_into_file", "path": path})

        if ("insert" in sl and "summary app" in sl) or sl.startswith("insert summary app"):
            return Action("copilot", {"op": "insert_summary_app"})
        if ("insert" in sl and "summary" in sl) or ("insert" in sl and "copilot" in sl):
            # Trigger OCR-based capture and append summary
            return Action("copilot", {"op": "insert_summary"})
        if "scroll chat" in sl:
            # Patterns like: "Scroll chat down x 3", "Scroll chat up 5", "Scroll chat"
            direction = "down" if "up" not in sl else "up"
            steps = 3
            m = re.search(r"\b(?:x\s*)?(\d+)\b", sl)
            if m:
                try:
                    steps = max(1, int(m.group(1)))
                except Exception:
                    steps = 3
            return Action("copilot", {"op": "scroll_chat", "direction": direction, "steps": steps})
        if sl.startswith("terminal:") or sl.startswith("run terminal:"):
            # e.g., "terminal: git status" or "run terminal: pip list"
            try:
                cmd = s.split(":", 1)[1].strip()
            except Exception:
                cmd = ""
            return Action("terminal", {"op": "run", "cmd": cmd})
        # Queue a terminal command to send AFTER stop
        if sl.startswith("terminal after stop:") or sl.startswith("queue terminal:") or sl.startswith("queue terminal after stop:"):
            try:
                cmd = s.split(":", 1)[1].strip()
            except Exception:
                cmd = ""
            return Action("terminal", {"op": "queue_after_stop", "cmd": cmd})
        if sl.startswith("stop"):
            return Action("vscode", {"op": "stop"})
        # Prepare a Copilot context pack (ContextPack_Current.md) for this project
        if "prepare context pack" in sl or "prepare copilot context" in sl:
            return Action("agent", {"op": "run_module", "module": "Scripts.prepare_context_pack"})
        # Timed commit via PowerShell after stop (with optional delay/loop/message)
        if ("commit copilot" in sl) or ("copilot commit" in sl) or ("commit message" in sl and "copilot" in sl):
            # Parse simple patterns: "in 7s", "every 10s", "x 3", and "message: TEXT"
            start_after = 0
            repeat_s = 0
            repeat_n = 1
            msg = None
            m = re.search(r"in\s+(\d+)s", sl)
            if m:
                try:
                    start_after = int(m.group(1))
                except Exception:
                    start_after = 0
            m = re.search(r"every\s+(\d+)s", sl)
            if m:
                try:
                    repeat_s = int(m.group(1))
                except Exception:
                    repeat_s = 0
            m = re.search(r"x\s*(\d+)", sl)
            if m:
                try:
                    repeat_n = max(1, int(m.group(1)))
                except Exception:
                    repeat_n = 1
            else:
                # If a repeat interval is provided but no count, loop indefinitely (0)
                if repeat_s > 0:
                    repeat_n = 0
            if ":" in s:
                try:
                    msg = s.split(":", 1)[1].strip()
                except Exception:
                    msg = None
            # Build PS command
            parts = [
                "powershell -NoProfile -ExecutionPolicy Bypass -File scripts/copilot_commit_start.ps1 -Mode app",
                f"-StartAfterSeconds {start_after}" if start_after > 0 else None,
                f"-RepeatSeconds {repeat_s}" if repeat_s > 0 else None,
                f"-RepeatCount {repeat_n}",
                f"-Message '{msg}'" if msg else None,
            ]
            cmd = " ".join([p for p in parts if p])
            return Action("terminal", {"op": "queue_after_stop", "cmd": cmd})
        # Agent terminal control
        if sl.startswith("agent:"):
            rest = s.split(":", 1)[1].strip()
            if rest.lower().startswith("launch ui"):
                return Action("agent", {"op": "launch_ui"})
            if rest.lower().startswith("terminal:"):
                cmd = rest.split(":", 1)[1].strip()
                return Action("agent", {"op": "terminal", "cmd": cmd})
            if rest.lower().startswith("run module:"):
                mod = rest.split(":", 1)[1].strip()
                return Action("agent", {"op": "run_module", "module": mod})
        return Action("noop", {})
