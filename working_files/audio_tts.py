"""
LGI — Looking Glass Interface
audio_tts.py: Local Kokoro TTS engine (Windows / NVIDIA CUDA).

Pattern proven in example_voice_py.md:
    pipeline = KPipeline(lang_code='a', device='cuda')
    for gs, ps, audio in pipeline(text, voice='af_heart', speed=1.2):
        sd.play(chunk_as_float32, 24000)

Hard requirements honored:
  * Playback NEVER blocks the STT listener, the gateway client, or the Qt
    main loop — generation and playback run on dedicated daemon threads.
  * eSpeak NG environment variables are set BEFORE the kokoro import on
    Windows (phonemizer requirement).
  * CUDA init failure degrades gracefully to CPU rather than crashing LGI.
"""

from __future__ import annotations

import os
import re
import sys
import threading
import time
from collections import deque
from queue import Empty, Queue
from typing import Callable, Optional

import numpy as np
import sounddevice as sd

# Standard Windows install locations (only applied if the path exists and
# the variable is not already set in the environment).
ESPEAK_ENV_CANDIDATES = {
    "PHONEMIZER_ESPEAK_LIBRARY": r"C:\Program Files\eSpeak NG\libespeak-ng.dll",
    "PHONEMIZER_ESPEAK_PATH": r"C:\Program Files\eSpeak NG\espeak-ng.exe",
    "ESPEAK_DATA_PATH": r"C:\Program Files\eSpeak NG\espeak-ng-data",
}

MAX_TEXT_CHARS = 1500

# Diagnostic tee: pythonw silences stdout, so every [tts] log line is also
# appended here. Permanent — this tee is what cracked the silent-voice case.
# Capped: when the file exceeds TRACE_MAX_BYTES the next write reopens it in
# mode "w", restarting the log in place, so it can never grow unbounded.
LOG_FILE = os.path.join(os.environ.get("TEMP", "."), "lgi_tts_log.txt")
TRACE_MAX_BYTES = 1_000_000  # ~1 MB; rotation is truncate-on-open (no rename)


def _trace(msg: str) -> None:
    try:
        mode = "a"
        try:
            if os.path.getsize(LOG_FILE) > TRACE_MAX_BYTES:
                mode = "w"  # rotate: cap exceeded → restart the log
        except OSError:
            pass  # file missing or unreadable → just append
        with open(LOG_FILE, mode, encoding="utf-8", errors="replace") as fh:
            fh.write(time.strftime("[%H:%M:%S] ") + msg + "\n")
    except Exception:
        pass  # the tee must never break TTS


