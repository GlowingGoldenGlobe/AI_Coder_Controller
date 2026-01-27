from __future__ import annotations

"""Entry point for running the Orchestrator Agent in Agent Mode.

This script treats the current controller + orchestrator as an "Agent"
whose sole objective set is config/objectives_orchestrator.md.

Usage (from repo root, with venv):

    Scripts/python.exe Scripts/orchestrator_agent.py

This will:
- Run src.main in headless Agent Mode.
- Load objectives from config/objectives_orchestrator.md.
- Allow the root-level vscode_automation orchestrator to keep VS Code
  Agent Mode editor tabs and chats active according to the objectives
  and config/vscode_orchestrator.json.
"""

import subprocess
import sys
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    cmd = [
        sys.executable,
        "-m",
        "src.main",
        "--headless",
        "--agent",
        "--objectives",
        "config/objectives_orchestrator.md",
    ]
    return subprocess.call(cmd, cwd=str(root))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
