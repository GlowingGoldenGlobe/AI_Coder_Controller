from __future__ import annotations

import ctypes
import os
import time
from ctypes import wintypes
from typing import Callable, List, Optional, Dict

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def _get_window_text(hwnd: int) -> str:
    length = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value or ""


def _get_class_name(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value or ""


def _is_window_visible(hwnd: int) -> bool:
    return bool(user32.IsWindowVisible(hwnd))


def _get_window_pid(hwnd: int) -> int:
    try:
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid))
        return int(pid.value)
    except Exception:
        return 0


def _get_process_path(pid: int) -> str:
    """Best-effort process image path; returns '' on failure."""
    try:
        if not pid:
            return ""
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, wintypes.DWORD(pid))
        if not handle:
            return ""
        try:
            # QueryFullProcessImageNameW
            buf_len = wintypes.DWORD(4096)
            buf = ctypes.create_unicode_buffer(buf_len.value)
            if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(buf_len)):
                return buf.value or ""
            # Fallback: psapi.GetProcessImageFileNameW (often returns a device path)
            try:
                psapi = ctypes.windll.psapi
                psapi.GetProcessImageFileNameW.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD]
                psapi.GetProcessImageFileNameW.restype = wintypes.DWORD
                buf2 = ctypes.create_unicode_buffer(4096)
                n = psapi.GetProcessImageFileNameW(handle, buf2, wintypes.DWORD(4096))
                if n:
                    return buf2.value or ""
            except Exception:
                pass
            return ""
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return ""


def _enum_windows(callback: Callable[[int], None]) -> None:
    def _cb(hwnd, lparam):
        try:
            callback(hwnd)
        except Exception:
            return True
        return True

    user32.EnumWindows(EnumWindowsProc(_cb), 0)


