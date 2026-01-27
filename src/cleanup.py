from __future__ import annotations
import fnmatch
import os
import time
from pathlib import Path
from typing import Iterable, List, Optional


class FileCleaner:
    def __init__(
        self,
        base: Path,
        dirs: Iterable[str] | None = None,
        patterns: Iterable[str] | None = None,
        retain_seconds: int = 30,
        logger=None,
        rules: Optional[Iterable[dict]] = None,
    ):
        self.base = Path(base)
        self.dirs = [str(d) for d in (dirs or [])]
        self.patterns = list(patterns or ["*.png", "*.jpg"])
        self.retain = max(1, int(retain_seconds))
        self.log = logger
        # Advanced per-rule configuration: [{dir, patterns, retain_seconds}]
        self.rules = list(rules) if rules else None

    def _log(self, msg: str, **kw):
        try:
            if self.log:
                if hasattr(self.log, "log"):
                    self.log.log("cleanup", message=msg, **kw)
                else:
                    self.log(f"cleanup: {msg} | {kw}")
        except Exception:
            pass

    def _has_marker(self, p: Path, marker_ext: str) -> bool:
        try:
            m = Path(str(p) + marker_ext)
            return m.exists()
        except Exception:
            return False

    def _should_delete(self, p: Path, now: float, retain: int, patterns: Iterable[str], require_marker: bool = False, marker_ext: str = ".assessed") -> bool:
        try:
            if not p.exists() or not p.is_file():
                return False
            age = now - p.stat().st_mtime
            if age < retain:
                return False
            name = p.name
            if not any(fnmatch.fnmatch(name, pat) for pat in patterns):
                return False
            if require_marker and (not self._has_marker(p, marker_ext)):
                return False
            return True
        except Exception:
            return False

    def clean_once(self) -> dict:
        now = time.time()
        deleted: List[str] = []
        scanned = 0
        rule_list = self.rules if self.rules is not None else [
            {"dir": rel, "patterns": self.patterns, "retain_seconds": self.retain} for rel in self.dirs
        ]
        for rule in rule_list:
            try:
                rel = str(rule.get("dir"))
                pats = list(rule.get("patterns", self.patterns))
                retain = max(1, int(rule.get("retain_seconds", self.retain)))
                max_keep = rule.get("max_keep")
                require_marker = bool(rule.get("require_marker", False))
                marker_ext = str(rule.get("marker_extension", ".assessed"))
                d = (self.base / rel).resolve()
                if not d.exists():
                    continue
                # First pass: age-based deletion
                for root, _dirs, files in os.walk(d):
                    for f in files:
                        scanned += 1
                        p = Path(root) / f
                        if self._should_delete(p, now, retain, pats, require_marker=require_marker, marker_ext=marker_ext):
                            try:
                                p.unlink(missing_ok=True)
                                deleted.append(str(p))
                                # If rule requires marker, also delete the marker next to the file
                                if require_marker:
                                    try:
                                        mp = Path(str(p) + marker_ext)
                                        if mp.exists():
                                            mp.unlink(missing_ok=True)
                                            deleted.append(str(mp))
                                    except Exception:
                                        pass
                            except Exception:
                                pass
                # Second pass: enforce max_keep if configured
                try:
                    if isinstance(max_keep, int) and max_keep > 0:
                        # Collect all matching files (recursive) with mtimes
                        matches: List[tuple[float, Path]] = []
                        for root, _dirs, files in os.walk(d):
                            for f in files:
                                p = Path(root) / f
                                name = p.name
                                if any(fnmatch.fnmatch(name, pat) for pat in pats):
                                    try:
                                        mt = p.stat().st_mtime
                                        matches.append((mt, p))
                                    except Exception:
                                        pass
                        # Sort newest first; delete older beyond cap
                        matches.sort(key=lambda x: x[0], reverse=True)
                        for _mt, p in matches[max_keep:]:
                            try:
                                p.unlink(missing_ok=True)
                                deleted.append(str(p))
                            except Exception:
                                pass
                except Exception:
                    pass
            except Exception:
                continue
        self._log("clean_once", scanned=scanned, deleted=len(deleted))
        return {"scanned": scanned, "deleted": deleted}
