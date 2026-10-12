"""
LGI — Looking Glass Interface
gateway_client.py: Thread-safe HTTP client for the Polaris Mainframe gateway.

Architecture rules honored here:
  * Port 8082 carries text/JSON only — never raw audio (northstar.md).
  * All calls are defensive: connection errors, timeouts, and malformed JSON
    are converted into {"ok": False, "error": ...} dicts. Exceptions never
    propagate into caller threads.
  * A single requests.Session is shared behind a lock for keep-alive reuse.

Confirmed contracts (polaris_continuous_learning_gateway.py / supervisor_routes.py):
  POST /api/chat
      request:  {"prompt": "..."}  ("message" is the legacy alias; both are sent)
      optional desktop-vision fields (added only when a screenshot is attached):
                 {"image_base64": "<b64 jpeg>", "ocr_text": "...",
                  "active_window": "..."} — the gateway routes these turns
                 through its vision-capable LLM (images ride inside the JSON
                 payload; raw frames never stream over the wire)
      response: Ollama JSON {"response": "<text>", ...}
                (+ {"vision": true, "vision_model": "..."} on vision turns)
      errors:   404 {"error": "Unknown user"} (IP not allowlisted),
                400 {"error": "Missing prompt or message field"}, 503 offline
  POST /api/supervisor/audit
      request:  {"active_window", "clipboard_snippet", "screen_keywords",
                 "user", "app", "llm_triage"}
      response: {"status": "success", "verdict": "SUPERVISOR_ALERT"|"LOG_ONLY"|"NO_ACTION",
                 "category", "vault_persisted",
                 "triage": {"heuristic": {...}, "llm": {...}}, "timestamp"}
  GET  /api/supervisor/status
      response: lightweight liveness probe (used for the POLARIS LED heartbeat)

Feeding the Phase 3 always-listening HUD (read-only set):
  GET  /api/history/<user_id>
      response: {"status": "success", "user": ..., "count": N, "history": [
      {"user_message": "...", "ai_response": "...", "timestamp": ...,
      "sender": ...}]} — paired unification envelope; the fallback seed
      poll used while the Socket.IO feed is down.
  GET  /api/memory/vault/stats
  GET  /api/memory/vault/list?limit=N[&category=core|architecture|user|
      lesson|journal|attachment]
      response: {"documents": [{path, title, category, updated, snippet}]}
  GET  /api/memory/vault/doc?path=<enc>
      response: {"document": {path, content, meta: {...}, modified}}
  POST /api/memory/vault/query  {"query", "mode": hybrid|keyword|semantic,
      "limit", ["categories"]}
      response: {"results": [{path, title, category, updated, snippet,
      score, match_type}]}
"""

from __future__ import annotations

import threading
import time
import os
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import quote

import requests

DEFAULT_BASE_URL = "http://192.168.50.51:8082"

# v1.4.2 gateway asks WHERE a prompt came from so device-specific broadcasts
# (chat_tool_report) can be audience-correct. Keep in sync with
# gateway_feed.GatewayFeedClient.DEVICE_ID.
DEVICE_ID = "lgi-hud-x17"

# v1.4.2 timeout headroom for the chat pipeline: /api/chat on tool turns runs
# TWO LLM passes (ack + truthful report) plus the tools themselves (SSH checks,
# doc reads…). 65 s flirted with the cap; 180 s clears it. Override with
# LGI_CHAT_TIMEOUT (seconds; LGIConfig-independent — read in PolarisClient).


def _env_chat_timeout(default: float = 180.0) -> float:
    raw = os.environ.get("LGI_CHAT_TIMEOUT")
    if not raw:
        return default
    try:
        value = float(raw)
        return value if value > 0 else default
    except (TypeError, ValueError):
        return default