class WindowsManager:
    """Minimal window focusing helper using Win32 APIs via ctypes.

    - Enumerates top-level windows
    - Finds by title substring (case-insensitive) and/or class name
    - Attempts to bring to foreground reliably using AttachThreadInput hack
    """

    SW_RESTORE = 9
    SW_MAXIMIZE = 3

    def __init__(self) -> None:
        self._maximized_at: Dict[int, float] = {}

    def list_windows(self, include_empty_titles: bool = False) -> List[Dict[str, str]]:
        out: List[Dict[str, str]] = []
        def _collect(hwnd: int):
            if not _is_window_visible(hwnd):
                return
            title = _get_window_text(hwnd)
            if (not include_empty_titles) and (not title):
                return
            cls = _get_class_name(hwnd)
            out.append({"hwnd": str(hwnd), "title": title, "class": cls})
        _enum_windows(_collect)
        return out

    def find_first(self, title_contains: Optional[str] = None, class_contains: Optional[str] = None) -> Optional[int]:
        tsub = (title_contains or "").lower()
        csub = (class_contains or "").lower()
        found: Optional[int] = None
        def _check(hwnd: int):
            nonlocal found
            if found is not None:
                return
            if not _is_window_visible(hwnd):
                return
            title = _get_window_text(hwnd)
            cls = _get_class_name(hwnd)
            if tsub and tsub not in title.lower():
                return
            if class_contains and csub not in cls.lower():
                return
            found = hwnd
        _enum_windows(_check)
        return found

    def find_first_any(
        self,
        title_contains: Optional[str] = None,
        class_contains: Optional[str] = None,
        process_contains: Optional[str] = None,
    ) -> Optional[int]:
        """Find first visible top-level window matching optional title/class/process substrings."""
        tsub = (title_contains or "").lower()
        csub = (class_contains or "").lower()
        psub = (process_contains or "").lower()
        found: Optional[int] = None

        def _check(hwnd: int):
            nonlocal found
            if found is not None:
                return
            if not _is_window_visible(hwnd):
                return
            title = _get_window_text(hwnd)
            cls = _get_class_name(hwnd)
            if tsub and tsub not in (title or "").lower():
                return
            if csub and csub not in (cls or "").lower():
                return
            if psub:
                pid = _get_window_pid(hwnd)
                path = _get_process_path(pid)
                name = os.path.basename(path).lower() if path else ""
                if psub not in name:
                    return
            found = hwnd

        _enum_windows(_check)
        return found

    def focus_hwnd(self, hwnd: int) -> bool:
        if not hwnd:
            return False
        # Restore if minimized
        user32.ShowWindowAsync(hwnd, self.SW_RESTORE)
        now = time.time()
        last = self._maximized_at.get(hwnd)
        if last is None or (now - last) > 5.0:
            try:
                user32.ShowWindowAsync(hwnd, self.SW_MAXIMIZE)
            except Exception:
                pass
            self._maximized_at[hwnd] = now

        # Attach thread input trick to allow SetForegroundWindow
        fg = user32.GetForegroundWindow()
        if fg == hwnd:
            return True
        pid = wintypes.DWORD()
        tid1 = user32.GetWindowThreadProcessId(fg, ctypes.byref(pid))
        tid2 = user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        attached = False
        if tid1 != tid2 and tid1 and tid2:
            attached = bool(user32.AttachThreadInput(tid1, tid2, True))
        try:
            user32.SetForegroundWindow(hwnd)
            user32.BringWindowToTop(hwnd)
        finally:
            if attached:
                user32.AttachThreadInput(tid1, tid2, False)
        return True

    def get_foreground(self) -> Optional[int]:
        try:
            hwnd = user32.GetForegroundWindow()
            return int(hwnd) if hwnd else None
        except Exception:
            return None

    def get_window_info(self, hwnd: int) -> Dict[str, str]:
        try:
            title = _get_window_text(hwnd)
            cls = _get_class_name(hwnd)
            pid = _get_window_pid(hwnd)
            path = _get_process_path(pid)
            name = os.path.basename(path) if path else ""
            return {
                "hwnd": str(hwnd),
                "title": title or "",
                "class": cls or "",
                "pid": str(pid or 0),
                "process": name or "",
                "process_path": path or "",
            }
        except Exception:
            return {"hwnd": str(hwnd or 0), "title": "", "class": "", "pid": "0", "process": "", "process_path": ""}

    def get_window_process_name(self, hwnd: int) -> str:
        try:
            pid = _get_window_pid(hwnd)
            path = _get_process_path(pid)
            return os.path.basename(path) if path else ""
        except Exception:
            return ""

    def get_window_rect(self, hwnd: int) -> Optional[Dict[str, int]]:
        try:
            rect = wintypes.RECT()
            if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                return None
            left, top, right, bottom = rect.left, rect.top, rect.right, rect.bottom
            return {
                "left": int(left),
                "top": int(top),
                "right": int(right),
                "bottom": int(bottom),
                "width": int(max(0, right - left)),
                "height": int(max(0, bottom - top)),
            }
        except Exception:
            return None

    def close_hwnd(self, hwnd: int) -> bool:
        """Request a window to close via WM_CLOSE."""
        try:
            WM_CLOSE = 0x0010
            # PostMessage to avoid potential hangs
            return bool(user32.PostMessageW(hwnd, WM_CLOSE, 0, 0))
        except Exception:
            return False

    def get_clipboard_text(self, timeout_s: float = 0.6) -> str:
        """Best-effort read of current Unicode text from the clipboard.

        Returns '' if clipboard is unavailable or does not contain text.
        """
        try:
            return _clipboard_get_text(timeout_s=timeout_s)
        except Exception:
            return ""

    def set_clipboard_text(self, text: str, timeout_s: float = 0.6) -> bool:
        """Best-effort set Unicode text to the clipboard."""
        try:
            return bool(_clipboard_set_unicode_text(str(text or ""), timeout_s=timeout_s))
        except Exception:
            return False

    def send_input_keys(self, keys: List[str]) -> bool:
        """Send a key press (or hotkey chord) via Win32 SendInput.

        Notes:
        - This sends input to the *foreground* window (like physical keyboard input).
        - Use together with strict foreground gating.
        """
        try:
            return bool(_send_input_hotkey(keys))
        except Exception:
            return False


