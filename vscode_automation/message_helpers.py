from __future__ import annotations

from typing import Any, Dict, Iterable, Optional

from .config import OrchestratorOptions


def select_template(options: OrchestratorOptions) -> Optional[str]:
	"""Pick a message template text based on configured strategy.

	Currently supports:
	- "first" (default): first non-empty template.
	- "cycle": rotate through templates on each call using an in-memory index.

	This stays stateless with respect to objectives; it only uses JSON config
	and can be swapped out or extended later.
	"""
	tmpls: Iterable[Dict[str, Any]] = options.message.default_templates
	texts = [str(t.get("text") or "").strip() for t in tmpls]
	texts = [t for t in texts if t]
	if not texts:
		return None
	strategy = (options.message_strategy or "first").lower()
	if strategy == "cycle":
		# Simple, per-process rotation index.
		idx = getattr(select_template, "_idx", 0)
		val = texts[idx % len(texts)]
		setattr(select_template, "_idx", idx + 1)
		return val
	# Fallback: first non-empty.
	return texts[0]
