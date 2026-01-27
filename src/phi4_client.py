from __future__ import annotations
import json
import time
from typing import Any, Dict
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError


class Phi4Client:
    """Minimal OpenAI-compatible client wrapper for an external PHI-4 endpoint.

    Expects an OpenAI-compatible base URL in `endpoint` (e.g., http://localhost:11434/v1
    or https://<azure-endpoint>/openai/deployments/<deployment>).

    If the endpoint is not actually reachable, methods return sensible fallbacks so the app
    can continue operating without crashing.
    """

    def __init__(self, endpoint: str, api_key: str = "", model: str = "phi-4-mini", timeout_ms: int = 15000, health_path: str = "/health"):
        self.endpoint = endpoint.rstrip("/")
        self.api_key = api_key or ""
        self.model = model
        self.timeout = max(1000, int(timeout_ms)) / 1000.0
        self.health_path = health_path

    def _headers(self) -> Dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            # OpenAI-compatible auth
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def _post_json(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        url = path if path.startswith("http") else f"{self.endpoint}{path}"
        data = json.dumps(payload).encode("utf-8")
        req = Request(url, data=data, headers=self._headers(), method="POST")
        try:
            with urlopen(req, timeout=self.timeout) as resp:
                body = resp.read().decode("utf-8", errors="replace")
                return json.loads(body)
        except (HTTPError, URLError, TimeoutError) as e:
            return {"ok": False, "error": str(e)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def _get_json(self, path: str) -> Dict[str, Any]:
        url = path if path.startswith("http") else f"{self.endpoint}{path}"
        req = Request(url, headers=self._headers(), method="GET")
        try:
            with urlopen(req, timeout=self.timeout) as resp:
                body = resp.read().decode("utf-8", errors="replace")
                return json.loads(body)
        except (HTTPError, URLError, TimeoutError) as e:
            return {"ok": False, "error": str(e)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def ping(self) -> Dict[str, Any]:
        # Try a health endpoint if provided; otherwise do a quick noop post
        if self.health_path:
            res = self._get_json(self.health_path)
            if res and isinstance(res, dict) and (res.get("ok") or res.get("status") == "ok"):
                return {"ok": True, "detail": res}
            # fall through to noop
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 1,
            "temperature": 0.0,
        }
        res = self._post_json("/chat/completions" if "/v1" in self.endpoint else "/v1/chat/completions", payload)
        return {"ok": bool(res and isinstance(res, dict) and res.get("id")), "detail": res}

    def compose(self, question: str, context: Dict[str, Any]) -> Dict[str, Any]:
        # Compose a basic prompt with context; expect an OpenAI-compatible response
        snapshot = {
            "question": question,
            "context": context,
            "ts": int(time.time()),
        }
        system = (
            "You generate concise, actionable plans for software automation tasks. "
            "Return a short plan with steps and key risks."
        )
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(snapshot, ensure_ascii=False)},
        ]
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.2,
            "max_tokens": 512,
        }
        path = "/chat/completions" if "/v1" in self.endpoint else "/v1/chat/completions"
        res = self._post_json(path, payload)
        if res.get("ok") is False:
            # Endpoint not reachable; provide a minimal local fallback structure
            return {
                "prompt": f"[LOCAL-FALLBACK] {question}",
                "reasons": ["remote planner unreachable"],
                "plan": [
                    "Ensure VS Code is focused",
                    "Verify controls active and OCR available if needed",
                    "Retry sending when ready or proceed with local steps",
                ],
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }
        try:
            content = (
                res.get("choices", [{}])[0]
                .get("message", {})
                .get("content", "")
                .strip()
            )
        except Exception:
            content = ""
        return {
            "prompt": content or f"Plan for: {question}",
            "reasons": [],
            "plan": ["Parse objectives", "Focus VS Code", "Execute safe steps"],
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