def _clipboard_get_unicode_text(timeout_s: float = 0.6) -> str:
    CF_UNICODETEXT = 13
    start = ctypes.c_double(ctypes.windll.kernel32.GetTickCount64() / 1000.0)

    # Functions
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = wintypes.BOOL
    user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
    user32.IsClipboardFormatAvailable.restype = wintypes.BOOL
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    user32.GetClipboardData.restype = wintypes.HANDLE

    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = wintypes.LPVOID
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalUnlock.restype = wintypes.BOOL

    def _now_s() -> float:
        return float(ctypes.windll.kernel32.GetTickCount64()) / 1000.0

    deadline = float(start.value) + max(0.05, float(timeout_s))
    while _now_s() <= deadline:
        if user32.OpenClipboard(None):
            try:
                if not user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
                    return ""
                handle = user32.GetClipboardData(CF_UNICODETEXT)
                if not handle:
                    return ""
                locked = kernel32.GlobalLock(handle)
                if not locked:
                    return ""
                try:
                    # Treat locked pointer as wide-char string.
                    text = ctypes.wstring_at(locked)
                    return text or ""
                finally:
                    kernel32.GlobalUnlock(handle)
            finally:
                user32.CloseClipboard()
        # Clipboard can be briefly locked by other apps.
        ctypes.windll.kernel32.Sleep(40)
    return ""


def _clipboard_get_text(timeout_s: float = 0.6) -> str:
    """Read text from clipboard.

    Tries CF_UNICODETEXT first, then falls back to CF_TEXT.
    Returns '' if clipboard is unavailable or does not contain text.
    """
    text = _clipboard_get_unicode_text(timeout_s=timeout_s)
    if text:
        return text

    CF_TEXT = 1
    start = ctypes.c_double(ctypes.windll.kernel32.GetTickCount64() / 1000.0)

    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = wintypes.BOOL
    user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
    user32.IsClipboardFormatAvailable.restype = wintypes.BOOL
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    user32.GetClipboardData.restype = wintypes.HANDLE

    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = wintypes.LPVOID
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalUnlock.restype = wintypes.BOOL

    def _now_s() -> float:
        return float(ctypes.windll.kernel32.GetTickCount64()) / 1000.0

    deadline = float(start.value) + max(0.05, float(timeout_s))
    while _now_s() <= deadline:
        if user32.OpenClipboard(None):
            try:
                if not user32.IsClipboardFormatAvailable(CF_TEXT):
                    return ""
                handle = user32.GetClipboardData(CF_TEXT)
                if not handle:
                    return ""
                locked = kernel32.GlobalLock(handle)
                if not locked:
                    return ""
                try:
                    # CF_TEXT is ANSI bytes null-terminated.
                    raw = ctypes.string_at(locked)
                    try:
                        return raw.decode("mbcs", errors="replace") or ""
                    except Exception:
                        return ""
                finally:
                    kernel32.GlobalUnlock(handle)
            finally:
                user32.CloseClipboard()
        ctypes.windll.kernel32.Sleep(40)
    return ""


