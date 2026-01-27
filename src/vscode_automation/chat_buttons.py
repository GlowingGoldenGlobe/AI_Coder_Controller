"""Compatibility shim.

The authoritative VS Code chat button analyzer lives in the top-level
``vscode_automation`` package.
"""

from vscode_automation.chat_buttons import ChatButtonAnalyzer  # type: ignore

__all__ = [
    "ChatButtonAnalyzer",
]
