from __future__ import annotations

import re
from pathlib import Path


_IMPORT_RE = re.compile(r"^\s*(from|import)\s+vscode_automation\b", re.MULTILINE)


def _iter_python_files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return [p for p in root.rglob("*.py") if p.is_file()]


def test_no_direct_vscode_automation_imports_outside_shims() -> None:
    """Prevent drift: non-shim code must import via src.vscode_automation.

    Allowed locations:
    - top-level package: vscode_automation/** (authoritative implementation)
    - compatibility shims: src/vscode_automation/**

    Enforced locations:
    - src/** (except src/vscode_automation/**)
    - Scripts/**
    """

    repo_root = Path(__file__).resolve().parents[1]
    scan_roots = [repo_root / "src", repo_root / "Scripts"]

    offenders: list[tuple[Path, int, str]] = []

    for scan_root in scan_roots:
        for path in _iter_python_files(scan_root):
            # Compatibility shims are allowed to import the canonical package.
            try:
                rel = path.relative_to(repo_root)
            except ValueError:
                rel = path

            if rel.parts[:2] == ("src", "vscode_automation"):
                continue

            text = path.read_text(encoding="utf-8", errors="replace")
            for match in _IMPORT_RE.finditer(text):
                line_no = text.count("\n", 0, match.start()) + 1
                line = text.splitlines()[line_no - 1].rstrip("\n")
                offenders.append((rel, line_no, line.strip()))

    if offenders:
        formatted = "\n".join(
            f"- {p.as_posix()}:{line_no}: {line}" for p, line_no, line in offenders
        )
        raise AssertionError(
            "Direct imports from 'vscode_automation' are forbidden outside shims. "
            "Import via 'src.vscode_automation' instead.\n" + formatted
        )