def _clipboard_set_unicode_text(text: str, timeout_s: float = 0.6) -> bool:
    CF_UNICODETEXT = 13
    GMEM_MOVEABLE = 0x0002

    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = wintypes.BOOL
    user32.EmptyClipboard.argtypes = []
    user32.EmptyClipboard.restype = wintypes.BOOL
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user32.SetClipboardData.restype = wintypes.HANDLE

    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = wintypes.LPVOID
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalFree.restype = wintypes.HGLOBAL

    def _now_s() -> float:
        return float(ctypes.windll.kernel32.GetTickCount64()) / 1000.0

    start = _now_s()
    deadline = start + max(0.05, float(timeout_s))
    while _now_s() <= deadline:
        if user32.OpenClipboard(None):
            hmem = None
            try:
                if not user32.EmptyClipboard():
                    return False
                # Allocate global memory for UTF-16LE text including null terminator.
                buf = (text or "") + "\x00"
                size_bytes = len(buf.encode("utf-16le"))
                hmem = kernel32.GlobalAlloc(GMEM_MOVEABLE, size_bytes)
                if not hmem:
                    return False
                locked = kernel32.GlobalLock(hmem)
                if not locked:
                    kernel32.GlobalFree(hmem)
                    hmem = None
                    return False
                try:
                    ctypes.memmove(locked, buf.encode("utf-16le"), size_bytes)
                finally:
                    kernel32.GlobalUnlock(hmem)
                if not user32.SetClipboardData(CF_UNICODETEXT, hmem):
                    kernel32.GlobalFree(hmem)
                    hmem = None
                    return False
                # Ownership transferred to the system; do not free.
                hmem = None
                return True
            finally:
                user32.CloseClipboard()
                if hmem is not None:
                    try:
                        kernel32.GlobalFree(hmem)
                    except Exception:
                        pass
        ctypes.windll.kernel32.Sleep(40)
    return False


def _send_input_hotkey(keys: List[str]) -> bool:
    """Send a hotkey chord via Win32 SendInput.

    keys: list like ["ctrl", "c"] or ["shift", "tab"] or ["tab"].
    """
    if not keys:
        return False

    # Basic VK mapping.
    vk_map = {
        "tab": 0x09,
        "enter": 0x0D,
        "return": 0x0D,
        "esc": 0x1B,
        "escape": 0x1B,
        "shift": 0x10,
        "ctrl": 0x11,
        "control": 0x11,
        "alt": 0x12,
        "down": 0x28,
        "up": 0x26,
        "left": 0x25,
        "right": 0x27,
        "pagedown": 0x22,
        "pageup": 0x21,
        "home": 0x24,
        "end": 0x23,
        "insert": 0x2D,
        "win": 0x5B,
        "winleft": 0x5B,
        "lwin": 0x5B,
    }

    def _vk_for(k: str) -> int:
        kk = (k or "").lower()
        if kk in vk_map:
            return int(vk_map[kk])
        # single character: map to virtual key via VkKeyScanW
        if len(kk) == 1:
            vk = user32.VkKeyScanW(ord(kk))
            return int(vk & 0xFF)
        return 0

    # SendInput structs
    INPUT_KEYBOARD = 1
    KEYEVENTF_KEYUP = 0x0002

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ("wVk", wintypes.WORD),
            ("wScan", wintypes.WORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", wintypes.ULONG_PTR),
        ]

    class _INPUTUNION(ctypes.Union):
        _fields_ = [("ki", KEYBDINPUT)]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("union", _INPUTUNION)]

    def _key_input(vk: int, key_up: bool) -> INPUT:
        flags = KEYEVENTF_KEYUP if key_up else 0
        ki = KEYBDINPUT(wVk=wintypes.WORD(vk), wScan=0, dwFlags=flags, time=0, dwExtraInfo=0)
        u = _INPUTUNION(ki=ki)
        return INPUT(type=INPUT_KEYBOARD, union=u)

    # Normalize keys
    norm = [str(k).lower() for k in keys if str(k).strip()]
    vks = [_vk_for(k) for k in norm]
    if any(vk == 0 for vk in vks):
        return False

    # Treat all but last as modifiers.
    mods = vks[:-1]
    main = vks[-1]

    seq: List[INPUT] = []
    for vk in mods:
        seq.append(_key_input(vk, key_up=False))
    seq.append(_key_input(main, key_up=False))
    seq.append(_key_input(main, key_up=True))
    for vk in reversed(mods):
        seq.append(_key_input(vk, key_up=True))

    arr = (INPUT * len(seq))(*seq)
    sent = user32.SendInput(len(seq), ctypes.byref(arr), ctypes.sizeof(INPUT))
    return int(sent) == len(seq)
