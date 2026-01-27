import time
import json
from pathlib import Path


class OCREngine:
    def __init__(self, observe_timeout_ms: int = 2000):
        self.observe_timeout_ms = observe_timeout_ms

    def observe(self, tag: str) -> dict | None:
        # Placeholder: integrate real OCR here
        # Return a dict containing text and cursor hints when successful
        return {"tag": tag, "text": "", "cursor": "unknown"}


class InputGuard:
    def __init__(self, ocr: OCREngine, log_path: Path):
        self.ocr = ocr
        self.log_path = log_path
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

    def _log(self, event: str, meta: dict | None = None):
        entry = {"ts": time.time(), "event": event}
        if meta:
            entry.update(meta)
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")

    def require_observe(self, phase: str) -> dict | None:
        obs = self.ocr.observe(f"pre_{phase}")
        if not obs:
            self._log("ocr_observe_failed", {"phase": phase})
            return None
        self._log("ocr_observe_ok", {"phase": phase, "obs": obs})
        return obs

    def wrong_field(self, obs: dict) -> bool:
        # Accept obs containing elements or image path; detect large overlays indicating wrong field
        try:
            elems = (obs.get("elements") or []) if isinstance(obs, dict) else []
            if elems:
                # If a large element exists, assume palette/overlay
                try:
                    imgp = obs.get("image_path") or ""
                    if imgp:
                        from PIL import Image
                        im = Image.open(imgp)
                        w, h = im.size
                        area = float(w * h)
                        for e in elems:
                            b = e.get("bbox") or {}
                            a = float((b.get("width") or 0) * (b.get("height") or 0))
                            if a > 0.25 * area:
                                return True
                except Exception:
                    return False
        except Exception:
            pass
        return False

    def before_tab(self) -> bool:
        obs = self.require_observe("tab")
        if not obs:
            return False
        if self.wrong_field(obs):
            self._log("text_input_wrong_field", {"phase": "tab"})
            return False
        return True

    def before_text(self) -> bool:
        obs = self.require_observe("text_input")
        if not obs:
            return False
        if self.wrong_field(obs):
            self._log("text_input_wrong_field", {"phase": "text_input"})
            return False
        return True

    def before_enter(self) -> bool:
        obs = self.require_observe("enter")
        if not obs:
            return False
        if self.wrong_field(obs):
            self._log("text_input_wrong_field", {"phase": "enter"})
            return False
        return True


def demo_run():
    ocr = OCREngine()
    guard = InputGuard(ocr, Path("logs/events.jsonl"))
    # Simulate a workflow with required checks
    if not guard.before_tab():
        return 1
    if not guard.before_text():
        return 2
    if not guard.before_enter():
        return 3
    guard._log("gather_info", {"notes": "Collected placeholders"})
    guard._log("assess", {"status": "ok"})
    return 0


if __name__ == "__main__":
    code = demo_run()
    raise SystemExit(code)
