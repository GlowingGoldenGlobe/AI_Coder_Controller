from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class MessageOptions:
    enabled: bool = True
    max_length: int = 400
    compose_when_prompts_match: List[str] = field(default_factory=list)
    default_templates: List[Dict[str, Any]] = field(default_factory=list)
    allow_auto_send: bool = False
    press_enter: bool = True
    focus_input: bool = True
    send_keys: List[str] = field(default_factory=list)


@dataclass
class OrchestratorOptions:
    enabled: bool = True
    interval_s: float = 6.0
    max_windows_per_cycle: int = 8
    action_hints: List[str] = field(default_factory=list)
    message: MessageOptions = field(default_factory=MessageOptions)
    message_strategy: str = "first"  # how to pick templates: first|cycle

    @classmethod
    def load(cls, root: Optional[Path] = None) -> "OrchestratorOptions":
        base = Path(root) if root is not None else Path(__file__).resolve().parent.parent
        cfg_path = base / "config" / "vscode_orchestrator.json"
        data: Dict[str, Any] = {}
        try:
            if cfg_path.is_file():
                data = json.loads(cfg_path.read_text(encoding="utf-8"))
        except Exception:
            data = {}

        msg_raw = data.get("message") or {}
        msg = MessageOptions(
            enabled=bool(msg_raw.get("enabled", True)),
            max_length=int(msg_raw.get("max_length", 400)),
            compose_when_prompts_match=list(msg_raw.get("compose_when_prompts_match") or []),
            default_templates=list(msg_raw.get("default_templates") or []),
            allow_auto_send=bool(msg_raw.get("allow_auto_send", False)),
            press_enter=bool(msg_raw.get("press_enter", True)),
            focus_input=bool(msg_raw.get("focus_input", True)),
            send_keys=list(msg_raw.get("send_keys") or []),
        )

        return cls(
            enabled=bool(data.get("enabled", True)),
            interval_s=float(data.get("interval_s", 6.0)),
            max_windows_per_cycle=int(data.get("max_windows_per_cycle", 8)),
            action_hints=list(data.get("action_hints") or []),
            message=msg,
            message_strategy=str(data.get("message_strategy", "first")),
        )
