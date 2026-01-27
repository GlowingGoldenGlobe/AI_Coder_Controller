"""Compatibility shim for legacy imports.

This module forwards to the root-level ``vscode_automation`` package so
there is a single authoritative orchestrator implementation.
"""

from vscode_automation import (  # type: ignore
    VSCodeWindowSet,
    ChatButtonAnalyzer,
    MultiWindowChatKeepalive,
    run_multi_window_keepalive_cycle,
)

__all__ = [
    "VSCodeWindowSet",
    "ChatButtonAnalyzer",
    "MultiWindowChatKeepalive",
    "run_multi_window_keepalive_cycle",
]
