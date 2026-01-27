from pathlib import Path
import sys

def tail(path: Path, n: int = 80):
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except Exception as e:
        print(f"Error reading {path}: {e}")
        return
    for line in lines[-n:]:
        print(line)

if __name__ == "__main__":
    root = Path(__file__).resolve().parent.parent
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 80
    tail(root / "logs" / "actions" / "actions.jsonl", n)
