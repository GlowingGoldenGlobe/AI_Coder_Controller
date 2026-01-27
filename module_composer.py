import os
import datetime
import fnmatch
import json
import ast
from typing import List, Optional, Tuple, Set


DEFAULT_IGNORE_DIRS: Set[str] = {
    ".git",
    ".hg",
    ".svn",
    "__pycache__",
    ".venv",
    "venv",
    "Include",
    "Lib",
    "node_modules",
}

DEFAULT_IGNORE_EXTS: Set[str] = {
    ".pyc",
    ".pyo",
    ".pyd",
    ".so",
    ".dll",
    ".exe",
}

TEXT_EXTS: Set[str] = {
    ".py",
    ".md",
    ".txt",
    ".json",
    ".ps1",
    ".bat",
    ".cfg",
    ".ini",
    ".yml",
    ".yaml",
}


MODULES_JSON_INCLUDE_GLOBS: List[str] = [
    "module_composer.py",
    "README*",
    "Instructions*.md",
    "requirements*.txt",
    "pyvenv.cfg",
    "module_*.py",
    "src/**",
    "Scripts/**",
    "config/**",
    "docs/**",
    "projects/**",
]

EXTRA_IGNORED_DIRS_FOR_JSON: Set[str] = {
    "Copilot_Attachments",
    "Archive_old_files",
    "Archive_OCR_Images_Assessments",
    "logs",
    "recordings",
}


def _first_line(text: str) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    return text.splitlines()[0].strip()


def _to_posix_relpath(path: str, root: str) -> str:
    return os.path.relpath(path, root).replace(os.sep, "/")


def _matches_any(rel_posix: str, basename: str, patterns: List[str]) -> bool:
    if not patterns:
        return True
    for pat in patterns:
        if fnmatch.fnmatch(basename, pat):
            return True
        if fnmatch.fnmatch(rel_posix, pat):
            return True
    return False


def _walk_files(
    root: str,
    include_globs: Optional[List[str]] = None,
    ignore_dirs: Optional[Set[str]] = None,
    ignore_exts: Optional[Set[str]] = None,
) -> List[Tuple[str, int, float]]:
    root = os.path.abspath(root)
    include_globs = include_globs or []
    ignore_dirs = DEFAULT_IGNORE_DIRS if ignore_dirs is None else ignore_dirs
    ignore_exts = DEFAULT_IGNORE_EXTS if ignore_exts is None else ignore_exts

    entries: List[Tuple[str, int, float]] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in ignore_dirs]
        for fname in filenames:
            _, ext = os.path.splitext(fname)
            if ext in ignore_exts:
                continue
            fpath = os.path.join(dirpath, fname)
            rel_posix = _to_posix_relpath(fpath, root)
            if include_globs and not _matches_any(rel_posix, fname, include_globs):
                continue
            try:
                stat = os.stat(fpath)
            except OSError:
                continue
            entries.append((fpath, stat.st_size, stat.st_mtime))

    entries.sort(key=lambda t: _to_posix_relpath(t[0], root).lower())
    return entries


def _is_probably_binary(path: str) -> bool:
    try:
        with open(path, "rb") as f:
            head = f.read(2048)
        return b"\x00" in head
    except OSError:
        return False


def _read_text_or_placeholder(path: str, max_chars: int = 200_000) -> str:
    try:
        if _is_probably_binary(path):
            try:
                size = os.path.getsize(path)
            except OSError:
                size = None
            if size is None:
                return "<binary file omitted>\n"
            return f"<binary file omitted; size={size} bytes>\n"
    except Exception:
        return "<unreadable file>\n"

    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read(max_chars + 1)
        if len(text) > max_chars:
            return text[:max_chars] + "\n<...truncated...>\n"
        return text
    except (OSError, UnicodeDecodeError):
        return "<unreadable file>\n"


