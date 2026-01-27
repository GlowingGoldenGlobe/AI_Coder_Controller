"""Compatibility shim.

The authoritative VS Code window discovery helpers live in the top-level
``vscode_automation`` package.
"""

from vscode_automation.window_set import VSCodeWindow, VSCodeWindowSet  # type: ignore

__all__ = [
    "VSCodeWindow",
    "VSCodeWindowSet",
]
