from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from vscode_automation import run_multi_window_keepalive_cycle as _root_run_cycle  # type: ignore


def run_multi_window_keepalive_cycle(root: Optional[Path] = None) -> Dict[str, Any]:
    """Compatibility wrapper that forwards to the root orchestrator helper.

    Existing callers using ``src.vscode_automation.helpers`` continue to work,
    but all behavior is centralized in the top-level ``vscode_automation``
    package.
    """
    base = Path(root) if root is not None else Path(__file__).resolve().parent.parent
    return _root_run_cycle(base)
