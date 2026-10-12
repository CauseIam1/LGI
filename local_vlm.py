"""
LGI — Looking Glass Interface
local_vlm.py: Webcam-analyze VLM client (Ollama — polaris-ai:latest).

v1.2 design note: the original blueprint called for a local 4-bit llava model
resident on the X17. The operator opted for the existing `polaris-ai:latest`
(Modelfile wrap of glm-5.3-flash:cloud — vision-capable, and the same image
model the gateway already routes LGI desktop-vision turns through), so this
module is a thin, defensive Ollama /api/generate client:

  * POST {base_url}/api/generate with the prompt and the base64 webcam JPEG
    in the top-level `images` field — the exact Ollama shape the gateway's
    vision ladder uses (field-proven with polaris-ai:latest). No local
    model load, no VRAM residency, nothing to idle-unload.
  * Single-flight: one analysis at a time; concurrent requests fail fast with
    a busy error instead of queueing behind a slow vision pass.
  * Never raises: every failure returns {"ok": False, "error": ...} and the
    caller routes a short spoken error line. Ollama offline (503 / refused)
    degrades the feature, never the app.
  * Model / URL / timeout are config knobs (LGI_VLM_MODEL / LGI_VLM_URL /
    LGI_VLM_TIMEOUT) — swapping to a true local vision model later (e.g.
    `ollama pull llava` on the X17 + LGI_VLM_URL=http://localhost:11434) is
    a one-line change, no code edit.
"""

from __future__ import annotations

import threading
from typing import Callable, Dict, Optional

import requests

DEFAULT_BASE_URL = "http://192.168.50.51:11434"
DEFAULT_MODEL = "polaris-ai:latest"

DEFAULT_PROMPT = (
    "You are the camera eyes of a desktop assistant. Look at this webcam "
    "frame and describe in one or two short sentences what you see — people, "
    "objects, and anything notable. Be concrete and brief; your reply is "
    "spoken aloud."
)


class LocalVLM:
    """Ollama /api/generate client for webcam-analyze turns. Never raises."""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        model: str = DEFAULT_MODEL,
        timeout_s: float = 90.0,        # cloud-proxied vision pass — keep generous
        max_reply_chars: int = 600,
        log: Callable[[str], None] = print,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_s = timeout_s
        self.max_reply_chars = max_reply_chars
        self._log = log
        self._session = requests.Session()
        self._busy = threading.Lock()

    # -- public ------------------------------------------------------------ #
    def analyze(self, jpeg_b64: str, prompt: Optional[str] = None) -> Dict:
        """
        Describe one webcam frame. Returns:
            {"ok": True,  "response": "<short description>"}
            {"ok": False, "error": "..."} — no frame / busy / Ollama failure.
        """
        if not jpeg_b64:
            return {"ok": False, "error": "no webcam frame available"}
        if not self._busy.acquire(blocking=False):
            return {"ok": False, "error": "analysis already in progress"}
        try:
            payload = {
                "model": self.model,
                "prompt": prompt or DEFAULT_PROMPT,
                "images": [jpeg_b64],   # top-level images (Ollama /api/generate)
                "stream": False,
                "options": {"temperature": 0.3, "num_predict": 220},
            }
            response = self._session.post(
                f"{self.base_url}/api/generate", json=payload, timeout=self.timeout_s)
            if response.status_code == 404:
                return {"ok": False,
                        "error": f"model '{self.model}' not found on Ollama"}
            response.raise_for_status()
            data = response.json()
            content = str(data.get("response") or "").strip()
            if not content:
                return {"ok": False, "error": "model returned an empty description"}
            return {"ok": True, "response": content[: self.max_reply_chars]}
        except requests.Timeout:
            return {"ok": False, "error": "Ollama vision pass timed out"}
        except requests.ConnectionError:
            return {"ok": False, "error": f"Ollama unreachable at {self.base_url}"}
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        finally:
            self._busy.release()