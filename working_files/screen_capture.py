"""
LGI — Looking Glass Interface
screen_capture.py: Continuous rolling desktop capture ("dashcam").

Design (Polaris Desktop Vision Integration):
  * dxcam talks to the Windows Desktop Duplication API (DXGI) for
    hardware-accelerated, zero-overhead frame grabbing on the X17.
  * The capture thread runs at `fps` (default 1 FPS) in video_mode=True with a
    small bounded ring buffer (default 8 frames) — flat memory while always
    keeping a ready frame for voice-triggered vision queries. No CUDA usage:
    capture rides the GPU copy engine, never contending with whisper-turbo
    or Kokoro.
  * Raw frames NEVER leave the machine. Only a compressed, downscaled JPEG is
    handed to the caller (images ride inside JSON payloads — LGI.md §2.3/§7).
  * Degrade, never crash: if dxcam is unavailable (non-Windows dev machine,
    DXGI blocked, driver hiccup) the roller falls back to one-shot mss grabs
    per request, so desktop vision keeps working at slightly higher latency.

Thread model:
  * latest_jpeg_b64() / ocr_text() may be called from any worker thread (the
    chat executor); frame access is guarded by a lock.
"""

from __future__ import annotations

import base64
import threading
import time
from typing import Callable, Optional

import numpy as np


class ScreenRoller:
    """Rolling desktop capture: DXcam background thread + on-demand JPEGs."""

    def __init__(
        self,
        fps: float = 1.0,
        max_buffer_len: int = 8,
        log: Callable[[str], None] = print,
    ) -> None:
        self.fps = max(0.5, fps)
        self.max_buffer_len = max(2, max_buffer_len)
        self._log = log
        self._lock = threading.Lock()
        self._camera = None            # dxcam handle (None in fallback modes)
        self._mode = "idle"            # idle | dxcam | mss | disabled
        self._last_frame_ts: Optional[float] = None
        self._tesseract_ok = True      # auto-disabled on first ImportError

    # -- lifecycle ------------------------------------------------------------ #
    def start(self) -> None:
        with self._lock:
            if self._mode != "idle":
                return
            try:
                import dxcam
                self._camera = dxcam.create(output_color="BGR",
                                            max_buffer_len=self.max_buffer_len)
                if self._camera is None:
                    raise RuntimeError("dxcam.create() returned None "
                                       "(no DXGI output device)")
                self._camera.start(target_fps=self.fps, video_mode=True)
                self._mode = "dxcam"
                self._log(f"[screen] dxcam dashcam rolling at {self.fps:.0f} fps "
                          f"(ring buffer {self.max_buffer_len} frames)")
            except Exception as exc:
                self._camera = None
                self._mode = "mss"
                self._log(f"[screen] dxcam unavailable ({exc}) — "
                          "falling back to mss one-shot capture")

    def stop(self) -> None:
        with self._lock:
            if self._mode == "dxcam" and self._camera is not None:
                try:
                    self._camera.stop()
                    self._camera.release()
                except Exception:
                    pass
                self._camera = None
            self._mode = "disabled"

    # -- frame access ----------------------------------------------------------- #
    def _latest_frame_locked(self):
        """Detached copy of the most recent dxcam ring-buffer frame."""
        if self._mode == "dxcam" and self._camera is not None:
            try:
                frame = self._camera.get_latest_frame()
                if frame is not None:
                    self._last_frame_ts = time.monotonic()
                    return frame.copy()  # detach from the reusable ring buffer
            except Exception as exc:
                self._log(f"[screen] dxcam frame grab failed: {exc}")
        return None

    def _mss_frame(self):
        """Fallback one-shot grab of the primary monitor (mss + OpenCV)."""
        try:
            import cv2
            import mss
            with mss.mss() as sct:
                shot = sct.grab(sct.monitors[1])
                img = np.frombuffer(shot.raw, dtype=np.uint8).reshape(
                    shot.size[1], shot.size[0], 4
                )
            self._last_frame_ts = time.monotonic()
            return cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
        except Exception as exc:
            self._log(f"[screen] mss fallback grab failed: {exc}")
            return None

    def latest_frame(self):
        """Most recent desktop frame (BGR ndarray) or None if unavailable."""
        with self._lock:
            frame = self._latest_frame_locked()
        if frame is not None:
            return frame
        if self._mode in ("mss", "dxcam"):
            return self._mss_frame()  # dxcam buffer not primed yet → one-shot
        return None

    def frame_age_s(self) -> Optional[float]:
        ts = self._last_frame_ts
        return None if ts is None else max(0.0, time.monotonic() - ts)

    # -- vision payload helpers --------------------------------------------------- #
    def latest_jpeg_b64(self, max_width: int = 1920, quality: int = 70) -> str:
        """Compressed base64 JPEG of the latest frame, downscaled to max_width."""
        frame = self.latest_frame()
        if frame is None:
            return ""
        try:
            import cv2
            ok, buf = cv2.imencode(
                ".jpg", self._resize(frame, max_width),
                [cv2.IMWRITE_JPEG_QUALITY, int(quality)])
            return base64.b64encode(buf.tobytes()).decode("ascii") if ok else ""
        except Exception as exc:
            self._log(f"[screen] JPEG encode failed: {exc}")
            return ""

    def ocr_text(self, max_chars: int = 4000) -> str:
        """Local Tesseract OCR of the latest frame ("" when unavailable)."""
        if not self._tesseract_ok:
            return ""
        frame = self.latest_frame()
        if frame is None:
            return ""
        try:
            import cv2
            import pytesseract
            text = pytesseract.image_to_string(self._resize(frame, 1920))
            text = " ".join(str(text or "").split())
            return text[:max_chars]
        except ImportError:
            self._tesseract_ok = False
            self._log("[screen] pytesseract/Tesseract not installed — OCR context "
                      "disabled (vision turns continue without it)")
            return ""
        except Exception as exc:
            self._log(f"[screen] OCR failed: {exc}")
            return ""

    @staticmethod
    def _resize(img, max_width: int):
        h, w = img.shape[:2]
        if w <= max_width:
            return img
        import cv2
        scale = max_width / float(w)
        return cv2.resize(img, (max_width, int(h * scale)),
                          interpolation=cv2.INTER_AREA)

    @property
    def mode(self) -> str:
        return self._mode