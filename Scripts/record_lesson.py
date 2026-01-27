from __future__ import annotations
import argparse
import json
import sys
import time
from pathlib import Path


def write_jsonl(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def append_markdown(path: Path, title: str, body: str, tags: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    with path.open("a", encoding="utf-8") as f:
        f.write(f"\n\n## {title} ({ts})\n\n")
        if tags:
            f.write(f"Tags: {', '.join(tags)}\n\n")
        f.write(body.strip() + "\n")


def main() -> int:
    ap = argparse.ArgumentParser(description="Record a learning lesson to JSONL and Markdown")
    ap.add_argument("--title", required=True)
    ap.add_argument("--body", required=True)
    ap.add_argument("--tags", default="", help="Comma-separated tags")
    args = ap.parse_args()

    root = Path(__file__).resolve().parent.parent
    jsonl_path = root / "projects" / "Self-Improve" / "lessons.jsonl"
    md_path = root / "projects" / "Self-Improve" / "lessons.md"

    tags = [t.strip() for t in (args.tags or "").split(",") if t.strip()]
    obj = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "title": args.title,
        "body": args.body,
        "tags": tags,
    }
    write_jsonl(jsonl_path, obj)
    append_markdown(md_path, args.title, args.body, tags)
    print("Recorded lesson:", md_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