def compose_modules_index_json(
    output_path: str,
    root: str,
    include_globs: Optional[List[str]] = None,
    max_source_bytes: int = 2_000_000,
    per_doc_max_chars: int = 60_000,
    total_doc_budget_chars: int = 260_000,
) -> None:
    """Write a single JSON file containing:

    - Included file list (paths + size/mtime)
    - Embedded README/config/docs text (bounded)
    - Python module inventory (module docstring + top-level function docstrings)
    """

    root = os.path.abspath(root)
    include_globs = include_globs or MODULES_JSON_INCLUDE_GLOBS
    ignore_dirs = set(DEFAULT_IGNORE_DIRS) | set(EXTRA_IGNORED_DIRS_FOR_JSON)

    files = _walk_files(root, include_globs=include_globs, ignore_dirs=ignore_dirs)
    generated = datetime.datetime.now().isoformat(timespec="seconds")

    included_files = [
        {
            "path": _to_posix_relpath(fpath, root),
            "size_bytes": size,
            "mtime": datetime.datetime.fromtimestamp(mtime).isoformat(timespec="seconds"),
        }
        for (fpath, size, mtime) in files
    ]

    # Python module inventory
    modules_out: List[dict] = []
    for fpath, size, _mtime in files:
        if os.path.splitext(fpath)[1].lower() != ".py":
            continue

        rel_posix = _to_posix_relpath(fpath, root)
        name = os.path.splitext(os.path.basename(fpath))[0]

        if size > max_source_bytes:
            modules_out.append(
                {
                    "name": name,
                    "path": rel_posix,
                    "summary": f"<skipped parse; file too large: {size} bytes>",
                    "functions": [],
                }
            )
            continue

        try:
            with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                source = f.read()
            tree = ast.parse(source, filename=rel_posix)
            summary = _first_line(ast.get_docstring(tree) or "")
            functions = []
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    functions.append(
                        {
                            "name": node.name,
                            "purpose": _first_line(ast.get_docstring(node) or ""),
                        }
                    )
        except Exception as e:
            summary = f"<parse error: {type(e).__name__}: {e}>"
            functions = []

        modules_out.append(
            {
                "name": name,
                "path": rel_posix,
                "summary": summary,
                "functions": functions,
            }
        )

    modules_out.sort(key=lambda m: (m.get("path") or "").lower())

    # Embedded docs/config (bounded)
    def _doc_priority(rel_posix: str) -> int:
        rel_l = rel_posix.lower()
        base_l = os.path.basename(rel_posix).lower()
        if base_l.startswith("readme"):
            return 0
        if rel_l.startswith("config/"):
            return 1
        if rel_l.startswith("docs/"):
            return 2
        if base_l.startswith("instructions"):
            return 3
        if base_l.startswith("requirements") or base_l == "pyvenv.cfg":
            return 4
        if rel_l.startswith("projects/"):
            return 5
        return 9

    candidates = []
    for fpath, size, _mtime in files:
        ext = os.path.splitext(fpath)[1].lower()
        if ext == ".py":
            continue
        if ext and ext not in TEXT_EXTS:
            continue
        rel_posix = _to_posix_relpath(fpath, root)
        candidates.append((rel_posix, fpath, size))

    candidates.sort(key=lambda t: (_doc_priority(t[0]), t[0].lower()))

    docs_out: List[dict] = []
    budget_left = total_doc_budget_chars
    for rel_posix, fpath, size in candidates:
        if budget_left <= 0:
            break
        content = _read_text_or_placeholder(fpath, max_chars=per_doc_max_chars)
        if len(content) > budget_left:
            content = content[: max(0, budget_left)] + "\n<...truncated (budget)...>\n"
        docs_out.append(
            {
                "path": rel_posix,
                "size_bytes": size,
                "summary": _first_line(content),
                "content": content,
            }
        )
        budget_left -= len(content)

    payload = {
        "root": root,
        "generated": generated,
        "include_globs": include_globs,
        "included_files": included_files,
        "documents": docs_out,
        "modules": modules_out,
    }

    with open(output_path, "w", encoding="utf-8") as out:
        json.dump(payload, out, ensure_ascii=False, indent=2)
        out.write("\n")


def main() -> int:
    import sys

    args = sys.argv[1:]
    if len(args) < 2:
        print("Usage: python module_composer.py [output_path] [directory] [mode: modules_json|json]")
        return 1

    output_path, directory = args[0], args[1]
    mode = args[2] if len(args) >= 3 else "modules_json"
    if mode not in {"modules_json", "json"}:
        print("Unknown mode. Use 'modules_json' (or 'json').")
        return 1

    compose_modules_index_json(output_path, directory)
    print(f"Wrote modules JSON index: {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())