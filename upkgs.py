"""upkgs.py

Project-specific package upgrade helper for AI_Coder_Controller.

This repo’s dependencies live in:
- requirements.txt (runtime)
- requirements.monitor.txt (optional monitor tooling)

Goal: make a *daily* `python -m upkgs` safe and boring:
- Always uses the current venv’s interpreter (sys.executable)
- Upgrades pip tooling first (pip/setuptools/wheel)
- Upgrades packages listed in the requirements files
- Continues past individual package failures (so one bad wheel doesn’t block the rest)

Typical usage (PowerShell, with venv activated):
- Upgrade runtime deps: `python -m upkgs`
- Include monitor deps: `python -m upkgs --monitor`
- Upgrade everything outdated (more risky): `python -m upkgs --all`
- Dry run: `python -m upkgs --dry-run`
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Sequence


DEFAULT_REQUIREMENTS = "requirements.txt"
DEFAULT_MONITOR_REQUIREMENTS = "requirements.monitor.txt"

# Packages that commonly cause confusion when listed in requirements for Windows.
# This repo uses Tk via the stdlib `tkinter`; `pip install tk` is typically unnecessary.
SKIP_REQUIREMENTS = {"tk"}


@dataclass(frozen=True)
class UpgradeResult:
    attempted: int
    succeeded: int
    failed: int


def _run(cmd: Sequence[str], *, dry_run: bool) -> int:
    printable = " ".join(cmd)
    print(f"\n$ {printable}")
    if dry_run:
        return 0
    subprocess.check_call(list(cmd))
    return 0


def _pip(*args: str) -> List[str]:
    return [sys.executable, "-m", "pip", *args]


def _read_requirement_lines(req_path: Path, *, visited: set[Path] | None = None) -> List[str]:
    """Parse a requirements file into individual requirement spec strings.

Supports:
- blank lines / comments
- recursive includes via `-r other.txt` or `--requirement other.txt`

