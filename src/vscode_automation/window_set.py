from __future__ import annotations

from dataclasses import dataclass
from typing import List, Dict, Optional

from src.windows import WindowsManager


@dataclass
class VSCodeWindow:
    hwnd: int
    title: str
    cls: str
    process: str
    process_path: str


class VSCodeWindowSet:
    """Discover and manage all visible VS Code windows.

    This is intentionally read-only: it never modifies windows itself,
    only provides filtered views over WindowsManager state for higher-level
    controllers to act on (focus, click, etc.).
    """

    def __init__(self, winman: Optional[WindowsManager] = None):
        self.winman = winman or WindowsManager()

    def list_vscode_windows(self) -> List[VSCodeWindow]:
        """Return all visible VS Code windows (Code.exe / Visual Studio Code).

        Uses a combination of title and process name heuristics to stay robust
        across builds while avoiding non-Code foregrounds.
        """
        out: List[VSCodeWindow] = []
        raw = self.winman.list_windows(include_empty_titles=False)
        for w in raw:
            try:
                hwnd = int(w.get("hwnd") or 0)
            except Exception:
                continue
            if not hwnd:
                continue
            info = self.winman.get_window_info(hwnd)
            title = (info.get("title") or w.get("title") or "").strip()
            cls = (info.get("class") or w.get("class") or "").strip()
            proc = (info.get("process") or "").strip()
            path = (info.get("process_path") or "").strip()
            low_title = title.lower()
            is_vscode_title = "visual studio code" in low_title or low_title.endswith(" - visual studio code")
            is_vscode_proc = proc.lower().startswith("code") if proc else False
            if not (is_vscode_title or is_vscode_proc):
                continue
            out.append(VSCodeWindow(hwnd=hwnd, title=title, cls=cls, process=proc, process_path=path))
        return out

    def first_vscode_window(self) -> Optional[VSCodeWindow]:
        ws = self.list_vscode_windows()
        return ws[0] if ws else None

    def focus_window(self, win: VSCodeWindow) -> bool:
        """Bring a VS Code window to foreground."""
        try:
            return bool(self.winman.focus_hwnd(int(win.hwnd)))
        except Exception:
            return False

    def focus_all_round_robin(self) -> List[Dict[str, object]]:
        """Focus each VS Code window once, in discovery order.

        Returns a lightweight per-window result summary; higher-level
        controllers can use this to drive OCR/keepalive passes.
        """
        results: List[Dict[str, object]] = []
        for w in self.list_vscode_windows():
            ok = self.focus_window(w)
            results.append({
                "hwnd": w.hwnd,
                "title": w.title,
                "focused": bool(ok),
            })
        return results
