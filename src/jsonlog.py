from __future__ import annotations
from pathlib import Path
from typing import Any, Dict
from collections import deque
import json
import time
import threading


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()) + f".{int((time.time()%1)*1000):03d}Z"


class JsonActionLogger:
    def __init__(self, file_path: Path, error_window_s: float = 300.0):
        self.file_path = file_path
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        # Error rate tracking
        self._error_window_s = error_window_s
        self._error_counts: Dict[str, deque] = {}

    def log(self, event: str, **data: Any) -> None:
        rec: Dict[str, Any] = {
            "ts": _now_iso(),
            "event": event,
            **data,
        }
        line = json.dumps(rec, ensure_ascii=False, separators=(",", ":"))
        try:
            with self._lock:
                with open(self.file_path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
        except Exception:
            # Best-effort logging; do not raise
            pass
        
        # Track error events for rate monitoring
        if "error" in event.lower() or "fail" in event.lower() or data.get("ok") is False:
            self._record_error(event)
    
    def _record_error(self, error_type: str) -> None:
        """Record an error occurrence for rate tracking."""
        with self._lock:
            if error_type not in self._error_counts:
                self._error_counts[error_type] = deque()
            self._error_counts[error_type].append(time.time())
    
    def _prune_old_errors(self, error_type: str) -> None:
        """Remove old error timestamps outside the tracking window."""
        if error_type not in self._error_counts:
            return
        cutoff = time.time() - self._error_window_s
        q = self._error_counts[error_type]
        while q and q[0] < cutoff:
            q.popleft()
    
    def error_rate(self, error_type: str) -> int:
        """Get count of errors of this type in the current window."""
        with self._lock:
            if error_type not in self._error_counts:
                return 0
            self._prune_old_errors(error_type)
            return len(self._error_counts[error_type])
    
    def all_error_rates(self) -> Dict[str, int]:
        """Get counts of all error types in the current window."""
        with self._lock:
            result = {}
            for error_type in list(self._error_counts.keys()):
                self._prune_old_errors(error_type)
                count = len(self._error_counts.get(error_type, []))
                if count > 0:
                    result[error_type] = count
            return result
    
    def total_errors_in_window(self) -> int:
        """Get total error count across all types."""
        return sum(self.all_error_rates().values())
