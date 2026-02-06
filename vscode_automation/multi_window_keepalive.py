from __future__ import annotations

import time
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from src.control import Controller
from src.ocr import CopilotOCR
from src.windows import WindowsManager
from src.jsonlog import JsonActionLogger
from src.control_state import get_controls_state, is_state_stale

from .window_set import VSCodeWindowSet
from .chat_buttons import ChatButtonAnalyzer
from .config import OrchestratorOptions


class MultiWindowChatKeepalive:
	"""Scan all VS Code windows and nudge stalled chats via button clicks.

	This is a small orchestration layer that composes VSCodeWindowSet and
	ChatButtonAnalyzer to extend the existing single-window workflows:

	- Enumerates all visible Code.exe / VS Code windows.
	- For each, focuses the window and captures the configured chat ROI image.
	- When image analysis detects actionable button-like UI elements, it clicks
	  a primary button inside that ROI.

	The goal is to prevent agent workflows from stalling when multiple VS Code
	windows are open and any of them block on a chat-UI button.
	"""

	def __init__(
		self,
		ctrl: Controller,
		ocr: CopilotOCR,
		winman: Optional[WindowsManager] = None,
		log: Optional[JsonActionLogger] = None,
		delay_ms: int = 400,
		action_hints: Optional[Sequence[str]] = None,
		options: Optional[OrchestratorOptions] = None,
	) -> None:
		self.ctrl = ctrl
		self.ocr = ocr
		self.winman = winman or WindowsManager()
		root = Path(__file__).resolve().parent.parent
		self.options = options or OrchestratorOptions.load(root)
		self.windows = VSCodeWindowSet(self.winman)
		self.buttons = ChatButtonAnalyzer(
			ocr=self.ocr,
			ctrl=self.ctrl,
			winman=self.winman,
			delay_ms=delay_ms,
			options=self.options,
		)
		self.log = log or JsonActionLogger(root / "logs" / "actions" / "vscode_multi_keepalive.jsonl")
		self.delay_s = max(0, int(delay_ms)) / 1000.0
		self.action_hints = tuple(action_hints) if action_hints is not None else tuple(self.options.action_hints or ChatButtonAnalyzer.DEFAULT_ACTION_HINTS)

	def _log_event(self, event: str, **data: Any) -> None:
		try:
			self.log.log(event, **data)
		except Exception:
			pass

	def cycle_once(self, max_windows: Optional[int] = None, target_key: str = "vscode_chat") -> Dict[str, Any]:
		"""Run a single keepalive pass over all VS Code windows.

		Returns a summary dict with per-window results, suitable for piping
		into higher-level assessment or self-improvement flows.
		"""
		root = Path(__file__).resolve().parent.parent
		try:
			st = get_controls_state(root) or {}
			rules_path = root / "config" / "policy_rules.json"
			rules = json.loads(rules_path.read_text(encoding="utf-8")) if rules_path.exists() else {}
			controls_cfg = (rules.get("controls") or {}) if isinstance(rules, dict) else {}
			stale_after_s = float(controls_cfg.get("stale_after_s", 10.0) or 10.0)
			paused = bool(st.get("paused", False))
			stale = bool(is_state_stale(st, stale_after_s))
			if paused and not stale:
				summary = {
					"windows_scanned": 0,
					"actions_taken": 0,
					"results": [],
					"timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
					"skipped": "controls_paused",
				}
				self._log_event("vscode_multi_keepalive_cycle_skipped", reason="controls_paused")
				return summary
		except Exception:
			pass

		ws = self.windows.list_vscode_windows()
		limit = max_windows
		if limit is None or limit < 0:
			limit = int(self.options.max_windows_per_cycle)
		if limit is not None and limit >= 0:
			ws = ws[: int(limit)]

		results: List[Dict[str, Any]] = []
		actions = 0
		for w in ws:
			try:
				rec = self.buttons.click_primary_chat_button(
					hwnd=w.hwnd,
					target_key=target_key,
					action_hints=self.action_hints,
				)
				rec["window_title"] = w.title
				rec["window_process"] = w.process
				results.append(rec)
				if rec.get("clicked"):
					actions += 1
					# Small delay between windows to avoid rapid thrash.
					time.sleep(self.delay_s)
			except Exception as e:
				results.append({
					"hwnd": int(getattr(w, "hwnd", 0) or 0),
					"window_title": getattr(w, "title", ""),
					"error": str(e),
				})

		summary = {
			"windows_scanned": len(ws),
			"actions_taken": actions,
			"results": results,
			"timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
		}
		self._log_event("vscode_multi_keepalive_cycle", **summary)
		return summary

	def run_loop(self, interval_s: float = 5.0, max_cycles: Optional[int] = None) -> None:
		"""Optional helper: background-style loop for keepalive.

		The caller is responsible for choosing safe lifecycle integration.
		This method does not spawn threads; it is a simple blocking loop.
		"""
		cycles = 0
		while True:
			self.cycle_once()
			cycles += 1
			if max_cycles is not None and cycles >= max_cycles:
				break
			time.sleep(max(0.1, float(interval_s)))
