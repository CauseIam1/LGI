"""
LGI — Looking Glass Interface
vision_supervisor.py: Periodic desktop-context audit loop.

Each cycle gathers (every sensor independently guarded, failing soft):
  * active window title + derived app name      (pygetwindow)
  * clipboard snippet, change-gated              (pyperclip)
  * screen keyword tokens from the window title
  * optional compressed screen JPEG, 1280px q60  (mss + OpenCV)
  * optional compressed webcam JPEG, 640px q50   (OpenCV — reuses the shared
    WebcamCapture provider's latest frame when the perception stack runs)

With attention gating on (LGI_ATTENTION_GATE=1, the v1.2 default) the cycle
is skipped while the perception stack reports the operator is NOT at the
desk; unknown availability fails open and never blocks an audit.

The snapshot is POSTed to the Polaris gateway /api/supervisor/audit
(text/JSON only over the wire). The gateway replies with a verdict
(SUPERVISOR_ALERT / LOG_ONLY / NO_ACTION) which is forwarded to the result
callback for HUD + voice routing. Extra fields (screen_image, webcam_image)
are ignored by the current gateway and kept for forward compatibility with
future vault vision ingestion.
"""

from __future__ import annotations

import base64
import re
import threading
import time
from typing import Any, Callable, Dict, Optional, Tuple

import numpy as np

from gateway_client import PolarisClient


