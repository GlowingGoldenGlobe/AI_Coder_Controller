from __future__ import annotations
import hashlib
import time
from pathlib import Path
from typing import Optional


class OcrObserver:
    def __init__(self, ocr, action_log=None, stream_dir: Optional[Path] = None, interval_ms: int = 800):
        self.ocr = ocr
        self.log = action_log
        self.stream_dir = stream_dir
        self.interval = max(100, int(interval_ms)) / 1000.0
        self._last_run = 0.0
        self._last_hash: Optional[str] = None
        self.last_ok_ts: float = 0.0
        self.last_obs_ts: float = 0.0

        if self.stream_dir:
            self.stream_dir.mkdir(parents=True, exist_ok=True)
        self.stream_file = (self.stream_dir / "stream.jsonl") if self.stream_dir else None

    def _write_stream(self, obj: dict):
        if not self.stream_file:
            return
        try:
            import json
            with open(self.stream_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(obj, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def _load_notify_cfg(self) -> dict:
        try:
            import json

            root = Path(__file__).resolve().parent.parent
            cfg_path = root / "config" / "policy_rules.json"
            if cfg_path.exists():
                obj = json.loads(cfg_path.read_text(encoding="utf-8"))
                if isinstance(obj, dict):
                    return (obj.get("ocr") or {}) if isinstance(obj.get("ocr"), dict) else {}
        except Exception:
            return {}
        return {}

    def _notify_lane(self, message: str, lane: str = "workflow", image: str | None = None) -> None:
        try:
            import json

            root = Path(__file__).resolve().parent.parent
            d = root / "projects" / "Chat_Lanes"
            d.mkdir(parents=True, exist_ok=True)
            notif = d / "notifications.jsonl"
            evt = {
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                "type": "ocr_ready",
                "message": message,
                "lane": lane,
            }
            if image:
                evt["image"] = image
            with notif.open("a", encoding="utf-8") as f:
                f.write(json.dumps(evt, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def poll(self):
        now = time.time()
        if (now - self._last_run) < self.interval:
            return
        self._last_run = now
        self.last_obs_ts = now
        try:
            res = self.ocr.capture_chat_text(save_dir=self.stream_dir)
            ok = bool(res.get("ok"))
            if ok:
                self.last_ok_ts = now
            img = res.get("image_path")
            changed = False
            h = None
            if img:
                try:
                    with open(img, "rb") as f:
                        data = f.read()
                    h = hashlib.sha256(data).hexdigest()
                    changed = (h != self._last_hash) if h else False
                    if changed:
                        self._last_hash = h
                except Exception:
                    pass

            text = res.get("text", "") if isinstance(res, dict) else ""
            text_chars = len(text) if text else 0
            
            self._write_stream({
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "ok": ok,
                "changed": bool(changed),
                "image": str(img) if img else None,
                "elements": res.get("elements") if isinstance(res, dict) else None,
                "text": text if text_chars > 0 else None,
                "text_chars": text_chars,
            })
            if self.log and changed:
                self.log.log("ocr_stream", ok=ok, image=str(img) if img else None, text_chars=text_chars)
            if changed:
                try:
                    cfg = self._load_notify_cfg()
                    notify = bool(cfg.get("notify_on_ready", False))
                    if notify and ok:
                        lane = str(cfg.get("notify_lane", "workflow") or "workflow")
                        msg = str(cfg.get("notify_message", "OCR image ready") or "OCR image ready")
                        self._notify_lane(msg, lane=lane, image=str(img) if img else None)
                except Exception:
                    pass
        except Exception:
            pass