class KokoroTTS:
    """Non-blocking text-to-speech worker built on the local Kokoro pipeline."""

    def __init__(
        self,
        voice: str = "bf_emma",  # Emma; the af_emma pack name does not exist in Kokoro-82M (probe-verified Sep 30, 2026)
        speed: float = 1.2,
        sample_rate: int = 24000,
        device: str = "cuda",
        status_callback: Optional[Callable[[str], None]] = None,
        log: Callable[[str], None] = print,
    ) -> None:
        self.voice = voice
        self.speed = speed
        self.sample_rate = sample_rate
        self.device = device
        self._status_callback = status_callback
        self._raw_log = log
        self._log = self._tee_log
        _trace(f"[tts] KokoroTTS constructed voice={voice} device={device}")

        self._queue: Queue = Queue()
        self._lock = threading.Lock()
        self._stop = threading.Event()      # worker shutdown signal
        self._generation = 0                # bumped to interrupt current playback
        self._pause_req = threading.Event() # set => hold playback ("paused")
        self._enabled = True                # v1.6.3 — 🔊/🔇 master speech gate (set_enabled)
        self._speaking = False
        self._pipeline = None
        self._pipeline_device = ""
        self._thread: Optional[threading.Thread] = None

        # v1.6.1 — self-echo guard bookkeeping (read by LGI._dispatch_utterance):
        # what is currently spoken / when playback last finished / the last few
        # utterances, so an open mic that hears our own playback is recognized.
        self._current_text = ""
        self._finished_at = 0.0
        self._spoken_recent: deque = deque(maxlen=4)

        self._ensure_espeak_env()

    def _tee_log(self, msg: str) -> None:
        """Route every [tts] log line to both the file trace and the sink."""
        _trace(msg)
        try:
            self._raw_log(msg)
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #
    def start(self) -> None:
        """Launch the background pipeline worker (idempotent)."""
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._worker, name="lgi-tts", daemon=True)
        self._thread.start()

    def shutdown(self) -> None:
        """Interrupt playback and terminate the worker."""
        self.stop()
        self._stop.set()
        self._queue.put(None)

    # ------------------------------------------------------------------ #
    # control
    # ------------------------------------------------------------------ #
    def speak(self, text: str) -> bool:
        """
        Enqueue text for speech. Non-blocking; returns False if empty/invalid.
        The worker lazily initialises the Kokoro pipeline on first use.
        """
        # v1.6.3 — 🔇 Voice master switch: gate speech BEFORE synthesis so a
        # disabled voice costs nothing (no pipeline churn, no GPU work).
        if not self._enabled:
            _trace("[tts] speak() dropped — voice disabled (🔇 Voice off)")
            return False
        cleaned = self._clean_for_speech(text)
        if not cleaned:
            return False
        if not (self._thread and self._thread.is_alive()):
            self.start()
        self._queue.put(cleaned)
        _trace(f"[tts] speak() enqueued len={len(cleaned)} pending={self._queue.qsize()}")
        return True

    def stop(self) -> None:
        """Interrupt current speech immediately and drop anything queued."""
        with self._lock:
            self._generation += 1
        self._pause_req.clear()  # stop also cancels a pause hold
        _trace(f"[tts] stop() called gen->{self._generation} queued_items_dropped")
        try:
            while True:
                self._queue.get_nowait()
        except Empty:
            pass
        try:
            sd.stop()
        except Exception:
            pass

    def pause(self) -> None:
        """Request hold: finish the current 20ms poll slice, freeze at chunk offset."""
        self._pause_req.set()
        _trace(f"[tts] pause() requested gen={self._generation}")

    def resume(self) -> None:
        """Release hold: replay the remainder from the captured offset, then continue."""
        if self._pause_req.is_set():
            self._pause_req.clear()
            _trace(f"[tts] resume() gen={self._generation}")

    def set_enabled(self, enabled: bool) -> None:
        """v1.6.3 — 🔊/🔇 Voice master switch. False gates every future
        speak() call (replies, alerts, lessons — everything); current
        playback is NOT touched here, the caller decides (stop() cancels)."""
        self._enabled = bool(enabled)
        _trace(f"[tts] voice {'enabled' if self._enabled else 'disabled — future speech gated'}")

    @property
    def enabled(self) -> bool:
        """True while speech is allowed (🔊 Voice on)."""
        return self._enabled

    @property
    def is_speaking(self) -> bool:
        with self._lock:
            return self._speaking

    @property
    def current_text(self) -> str:
        """v1.6.1: text of the utterance currently being spoken ("" when idle)."""
        with self._lock:
            return self._current_text

    @property
    def finished_at(self) -> float:
        """v1.6.1: monotonic time the last utterance finished (~0 if never)."""
        with self._lock:
            return self._finished_at

    def spoken_recent_text(self) -> str:
        """v1.6.1: the last few spoken utterances, oldest → newest, joined."""
        with self._lock:
            return " ".join(t for _, t in self._spoken_recent)

    @property
    def pipeline_device(self) -> str:
        return self._pipeline_device

    # ------------------------------------------------------------------ #
    # worker internals
    # ------------------------------------------------------------------ #
    def _worker(self) -> None:
        try:
            if not self._ensure_pipeline():
                self._notify("error")
                return
            self._notify("ready")
            _trace(f"[tts] worker ready pipeline={self._pipeline_device}")
            while not self._stop.is_set():
                item = self._queue.get()
                if item is None:
                    break
                if not isinstance(item, str) or not item.strip():
                    continue
                with self._lock:
                    self._generation += 1
                    generation = self._generation
                    self._speaking = True
                    self._current_text = item
                _trace(f"[tts] item picked up len={len(item)}")
                self._notify("speaking")
                try:
                    self._synthesize(item, generation)
                except Exception as exc:
                    self._log(f"[tts] synthesis error: {exc}")
                finally:
                    with self._lock:
                        self._speaking = False
                        finished = time.monotonic()
                        self._finished_at = finished
                        if self._current_text:
                            self._spoken_recent.append((finished, self._current_text))
                        self._current_text = ""
                    if not self._stop.is_set():
                        self._notify("idle")
        except Exception as exc:
            self._log(f"[tts] worker crashed: {exc}")
            self._notify("error")

    def _ensure_pipeline(self) -> bool:
        """Import kokoro and build KPipeline; CUDA falls back to CPU."""
        try:
            from kokoro import KPipeline  # heavy import — deferred off the main thread
        except Exception as exc:
            self._log(f"[tts] kokoro import failed ({exc}); voice output disabled")
            return False
        devices = [self.device, "cpu"] if self.device != "cpu" else ["cpu"]
        for dev in devices:
            try:
                self._pipeline = KPipeline(lang_code="a", device=dev)
                self._pipeline_device = dev
                if dev != self.device:
                    self._log("[tts] CUDA unavailable — Kokoro running on CPU")
                return True
            except Exception as exc:
                self._log(f"[tts] pipeline init on '{dev}' failed: {exc}")
        return False

    def _synthesize(self, text: str, generation: int) -> None:
        try:
            out_idx = sd.default.device[1]
            info = sd.query_devices()[out_idx]
            _trace(f"[tts] synthesize out_dev={out_idx} name={info['name']!r} hostapi={info['hostapi']} sr={info['default_samplerate']}")
        except Exception as exc:
            _trace(f"[tts] device introspection failed: {exc}")
        for _gs, _ps, audio in self._pipeline(text, voice=self.voice, speed=self.speed):
            if self._stop.is_set() or not self._is_current(generation):
                try:
                    sd.stop()
                except Exception:
                    pass
                return
            # Pause gate: engage during pipeline-generation silence too (Kokoro
            # chunk latency), not only mid-chunk — a hold before the next chunk
            # dispatches freezes the queue with "paused" state, no audio.
            if (self._pause_req.is_set() and not self._stop.is_set()
                    and self._is_current(generation)):
                self._notify("paused")
                while self._pause_req.is_set():
                    if self._stop.is_set() or not self._is_current(generation):
                        try:
                            sd.stop()
                        except Exception:
                            pass
                        return
                    time.sleep(0.05)
                self._notify("speaking")
            chunk = self._audio_to_float32(audio)
            if chunk is None or chunk.size == 0:
                continue
            if not self._play_chunk(chunk, generation):
                return

    def _play_chunk(self, chunk: np.ndarray, generation: int) -> bool:
        """Play one chunk with an interruptible, pausable wait. False => interrupted."""
        offset = 0                      # samples of this chunk already played
        total = chunk.size
        while offset < total:
            # Pause gate: hold silently until resumed, cancelled, or superseded.
            if self._pause_req.is_set():
                if self._stop.is_set() or not self._is_current(generation):
                    return False
                self._notify("paused")
                while self._pause_req.is_set():
                    if self._stop.is_set() or not self._is_current(generation):
                        return False
                    time.sleep(0.05)
                self._notify("speaking")
            remaining = chunk[offset:]
            try:
                sd.play(remaining, self.sample_rate)
                _trace(f"[tts] sd.play dispatched size={remaining.size} sr={self.sample_rate}")
            except Exception as exc:
                self._log(f"[tts] playback error: {exc}")
                _trace(f"[tts] sd.play FAILED: {exc}")
                return False
            t0 = time.monotonic()
            deadline = t0 + remaining.size / float(self.sample_rate)
            paused = False
            while time.monotonic() < deadline:
                if self._stop.is_set() or not self._is_current(generation):
                    try:
                        sd.stop()
                    except Exception:
                        pass
                    return False
                if self._pause_req.is_set():
                    # Capture how much of this slice actually played, stop the
                    # stream, and hold — resume() replays the remainder.
                    played = int((time.monotonic() - t0) * self.sample_rate)
                    offset += max(0, min(played, remaining.size))
                    try:
                        sd.stop()
                    except Exception:
                        pass
                    self._notify("paused")
                    while self._pause_req.is_set():
                        if self._stop.is_set() or not self._is_current(generation):
                            return False
                        time.sleep(0.05)
                    self._notify("speaking")
                    paused = True
                    break
                time.sleep(0.02)
            if not paused:
                offset = total        # slice finished playing to its deadline
        return True

    def _is_current(self, generation: int) -> bool:
        with self._lock:
            return self._generation == generation

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #
    def _notify(self, state: str) -> None:
        if self._status_callback:
            try:
                self._status_callback(state)
            except Exception:
                pass

    @staticmethod
    def _ensure_espeak_env() -> None:
        """Set eSpeak NG env vars before the kokoro import on Windows only."""
        if sys.platform != "win32":
            return
        for key, path in ESPEAK_ENV_CANDIDATES.items():
            if not os.environ.get(key) and os.path.exists(path):
                os.environ[key] = path

    @staticmethod
    def _audio_to_float32(audio) -> Optional[np.ndarray]:
        try:
            data = audio
            if hasattr(data, "detach"):  # torch tensor
                data = data.detach().cpu().numpy()
            arr = np.asarray(data, dtype=np.float32)
            if arr.ndim > 1:
                arr = arr.reshape(-1)
            return arr
        except Exception:
            return None

    @staticmethod
    def _clean_for_speech(text: str) -> str:
        """Strip markdown noise so spoken responses sound natural."""
        if not text:
            return ""
        cleaned = re.sub(r"```.*?```", " code block omitted. ", text, flags=re.DOTALL)
        cleaned = re.sub(r"`([^`]*)`", r"\1", cleaned)
        cleaned = re.sub(r"https?://\S+", " link. ", cleaned)
        cleaned = re.sub(r"[*_#>|]+", " ", cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        if len(cleaned) > MAX_TEXT_CHARS:
            head = cleaned[:MAX_TEXT_CHARS]
            cleaned = head.rsplit(" ", 1)[0] + " … truncated."
        return cleaned