class VisionSupervisor(threading.Thread):
    """Daemon thread auditing desktop context on a fixed interval."""

    def __init__(
        self,
        client: PolarisClient,
        interval_s: float = 30.0,
        user: str = "rich",
        screen_capture_enabled: bool = True,
        webcam_enabled: bool = True,
        clipboard_enabled: bool = True,
        webcam_provider: Optional[Any] = None,   # shared WebcamCapture (perception stack)
        attention_gate: bool = False,            # v1.2: skip audits while operator away
        result_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
        status_callback: Optional[Callable[[str], None]] = None,
        log: Callable[[str], None] = print,
    ) -> None:
        super().__init__(name="lgi-vision", daemon=True)
        self.client = client
        self.interval_s = max(5.0, interval_s)
        self.user = user
        self.screen_capture_enabled = screen_capture_enabled
        self.webcam_enabled = webcam_enabled
        self.clipboard_enabled = clipboard_enabled
        self.webcam_provider = webcam_provider
        self.attention_gate = attention_gate
        self._result_callback = result_callback
        self._status_callback = status_callback
        self._log = log

        self._stop_event = threading.Event()
        self._trigger = threading.Event()
        self._last_clipboard: Optional[str] = None

    # -- external control -------------------------------------------------- #
    def trigger(self) -> None:
        """Request an immediate audit cycle (Ctrl+Shift+S / HUD button)."""
        self._trigger.set()

    def stop(self) -> None:
        self._stop_event.set()
        self._trigger.set()

    # -- thread main -------------------------------------------------------- #
    def run(self) -> None:
        self._log(f"[vision] supervisor loop started (every {self.interval_s:.0f}s)")
        while not self._stop_event.is_set():
            try:
                self._cycle()
            except Exception as exc:
                self._log(f"[vision] audit cycle error: {exc}")
            # Sleep in short slices so trigger/stop react within 250 ms.
            deadline = time.monotonic() + self.interval_s
            while not self._stop_event.is_set() and time.monotonic() < deadline:
                if self._trigger.is_set():
                    break
                time.sleep(0.25)
            if self._stop_event.is_set():
                break
            self._trigger.clear()

    def _cycle(self) -> None:
        # v1.2 attention gate — skip the audit while the perception stack
        # reports the operator is away. Fail-open: perception down or flag
        # unknown (None) never blocks an audit.
        if self.attention_gate and self.webcam_provider is not None:
            attentive = getattr(self.webcam_provider, "user_attentive", None)
            if attentive is False:
                self._log("[vision] audit skipped — operator not at the desk")
                self._notify("idle")
                return
        self._notify("scanning")
        payload = self._gather()
        result = self.client.supervisor_audit(payload)
        if self._result_callback:
            try:
                self._result_callback(result)
            except Exception as exc:
                self._log(f"[vision] result dispatch error: {exc}")
        self._notify("idle")

    # ------------------------------------------------------------------ #
    # sensor gathering (each guarded; sensors auto-disable on failure)
    # ------------------------------------------------------------------ #
    def _gather(self) -> Dict[str, Any]:
        title, app = self._active_window()
        return {
            "active_window": title[:200],
            "clipboard_snippet": self._clipboard_snippet()[:4000],
            "screen_keywords": self._keywords(title),
            "user": self.user,
            "app": app[:80],
            "llm_triage": True,
            "screen_image": self._screen_image_b64(),
            "webcam_image": self._webcam_image_b64(),
        }

    def _active_window(self) -> Tuple[str, str]:
        title, app = "", "unknown"
        try:
            import pygetwindow as gw
            window = gw.getActiveWindow()
            if window is not None and getattr(window, "title", ""):
                title = str(window.title)
                app = title.split(" - ")[0].strip() or "unknown"
        except Exception:
            pass
        return title, app

    def _clipboard_snippet(self) -> str:
        if not self.clipboard_enabled:
            return ""
        try:
            import pyperclip
            text = pyperclip.paste()
        except Exception:
            return ""
        if not isinstance(text, str) or not text.strip():
            return ""
        if text == self._last_clipboard:  # change-gated: don't re-audit stale clips
            return ""
        self._last_clipboard = text
        return text

    @staticmethod
    def _keywords(title: str) -> list:
        seen, keywords = set(), []
        for token in re.findall(r"[a-z][a-z0-9_-]{2,}", title.lower()):
            if token not in seen:
                seen.add(token)
                keywords.append(token)
            if len(keywords) >= 12:
                break
        return keywords

    def _screen_image_b64(self) -> str:
        if not self.screen_capture_enabled:
            return ""
        try:
            import cv2
            import mss
            with mss.mss() as sct:
                shot = sct.grab(sct.monitors[1])  # primary monitor
                img = np.frombuffer(shot.raw, dtype=np.uint8).reshape(
                    shot.size[1], shot.size[0], 4
                )
            img = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
            img = self._resize(img, 1280)
            ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 60])
            return base64.b64encode(buf.tobytes()).decode("ascii") if ok else ""
        except ImportError:
            self.screen_capture_enabled = False
            self._log("[vision] mss/opencv missing — screen capture disabled")
            return ""
        except Exception as exc:
            self._log(f"[vision] screen capture failed: {exc}")
            return ""

    def _webcam_image_b64(self) -> str:
        if not self.webcam_enabled:
            return ""
        # Prefer the shared perception capture thread's latest frame (single
        # device owner, no per-audit open/close); fall back to the historical
        # one-shot capture when the perception stack is not running.
        if self.webcam_provider is not None:
            try:
                shared = self.webcam_provider.latest_jpeg_b64(max_width=640, quality=50)
                if shared:
                    return shared
            except Exception as exc:
                self._log(f"[vision] shared webcam frame unavailable: {exc}")
        try:
            import cv2
            cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)  # opened per-cycle; light stays off between audits
            try:
                if not cap.isOpened():
                    return ""
                ok, frame = cap.read()
                if not ok or frame is None:
                    return ""
            finally:
                cap.release()
            frame = self._resize(frame, 640)
            ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 50])
            return base64.b64encode(buf.tobytes()).decode("ascii") if ok else ""
        except ImportError:
            self.webcam_enabled = False
            self._log("[vision] opencv missing — webcam capture disabled")
            return ""
        except Exception as exc:
            self._log(f"[vision] webcam capture failed: {exc}")
            return ""

    @staticmethod
    def _resize(img: "np.ndarray", max_width: int):
        h, w = img.shape[:2]
        if w <= max_width:
            return img
        import cv2
        scale = max_width / float(w)
        return cv2.resize(img, (max_width, int(h * scale)), interpolation=cv2.INTER_AREA)

    def _notify(self, state: str) -> None:
        if self._status_callback:
            try:
                self._status_callback(state)
            except Exception:
                pass