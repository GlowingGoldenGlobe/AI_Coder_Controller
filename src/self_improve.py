from __future__ import annotations
import os
import datetime
from pathlib import Path
from typing import List, Tuple

# We rely on module_composer in workspace root
try:
    from module_composer import _walk_files  # type: ignore
except Exception:
    _walk_files = None  # type: ignore


def list_python_modules(src_dir: Path) -> List[Path]:
    return sorted([p for p in src_dir.glob("*.py") if p.is_file()])


def extract_functions(py_file: Path) -> List[str]:
    funcs: List[str] = []
    try:
        for line in py_file.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if s.startswith("def ") and s.endswith(":"):
                name = s[4:].split("(")[0].strip()
                if name:
                    funcs.append(name)
    except Exception:
        pass
    return funcs


def build_metadata_text(root: Path) -> str:
    now = datetime.datetime.now().isoformat(timespec="seconds")
    src_dir = root / "src"
    lines: List[str] = []
    lines.append(f"[# Metadata Summary]")
    lines.append(f"Root: {root}")
    lines.append(f"Generated: {now}")
    lines.append("")
    lines.append("[# Modules and Functions]")
    for mod in list_python_modules(src_dir):
        funcs = extract_functions(mod)
        rel = mod.relative_to(root)
        lines.append(f"- {rel}:")
        if funcs:
            for fn in funcs:
                lines.append(f"  - {fn}()")
        else:
            lines.append("  - <no functions found>")
    lines.append("")
    lines.append("[# Filesystem (focused)]")
    try:
        if _walk_files:
            files: List[Tuple[str, int, float]] = _walk_files(str(root))
            # Keep only a limited count for readability
            for fpath, size, _mtime in files[:200]:
                rel = os.path.relpath(fpath, str(root))
                lines.append(f"- {rel} | {size} bytes")
        else:
            lines.append("<module_composer not available>")
    except Exception as e:
        lines.append(f"<filesystem listing failed: {e}>")
    lines.append("")
    return "\n".join(lines) + "\n"


def write_metadata_file(root: Path) -> Path:
    out = root / "projects" / "Self-Improve" / "metadata.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    text = build_metadata_text(root)
    out.write_text(text, encoding="utf-8")
    return out
