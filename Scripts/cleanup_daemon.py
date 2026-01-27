from __future__ import annotations
import argparse
import json
import time
from pathlib import Path
from typing import Iterable


def load_manifest(paths: Iterable[Path]) -> dict[str, dict]:
    index: dict[str, dict] = {}
    for p in paths:
        try:
            if not p.exists():
                continue
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    fp = str(obj.get("path") or obj.get("file") or obj.get("image_path") or "").strip()
                    if not fp:
                        continue
                    index[fp] = obj
                except Exception:
                    continue
        except Exception:
            continue
    return index


def is_deletable_image(p: Path, manifest: dict[str, dict], retain_seconds: int) -> bool:
    try:
        if not p.exists() or not p.is_file():
            return False
        age = time.time() - p.stat().st_mtime
        if age < retain_seconds:
            return False
        key = str(p)
        meta = manifest.get(key) or manifest.get(p.name) or {}
        # deleteable flag OR observed tag
        deletable = bool(meta.get("deleteable", False)) or bool(meta.get("observed", False))
        return deletable
    except Exception:
        return False


def is_deletable_video(p: Path, retain_seconds: int, marker_ext: str = ".assessed") -> bool:
    try:
        if not p.exists() or not p.is_file():
            return False
        age = time.time() - p.stat().st_mtime
        if age < retain_seconds:
            return False
        marker = Path(str(p) + marker_ext)
        return marker.exists()
    except Exception:
        return False


def loop_cleanup(root: Path, interval_s: float, retain_seconds: int, manifest_paths: list[Path]) -> None:
    ocr_dir = root / "logs" / "ocr"
    screens_dir = root / "logs" / "screens"
    while True:
        manifest = load_manifest(manifest_paths)
        deleted = []
        # Images
        if ocr_dir.exists():
            for p in ocr_dir.glob("**/*"):
                if p.suffix.lower() in {".png", ".jpg", ".jpeg"}:
                    if is_deletable_image(p, manifest, retain_seconds):
                        try:
                            p.unlink(missing_ok=True)
                            deleted.append(str(p))
                        except Exception:
                            pass
        # Videos (assessed)
        if screens_dir.exists():
            for p in screens_dir.glob("**/*.mp4"):
                if is_deletable_video(p, retain_seconds, ".assessed"):
                    try:
                        p.unlink(missing_ok=True)
                        # also remove marker
                        Path(str(p) + ".assessed").unlink(missing_ok=True)
                        deleted.append(str(p))
                    except Exception:
                        pass
        print(f"cleanup_daemon: deleted={len(deleted)}")
        time.sleep(max(0.5, interval_s))


def main():
    parser = argparse.ArgumentParser(description="Cleanup daemon for observed OCR images")
    parser.add_argument("--interval", type=float, default=5.0, help="cleanup interval seconds")
    parser.add_argument("--retain", type=int, default=5, help="minimum age in seconds before deletion")
    parser.add_argument("--manifest", type=str, nargs="*", default=[
        "logs/ocr/observations.jsonl",
        "logs/tests/observe_react_workflow.jsonl",
        "logs/errors/events.jsonl",
    ], help="paths to jsonl manifests containing observed/deleteable entries")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    manifest_paths = [root / m for m in args.manifest]
    loop_cleanup(root, args.interval, args.retain, manifest_paths)


if __name__ == "__main__":
    main()
