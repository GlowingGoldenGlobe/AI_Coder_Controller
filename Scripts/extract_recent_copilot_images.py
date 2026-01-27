import json
from pathlib import Path

EVENTS = Path("logs/errors/events.jsonl")
OUT = Path("logs/tests/ocr_verification_recent_20251218.txt")

OUT.parent.mkdir(parents=True, exist_ok=True)

lines = EVENTS.read_text(encoding="utf-8", errors="ignore").splitlines()

with OUT.open("w", encoding="utf-8") as fo:
    fo.write("OCR Recent Copilot Image Events\n")
    fo.write(f"Source: {EVENTS}\n\n")
    for i, line in enumerate(lines[-1000:]):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        ev = str(obj.get("event") or "")
        if not ev.startswith("copilot_"):
            continue
        has_image = False
        for k in ("point_image_path", "image_path"):
            if isinstance(obj.get(k), str) and obj.get(k).lower().endswith(".png"):
                has_image = True
        v2 = obj.get("image_paths")
        if isinstance(v2, list) and any(isinstance(p, str) and p.lower().endswith(".png") for p in v2):
            has_image = True
        if not has_image:
            continue
        fo.write("---\n")
        fo.write(f"ts: {obj.get('ts')}\n")
        fo.write(f"event: {obj.get('event')}\n")
        fo.write(f"step/tag: {obj.get('step') or obj.get('tag') or ''}\n")
        for k in ("point_image_path", "image_path", "image_paths"):
            if obj.get(k) is not None:
                fo.write(f"{k}: {obj.get(k)}\n")
        for text_field in ("point_preview", "preview", "ocr_preview"):
            v = obj.get(text_field)
            if v:
                s = str(v).replace("\n", " ")
                fo.write(f"{text_field}: {s}\n")
        labels = obj.get("labels")
        if isinstance(labels, list) and labels:
            fo.write(f"labels: {labels[:10]}\n")
        fo.write("raw_json: ")
        fo.write(json.dumps(obj, ensure_ascii=False)[:1000])
        fo.write("\n\n")

print(OUT)