This intentionally does *not* try to fully parse pip options; for this repo
we mostly need simple specifiers like `numpy>=1.24.0`.
"""

    if visited is None:
        visited = set()
    req_path = req_path.resolve()
    if req_path in visited:
        return []
    visited.add(req_path)

    if not req_path.exists():
        return []

    lines: List[str] = []
    raw = req_path.read_text(encoding="utf-8", errors="replace").splitlines()
    for line in raw:
        s = line.strip()
        if not s or s.startswith("#"):
            continue

        # Remove inline comments (best-effort; avoids breaking URLs with '#').
        if " #" in s:
            s = s.split(" #", 1)[0].strip()

        if s.startswith("-r ") or s.startswith("--requirement "):
            parts = s.split(maxsplit=1)
            if len(parts) == 2:
                child = (req_path.parent / parts[1].strip()).resolve()
                lines.extend(_read_requirement_lines(child, visited=visited))
            continue

        # Ignore other pip options for now.
        if s.startswith("-"):
            print(f"[upkgs] Skipping unsupported requirements option: {s}")
            continue

        lines.append(s)
    return lines


def _dedupe_keep_order(items: Iterable[str]) -> List[str]:
    seen: set[str] = set()
    out: List[str] = []
    for item in items:
        key = item.strip()
        if not key:
            continue
        # Dedupe loosely by the leading name token.
        name = key.split("==", 1)[0].split(">=", 1)[0].split("<=", 1)[0].split("~=", 1)[0].split("!=", 1)[0]
        name = name.split("[", 1)[0].strip().lower()
        if name in seen:
            continue
        seen.add(name)
        out.append(key)
    return out


def _upgrade_tooling(*, dry_run: bool) -> None:
    _run(_pip("install", "--upgrade", "pip", "setuptools", "wheel"), dry_run=dry_run)


def _build_temp_requirements(requirement_specs: Sequence[str]) -> Path:
    """Create a temporary requirements file containing only requirement specs."""
    # Use delete=False so pip can read it on Windows.
    fd, path_str = tempfile.mkstemp(prefix="upkgs_", suffix=".txt")
    os.close(fd)
    path = Path(path_str)
    content = "\n".join(requirement_specs) + "\n"
    path.write_text(content, encoding="utf-8")
    return path


def _upgrade_from_requirements_file(requirement_specs: Sequence[str], *, dry_run: bool, timeout_s: int) -> bool:
    """Prefer a single resolver pass for the whole project requirements set.

    Returns True if the bulk install succeeded, False otherwise.
    """
    if not requirement_specs:
        return True

    temp_path: Path | None = None
    try:
        temp_path = _build_temp_requirements(requirement_specs)
        _run(
            _pip(
                "install",
                f"--default-timeout={timeout_s}",
                "--upgrade",
                "-r",
                str(temp_path),
            ),
            dry_run=dry_run,
        )
        return True
    except subprocess.CalledProcessError as e:
        print(f"[upkgs] Bulk requirements upgrade failed: {e}")
        return False
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except Exception:
                # Best-effort cleanup only.
                pass


def _upgrade_requirements(requirement_specs: Sequence[str], *, dry_run: bool, timeout_s: int) -> UpgradeResult:
    attempted = 0
    succeeded = 0
    failed = 0

    for spec in requirement_specs:
        name = spec.split("==", 1)[0].split(">=", 1)[0].split("<=", 1)[0].split("~=", 1)[0].split("!=", 1)[0]
        name = name.split("[", 1)[0].strip().lower()
        if name in SKIP_REQUIREMENTS:
            print(f"[upkgs] Skipping {spec!r} (handled by stdlib / not needed via pip)")
            continue

        attempted += 1
        try:
            _run(
                _pip(
                    "install",
                    f"--default-timeout={timeout_s}",
                    "--upgrade",
                    spec,
                ),
                dry_run=dry_run,
            )
            succeeded += 1
        except subprocess.CalledProcessError as e:
            failed += 1
            print(f"[upkgs] ERROR upgrading {spec!r}: {e}")
            print("[upkgs] Continuing with next package...")

    return UpgradeResult(attempted=attempted, succeeded=succeeded, failed=failed)


def _list_outdated() -> List[str]:
    """Return a list of package names that pip considers outdated."""
    proc = subprocess.run(
        _pip("list", "--outdated", "--format=json"),
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "pip list --outdated failed")
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Failed to parse pip outdated JSON: {e}")
    names: List[str] = []
    for item in data:
        name = str(item.get("name", "")).strip()
        if name:
            names.append(name)
    return names


def main(
    *,
    include_monitor: bool = False,
    upgrade_all_outdated: bool = False,
    dry_run: bool = False,
    requirements: Sequence[str] = (DEFAULT_REQUIREMENTS,),
    timeout_s: int = 1000,
) -> int:
    """Entry point for programmatic use.

    Returns process exit code (0 on success; non-zero if any upgrades failed).
    """

    print(f"[upkgs] Python: {sys.version.split()[0]}")
    print(f"[upkgs] Executable: {sys.executable}")

    repo_root = Path(__file__).resolve().parent
    req_files = [repo_root / r for r in requirements]
    if include_monitor:
        req_files.append(repo_root / DEFAULT_MONITOR_REQUIREMENTS)

    _upgrade_tooling(dry_run=dry_run)

    if upgrade_all_outdated:
        print("[upkgs] Mode: upgrade ALL outdated packages (more risky)")
        try:
            outdated = _list_outdated()
        except Exception as e:
            print(f"[upkgs] ERROR listing outdated packages: {e}")
            return 2
        if not outdated:
            print("[upkgs] No outdated packages found.")
            return 0
        result = _upgrade_requirements(outdated, dry_run=dry_run, timeout_s=timeout_s)
        print(f"[upkgs] Attempted {result.attempted}; OK {result.succeeded}; Failed {result.failed}")
        return 1 if result.failed else 0

    # Default: upgrade only what the project declares.
    all_specs: List[str] = []
    for req in req_files:
        specs = _read_requirement_lines(req)
        if not specs:
            print(f"[upkgs] Note: requirements file not found or empty: {req}")
            continue
        print(f"[upkgs] Loaded {len(specs)} specs from {req.name}")
        all_specs.extend(specs)

    specs = _dedupe_keep_order(all_specs)
    if not specs:
        print("[upkgs] No requirements found to upgrade.")
        return 0

    # First try a single pip resolver pass (better for constraints like opencv-python vs numpy).
    if _upgrade_from_requirements_file(specs, dry_run=dry_run, timeout_s=timeout_s):
        print(f"[upkgs] Upgraded via requirements set ({len(specs)} specs).")
        return 0

    # Fallback: upgrade individually and continue on errors.
    print("[upkgs] Falling back to per-package upgrades...")
    result = _upgrade_requirements(specs, dry_run=dry_run, timeout_s=timeout_s)
    print(f"[upkgs] Attempted {result.attempted}; OK {result.succeeded}; Failed {result.failed}")
    return 1 if result.failed else 0


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Upgrade this project’s dependencies in the current venv")
    p.add_argument(
        "--monitor",
        action="store_true",
        help=f"Also upgrade {DEFAULT_MONITOR_REQUIREMENTS}",
    )
    p.add_argument(
        "--all",
        action="store_true",
        help="Upgrade all outdated packages (not just project requirements). More risky.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print pip commands but do not execute them.",
    )
    p.add_argument(
        "--req",
        action="append",
        default=[],
        help=f"Additional requirements file(s) to apply (relative to repo root). Default: {DEFAULT_REQUIREMENTS}",
    )
    p.add_argument(
        "--timeout",
        type=int,
        default=int(os.environ.get("UPKGS_TIMEOUT", "1000")),
        help="pip --default-timeout in seconds (default 1000 or env UPKGS_TIMEOUT)",
    )
    return p


if __name__ == "__main__":
    args = _build_arg_parser().parse_args()
    reqs = [DEFAULT_REQUIREMENTS, *args.req] if args.req else [DEFAULT_REQUIREMENTS]
    raise SystemExit(
        main(
            include_monitor=args.monitor,
            upgrade_all_outdated=args.all,
            dry_run=args.dry_run,
            requirements=reqs,
            timeout_s=args.timeout,
        )
    )