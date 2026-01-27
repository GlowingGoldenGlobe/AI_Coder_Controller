from __future__ import annotations
import json
from pathlib import Path
from src.cleanup import FileCleaner


def main():
    root = Path(__file__).resolve().parent.parent
    cfg_path = root / "config" / "policy_rules.json"
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    except Exception:
        cfg = {}
    cleanup_cfg = (cfg.get("cleanup") or {})
    cleaner = FileCleaner(
        base=root,
        dirs=cleanup_cfg.get("dirs", ["logs/ocr"]),
        patterns=cleanup_cfg.get("patterns", ["*.png", "*.jpg"]),
        retain_seconds=int(cleanup_cfg.get("retain_seconds", 30)),
        logger=None,
        rules=cleanup_cfg.get("rules"),
    )
    res = cleaner.clean_once()
    print(f"Scanned: {res['scanned']}, Deleted: {len(res['deleted'])}")


if __name__ == "__main__":
    main()
