from __future__ import annotations
import argparse
import json
import re
from pathlib import Path

def update_vsbridge_focus_list(src: Path, banned: list[str]) -> dict:
    text = src.read_text(encoding="utf-8")
    # Try to locate the 'default_cmds = [ ... ]' block inside focus_copilot_chat_view
    pattern = r"(default_cmds\s*=\s*\[)(.*?)(\])"
    m = re.search(pattern, text, flags=re.S|re.M)
    if not m:
        # Fallback: older versions might use 'cmds = [ ... ]'
        pattern2 = r"(cmds\s*=\s*\[)(.*?)(\])"
        m2 = re.search(pattern2, text, flags=re.S|re.M)
        if not m2:
            return {"updated": False, "reason": "command list not found"}
        head, body, tail = m2.group(1), m2.group(2), m2.group(3)
    else:
        head, body, tail = m.group(1), m.group(2), m.group(3)

    # Extract quoted strings in the list
    items = re.findall(r"['\"]([^'\"]+)['\"]", body)
    if not items:
        return {"updated": False, "reason": "no items parsed"}

    bl = [b for b in (banned or []) if b]
    kept = []
    removed = []
    for it in items:
        low = it.lower()
        if any(b in low for b in bl):
            removed.append(it)
        else:
            kept.append(it)

    if not removed:
        return {"updated": False, "reason": "no banned items present", "kept": kept}

    # Rebuild pretty list with same quoting style (use double quotes)
    new_body = ",\n            ".join([f'"{k}"' for k in kept])
    new_block = f"{head}\n            {new_body}\n            {tail}"
    new_text = text[:m.start(1)] + new_block + text[m.end(3):]
    src.write_text(new_text, encoding="utf-8")
    return {"updated": True, "removed": removed, "kept": kept}


def main() -> int:
    ap = argparse.ArgumentParser(description="Improve modules based on policy (remove banned palette commands from source)")
    args = ap.parse_args()

    root = Path(__file__).resolve().parent.parent
    policy = root / "config" / "policy_rules.json"
    vsbridge = root / "src" / "vsbridge.py"

    if not policy.exists():
        print("No policy file found; nothing to improve.")
        return 0

    cfg = json.loads(policy.read_text(encoding="utf-8"))
    pal = (cfg.get("palette") or {}) if isinstance(cfg, dict) else {}
    banned = [str(x).lower() for x in (pal.get("banned") or [])]

    if not banned:
        print("No banned palette commands; nothing to improve.")
        return 0

    if not vsbridge.exists():
        print("vsbridge.py not found; nothing to improve.")
        return 0

    res = update_vsbridge_focus_list(vsbridge, banned)
    if res.get("updated"):
        print(f"Updated vsbridge.py; removed: {res.get('removed')}")
        return 0
    else:
        print(f"No changes to vsbridge.py: {res.get('reason')}")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