class PolarisClient:
    """JSON-over-HTTP client for the Polaris gateway (host port 8082)."""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        chat_timeout: float = None,    # None → LGI_CHAT_TIMEOUT env or 180 s default
        vision_chat_timeout: float = 120.0,  # vision turns: multimodal pass + big payloads
        audit_timeout: float = 30.0,   # supervisor audit incl. optional LLM triage
        status_timeout: float = 5.0,   # lightweight liveness probe
        max_retries: int = 2,
        backoff: float = 1.5,
        log: Callable[[str], None] = print,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.chat_timeout = float(chat_timeout) if chat_timeout else _env_chat_timeout()
        self.vision_chat_timeout = vision_chat_timeout
        self.audit_timeout = audit_timeout
        self.status_timeout = status_timeout
        self.max_retries = max_retries
        self.backoff = backoff
        self._log = log
        self._session = requests.Session()
        self._lock = threading.Lock()     # requests.Session is not strictly thread-safe
        self._online = False

    # ------------------------------------------------------------------ #
    # internals
    # ------------------------------------------------------------------ #
    def _request(
        self,
        method: str,
        path: str,
        payload: Optional[Dict[str, Any]] = None,
        timeout: float = 10.0,
    ) -> Dict[str, Any]:
        """Perform one HTTP request with retry/backoff. Never raises."""
        url = f"{self.base_url}{path}"
        last_error = "unknown error"

        for attempt in range(1 + self.max_retries):
            try:
                with self._lock:
                    response = self._session.request(
                        method, url, json=payload, timeout=timeout
                    )
                self._online = True

                try:
                    data = response.json()
                except ValueError:
                    data = {"error": f"non-JSON response (HTTP {response.status_code})"}

                if 200 <= response.status_code < 300 and "error" not in data:
                    return {"ok": True, "data": data, "status_code": response.status_code}

                # Gateway returned a JSON error body (e.g. {"error": "Unknown user"}).
                return {
                    "ok": False,
                    "error": str(data.get("error", f"HTTP {response.status_code}")),
                    "detail": data,
                    "status_code": response.status_code,
                }

            except (requests.ConnectionError, requests.Timeout) as exc:
                # Transient network problems: retry with linear backoff.
                self._online = False
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt < self.max_retries:
                    time.sleep(self.backoff * (attempt + 1))

            except requests.RequestException as exc:
                # Non-retryable protocol-level error.
                self._online = False
                last_error = f"{type(exc).__name__}: {exc}"
                break

        return {"ok": False, "error": last_error}

    # ------------------------------------------------------------------ #
    # public API
    # ------------------------------------------------------------------ #
    def chat(
        self,
        text: str,
        image_b64: Optional[str] = None,
        ocr_text: Optional[str] = None,
        active_window: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        POST /api/chat — send a voice-transcribed prompt to Polaris.

        Desktop-vision turns: pass image_b64 (base64 JPEG) to attach the
        operator's latest screen capture; ocr_text / active_window add local
        context. Text-only requests remain byte-identical to the classic
        contract. Never raises.

        Returns:
            {"ok": True, "response": "<polaris reply>", "data": {...}}
            {"ok": False, "error": "...", ...} on any failure.
        """
        payload = {"prompt": text, "message": text}
        # v1.4.2: origin tag — the gateway uses it to target device-specific
        # broadcasts (chat_tool_report is spoken ONLY on the asking device).
        payload["device"] = DEVICE_ID
        if image_b64:
            payload["image_base64"] = image_b64
            payload["active_window"] = str(active_window or "")
            if ocr_text:
                payload["ocr_text"] = str(ocr_text)
        timeout = self.vision_chat_timeout if image_b64 else self.chat_timeout
        result = self._request("POST", "/api/chat", payload, timeout)
        if not result["ok"]:
            return result

        data = result.get("data", {})
        response_text = str(data.get("response") or "").strip()
        if not response_text:
            return {"ok": False, "error": "gateway returned an empty response", "detail": data}
        return {"ok": True, "response": response_text, "data": data}

    def supervisor_audit(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        POST /api/supervisor/audit — submit a vision-context audit snapshot.

        Returns:
            {"ok": True, "verdict": "SUPERVISOR_ALERT"|"LOG_ONLY"|"NO_ACTION", "data": {...}}
            {"ok": False, "error": "...", ...} on any failure.
        """
        result = self._request("POST", "/api/supervisor/audit", payload, self.audit_timeout)
        if not result["ok"]:
            return result

        data = result.get("data", {})
        verdict = str(data.get("verdict") or "").strip().upper()
        if not verdict:
            return {"ok": False, "error": "audit response missing 'verdict'", "detail": data}
        return {"ok": True, "verdict": verdict, "data": data}

    def supervisor_status(self) -> Dict[str, Any]:
        """GET /api/supervisor/status — lightweight liveness probe for the POLARIS LED."""
        return self._request("GET", "/api/supervisor/status", None, self.status_timeout)

    def chat_history_paired(self, user: str) -> Dict[str, Any]:
        """GET /api/history/<user> — paired-seed fallback for the always-
        listening feed while its socket is down. Never raises.

        Returns:
            {"ok": True, "data": {...paired envelope...}} / {"ok": False, ...}
        """
        quoted = quote(str(user or "").strip(), safe="")
        result = self._request("GET", f"/api/history/{quoted}", None, 15.0)
        if not result["ok"]:
            return result
        data = result.get("data") or {}
        if str(data.get("status") or "") != "success":
            return {"ok": False, "error": "unpaired history envelope", "detail": data}
        return {"ok": True, "data": data}

    def vault_stats(self) -> Dict[str, Any]:
        """GET /api/memory/vault/stats — vault + index health for the HUD."""
        return self._request("GET", "/api/memory/vault/stats", None, 10.0)

    def vault_list(self, category: str = "", limit: int = 200) -> Dict[str, Any]:
        """GET /api/memory/vault/list — document index for the HUD browser.
        Query strings ride inside the path (_request sends JSON bodies only)."""
        params = f"limit={int(max(1, min(500, limit)))}"
        if str(category or "").strip():
            params += "&category=" + quote(str(category).strip(), safe="")
        return self._request("GET", f"/api/memory/vault/list?{params}", None, 15.0)

    def vault_doc(self, path: str) -> Dict[str, Any]:
        """GET /api/memory/vault/doc?path=<enc> — one document for the HUD viewer."""
        quoted = quote(str(path or "").strip(), safe="")
        if not quoted:
            return {"ok": False, "error": "vault doc requires a path"}
        return self._request("GET", f"/api/memory/vault/doc?path={quoted}", None, 15.0)

    def vault_search(self, query: str, mode: str = "hybrid", limit: int = 25,
                     categories: Optional[List[str]] = None) -> Dict[str, Any]:
        """POST /api/memory/vault/query — read-only HUD mirror of the
        dashboard's SEARCH (hybrid / keyword / semantic)."""
        payload: Dict[str, Any] = {
            "query": str(query or "").strip(),
            "mode": str(mode or "hybrid"),
            "limit": int(max(1, min(50, limit))),
        }
        if categories:
            payload["categories"] = [str(c) for c in categories if str(c).strip()]
        if not payload["query"]:
            return {"ok": False, "error": "vault search requires a query"}
        return self._request("POST", "/api/memory/vault/query", payload, 20.0)

    @property
    def online(self) -> bool:
        """Best-effort connectivity flag (updated on every request attempt)."""
        return self._online