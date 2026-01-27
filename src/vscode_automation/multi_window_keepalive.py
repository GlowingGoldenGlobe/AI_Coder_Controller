"""Compatibility shim.

The authoritative multi-window VS Code chat keepalive lives in the top-level
``vscode_automation`` package.
"""

from vscode_automation.multi_window_keepalive import MultiWindowChatKeepalive  # type: ignore

__all__ = [
    "MultiWindowChatKeepalive",
]
