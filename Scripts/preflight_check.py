from __future__ import annotations

import json
import platform
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple


def _try_import(name: str) -> Tuple[bool, str]:
    try:
        __import__(name)
        return True, "ok"
    except Exception as e:
        return False, str(e)


def _load_json(path: Path) -> Tuple[Dict[str, Any] | None, str | None]:
    if not path.exists():
        return None, "missing"
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except Exception as e:
        return None, str(e)


def main() -> int:
    root = Path(__file__).resolve().parent.parent

    report: Dict[str, Any] = {
        "python": {
            "executable": sys.executable,
            "version": sys.version,
        },
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "version": platform.version(),
        },
        "checks": [],
        "ok": True,
    }

    def add(kind: str, ok: bool, detail: Any) -> None:
        report["checks"].append({"kind": kind, "ok": bool(ok), "detail": detail})
        if not ok:
            report["ok"] = False

    # Core files
    for rel in [
        "config/policy_rules.json",
        "config/ocr.json",
        "config/templates.json",
        "config/vscode_orchestrator.json",
        "config/controls_state.json",
    ]:
        p = root / rel
        add("file_exists", p.exists(), {"path": str(p)})

    # JSON parse
    for rel in [
        "config/policy_rules.json",
        "config/ocr.json",
        "config/templates.json",
        "config/vscode_orchestrator.json",
    ]:
        p = root / rel
        data, err = _load_json(p)
        add("json_parse", data is not None, {"path": str(p), "error": err})

    # Optional: Tesseract path sanity (only if configured)
    ocr_cfg, _ = _load_json(root / "config/ocr.json")
    if isinstance(ocr_cfg, dict):
        tcmd = str(ocr_cfg.get("tesseract_cmd", "") or "").strip()
        if tcmd:
            add("tesseract_cmd_exists", Path(tcmd).exists(), {"tesseract_cmd": tcmd})
        else:
            # Not fatal; pytesseract can still find it via PATH.
            add("tesseract_cmd_configured", True, {"note": "tesseract_cmd not set; relies on PATH"})

    # Imports (best-effort). These are the typical runtime deps.
    imports: List[str] = [
        "mss",
        "numpy",
        "PIL",
        "cv2",
        "pyautogui",
        "pytesseract",
        "rich",
    ]
    # Windows-first optional deps
    if platform.system().lower() == "windows":
        imports.extend(["uiautomation", "pynput"])

    for mod in imports:
        ok, err = _try_import(mod)
        add("import", ok, {"module": mod, "error": None if ok else err})

    # Write a short machine-readable artifact for troubleshooting.
    out_dir = root / "logs" / "tests"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "preflight_check.json"
    try:
        out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        add("write_report", True, {"path": str(out_path)})
    except Exception as e:
        add("write_report", False, {"path": str(out_path), "error": str(e)})

    print(json.dumps(report, indent=2))

    # Return non-zero only for truly critical problems.
    # If you're on a non-Windows machine, allow missing Windows-only deps.
    critical_kinds = {"json_parse", "write_report"}
    critical_fail = any((not c["ok"]) and (c["kind"] in critical_kinds) for c in report["checks"])  # type: ignore[index]
    return 1 if critical_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
