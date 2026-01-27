from __future__ import annotations
from pathlib import Path
from typing import Dict, Any
import json
import datetime


def compose_prompt_and_contingencies(question: str, context: Dict[str, Any]) -> Dict[str, Any]:
    """PHI-4 SLM stub: returns a proposed Copilot prompt and a contingency plan.

    This is a placeholder for an SLM. It uses simple heuristics to document why
    sending was deferred and what to do next.
    """
    now = datetime.datetime.now().isoformat(timespec="seconds")
    reasons = []
    pre = context.get("preconditions", {})
    if not pre.get("controls_active", True):
        reasons.append("Controls not active (safety window or paused)")
    if not pre.get("vscode_focus", False):
        reasons.append("VS Code not focused or not detected")
    if pre.get("dry_run", False) and not context.get("allow_dry_run_send", False):
        reasons.append("Dry-run enabled; sending disabled by policy")
    if context.get("require_ocr_for_read", False) and not pre.get("ocr_available", False):
        reasons.append("OCR unavailable but required for reading")

    # Basic prompt template with question and context snapshot
    snapshot = {
        "question": question,
        "files_selected": context.get("files", []),
        "project": context.get("project"),
        "vsbridge": {
            "delay_ms": context.get("delay_ms"),
            "dry_run": pre.get("dry_run"),
        },
        "preconditions": pre,
        "timestamp": now,
    }
    prompt = (
        "Analyze the attached context and suggest next steps to complete the task.\n"
        "Focus on modularity, safety, and automation. Then summarize actionable changes.\n"
        f"User question: {question}\n"
        f"Context snapshot: {json.dumps(snapshot, indent=2)}\n"
    )

    plan = [
        "Ensure VS Code is open and focused",
        "Verify automation window is active (controls not paused)",
        "Disable dry-run if ready to actually send keystrokes",
        "If OCR is required, install/configure Tesseract and pytesseract",
        "Retry sending the prompt once preconditions hold",
    ]

    return {
        "prompt": prompt,
        "reasons": reasons,
        "plan": plan,
        "timestamp": now,
    }
