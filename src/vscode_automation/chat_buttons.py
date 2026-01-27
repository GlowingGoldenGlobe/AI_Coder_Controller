from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from mss import mss

from src.control import Controller
from src.ocr import CopilotOCR
from src.windows import WindowsManager
from src.jsonlog import JsonActionLogger


class ChatButtonAnalyzer:
    """Image/ROI-driven helper for clicking chat-related buttons in VS Code.

    Responsibilities:
    - Capture the configured Copilot chat ROI (per ocr.json / CopilotOCR).
    - Detect button-like rectangles inside that region.
    - Map detected element coordinates back to absolute screen space.
    - Move the mouse over a chosen button and click it.

    This class is deliberately conservative: it never sends keyboard input
    and delegates all foreground gating to WindowsManager and the caller.
    """

    DEFAULT_ACTION_HINTS: Sequence[str] = (
        "continue generating",
        "continue",
        "send",
        "submit",
        "apply",
        "accept",
        "ok",
    )

    def __init__(
        self,
        ocr: CopilotOCR,
        ctrl: Controller,
        winman: Optional[WindowsManager] = None,
        log: Optional[JsonActionLogger] = None,
        delay_ms: int = 300,
    ) -> None:
        self.ocr = ocr
        self.ctrl = ctrl
        self.winman = winman or WindowsManager()
        root = Path(__file__).resolve().parent.parent
        self.log = log or JsonActionLogger(root / "logs" / "actions" / "vscode_chat_buttons.jsonl")
        self.delay_s = max(0, int(delay_ms)) / 1000.0

    # --- ROI helpers -------------------------------------------------

    def _current_roi_bbox_screen(self) -> Optional[Dict[str, int]]:
        """Return the absolute screen bbox for the OCR ROI.

        Uses CopilotOCR.region_percent + monitor_index to reconstruct the
        same rectangle used by capture_chat_text/capture_image.
        """
        try:
            region = getattr(self.ocr, "region_percent", None) or {}
            lp = float(region.get("left", 65)) / 100.0
            tp = float(region.get("top", 0)) / 100.0
            wp = float(region.get("width", 35)) / 100.0
            hp = float(region.get("height", 100)) / 100.0
            with mss() as sct:
                mon = sct.monitors[int(getattr(self.ocr, "monitor_index", 1))]
                sw, sh = int(mon["width"]), int(mon["height"])
                left = int(sw * lp)
                top = int(sh * tp)
                width = max(1, int(sw * wp))
                height = max(1, int(sh * hp))
                return {
                    "left": int(mon["left"]) + left,
                    "top": int(mon["top"]) + top,
                    "width": width,
                    "height": height,
                }
        except Exception:
            return None

    def _set_alt_region(self, target_key: str) -> Optional[Dict[str, Any]]:
        """Best-effort temporary swap of OCR.region_percent from cfg.targets.

        Returns the original region dict if a swap occurred, else None.
        """
        try:
            cfg = getattr(self.ocr, "cfg", {}) or {}
            targets = cfg.get("targets") or {}
            alt = targets.get(target_key)
            if not isinstance(alt, dict):
                return None
            orig = getattr(self.ocr, "region_percent", None)
            setattr(self.ocr, "region_percent", alt)
            return {"orig": orig}
        except Exception:
            return None

    def _restore_alt_region(self, token: Optional[Dict[str, Any]]) -> None:
        if not token:
            return
        try:
            orig = token.get("orig", None)
            if orig is not None:
                setattr(self.ocr, "region_percent", orig)
        except Exception:
            pass

    # --- Core operations ---------------------------------------------

    def _capture_chat_for_window(self, hwnd: int, target_key: str = "vscode_chat") -> Dict[str, Any]:
        """Focus a VS Code window, then capture chat ROI via OCR.

        Returns the underlying CopilotOCR result plus an attached "roi" bbox.
        """
        focused = False
        try:
            focused = bool(self.winman.focus_hwnd(int(hwnd)))
        except Exception:
            focused = False
        time.sleep(self.delay_s)

        swap = self._set_alt_region(target_key)
        try:
            root = Path(__file__).resolve().parent.parent
            debug_dir = root / "logs" / "ocr"
            res = self.ocr.capture_chat_text(save_dir=debug_dir)
        finally:
            self._restore_alt_region(swap)

        roi = self._current_roi_bbox_screen() or {"left": 0, "top": 0, "width": 0, "height": 0}
        out = dict(res or {})
        out["roi"] = roi
        out["focused"] = focused
        return out

    def _pick_primary_button(self, elements: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not elements:
            return None
        # Prefer larger, more prominent buttons while avoiding oversize overlays.
        # elements already contain a "score"; sort by it descending.
        filtered: List[Dict[str, Any]] = []
        for e in elements:
            bbox = e.get("bbox") or {}
            w = int(bbox.get("width") or 0)
            h = int(bbox.get("height") or 0)
            if w < 20 or h < 12:
                continue
            filtered.append(e)
        if not filtered:
            filtered = elements
        filtered.sort(key=lambda r: float(r.get("score", 0.0)), reverse=True)
        return filtered[0] if filtered else None

    def _needs_action(self, text: str, hints: Optional[Sequence[str]]) -> bool:
        if not hints:
            return True
        low = (text or "").lower()
        return any(h in low for h in hints if h)

    def click_primary_chat_button(
        self,
        hwnd: int,
        target_key: str = "vscode_chat",
        action_hints: Optional[Sequence[str]] = None,
    ) -> Dict[str, Any]:
        """Best-effort: click a primary button in the chat ROI for a window.

        - Focuses the given window.
        - Captures OCR/element info for the chat region.
        - If OCR text suggests an action is needed (per hints), chooses a
          prominent button and moves/clicks the mouse at its center.

        Returns a structured dict with observation and action details.
        """
        hints = action_hints or self.DEFAULT_ACTION_HINTS
        obs = self._capture_chat_for_window(hwnd, target_key=target_key)
        text = str(obs.get("text") or "")
        elements = list(obs.get("elements") or [])
        roi = obs.get("roi") or {"left": 0, "top": 0}

        needs = self._needs_action(text, hints)
        primary = self._pick_primary_button(elements)
        clicked = False
        click_x = None
        click_y = None

        if needs and primary is not None:
            bbox = primary.get("bbox") or {}
            try:
                ex = int(bbox.get("left", 0)) + int(bbox.get("width", 0)) // 2
                ey = int(bbox.get("top", 0)) + int(bbox.get("height", 0)) // 2
                rx = int(roi.get("left", 0)) + ex
                ry = int(roi.get("top", 0)) + ey
                moved = self.ctrl.move_mouse(rx, ry)
                time.sleep(max(self.delay_s / 2.0, 0.1))
                if moved:
                    clicked = self.ctrl.click_at(rx, ry)
                    click_x, click_y = rx, ry
            except Exception:
                clicked = False

        record = {
            "event": "vscode_chat_click_primary_button",
            "hwnd": int(hwnd),
            "focused": bool(obs.get("focused")),
            "needs_action": bool(needs),
            "clicked": bool(clicked),
            "click_x": click_x,
            "click_y": click_y,
            "text_preview": text[:160],
            "elements_count": len(elements),
        }
        try:
            self.log.log("vscode_chat_buttons", **record)
        except Exception:
            pass
        return record
