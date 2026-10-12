"""
LGI — Looking Glass Interface
audio_stt.py: Continuous open-mic STT listener (PyAudio capture +
faster-whisper transcription on the X17's NVIDIA GPU).

Design notes:
  * 16 kHz mono int16 capture; frames normalised to float32 for VAD.
  * Two-stage VAD: an adaptive energy gate segments live audio (200 ms
    pre-roll, 800 ms silence tail); faster-whisper's built-in Silero VAD
    (vad_filter=True) then strips residual non-speech from each utterance
    before inference — no hallucinated text on noise or breath.
  * Whisper default: large-v3-turbo CT2, beam_size=1,
    condition_on_previous_text=False (utterances are isolated).
  * Whisper init: CUDA + float16 with automatic CPU + int8 fallback.
  * `pause()` discards frames WITHOUT closing the audio stream, so resuming
    is instantaneous and the capture graph is never torn down.
  * Transcription runs on this daemon thread; the callback fires with
    final text only (never partials) to keep dispatch logic simple.
  * v1.3.2: degenerate whisper-on-silence transcripts ("A. . A. .") are
    dropped at the boundary (fewer than two distinct alphanumerics or a
    filler) and are never dispatched or shown.
  * v1.3.1 diagnostics: every stage prints an `[stt]` trace line (device open
    with name, speech onset with energies, transcribe start/finish durations,
    empty results) so a silent-deaf boot is diagnosable from lgi_console.log
    alone.
  * v1.6.5: absolute onset-gate floor raised 0.004 → 0.010 — the HyperX
    SoloCast's headphone/monitor bleed sits at 0.004–0.008 RMS and was
    minting phantom speech onsets (wake attempts) in ambient mode; real
    speech onsets measure ≥ ~0.02 (calibration onset: 0.0421).
  * v1.6.5 boot trace: the X17 venv's PyAudio build has NO classic
    default-input methods (every boot swallowed the AttributeError into
    "'?' (default input index -1)"). The trace now probes the known
    default-API spellings, falls back to a device-list scan of input-
    capable hardware so the boot line names real capture hardware, and
    logs the underlying cause — capture was never affected.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Callable, Optional

import numpy as np

SAMPLE_RATE = 16000

# v1.3.2 — degenerate-transcript junk filter. faster-whisper on breath/noise
# yields content-free text like "A. . A. ." or bare fillers ("uh", "hmm");
# those must never reach the HUD, the routing trace, or the gateway. A
# transcript is junk when its alphanumeric residue has fewer than two
# distinct characters, or the residue is a known filler (case-insensitive).
_JUNK_FILLERS = {"a", "uh", "um", "erm", "hmm", "mm", "mm-hmm", "uh-huh", "eh"}


def _is_junk_transcript(text: str) -> bool:
    """True for content-free whisper output (silence noise / bare fillers)."""
    normed = "".join(c for c in str(text or "").lower()
                     if c.isalnum() or c == "-")
    letters = normed.replace("-", "")
    return len(set(letters)) < 2 or normed in _JUNK_FILLERS


class MicListener(threading.Thread):
    """Daemon thread capturing mic audio, segmenting utterances, transcribing."""

    def __init__(
        self,
        model_size: str = "deepdml/faster-whisper-large-v3-turbo-ct2",
        device: str = "cuda",
        compute_type: str = "float16",
        language: str = "en",
        final_text_callback: Optional[Callable[[str], None]] = None,
        status_callback: Optional[Callable[[str], None]] = None,
        log: Callable[[str], None] = print,
        chunk_ms: int = 30,
        preroll_ms: int = 200,
        silence_end_ms: int = 800,
        min_speech_ms: int = 300,
        max_utterance_s: float = 30.0,
    ) -> None:
        super().__init__(name="lgi-stt", daemon=True)
        self.model_size = model_size
        self.device = device
        self.compute_type = compute_type
        self.language = language
        self._final_text_callback = final_text_callback
        self._status_callback = status_callback
        self._log = log

        self._stop_event = threading.Event()
        self._pause_flag = threading.Event()   # set => mic muted
        self._model = None

        self.chunk_ms = chunk_ms
        self._chunk_samples = int(SAMPLE_RATE * chunk_ms / 1000)
        self._preroll_chunks = max(1, preroll_ms // chunk_ms)
        self._silence_end_ms = silence_end_ms
        self._min_speech_ms = min_speech_ms
        self._max_utterance_ms = int(max_utterance_s * 1000)

    # -- external control -------------------------------------------------- #
    def pause(self) -> None:
        self._pause_flag.set()
        self._notify("muted")

    def resume(self) -> None:
        self._pause_flag.clear()
        self._notify("listening")

    @property
    def is_paused(self) -> bool:
        return self._pause_flag.is_set()

    def stop(self) -> None:
        self._stop_event.set()

    # -- thread main -------------------------------------------------------- #
    def run(self) -> None:
        if not self._init_model():
            return
        try:
            import pyaudio
        except Exception as exc:
            self._log(f"[stt] PyAudio unavailable ({exc}); listener disabled")
            self._notify("error")
            return

        pa = None
        stream = None
        try:
            pa = pyaudio.PyAudio()
            stream = pa.open(
                format=pyaudio.paInt16,
                channels=1,
                rate=SAMPLE_RATE,
                input=True,
                frames_per_buffer=self._chunk_samples,
            )
            _in_idx, _in_name = -1, "?"
            try:
                # v1.6.5: default-input lookups differ across PyAudio builds —
                # the X17 venv has NONE of the classic methods (the boot trace
                # used to swallow that AttributeError into "'?' (default
                # input index -1)"). Probe every known spelling, then fall
                # back to a device-list scan; capture always keeps using the
                # PortAudio default the unqualified open() above chose.
                _dev_fn = getattr(pa, "get_default_input_device", None)
                _info_fn = getattr(pa, "get_default_input_device_info", None)
                _hapi_fn = getattr(pa, "get_default_host_api_info", None)
                _idx = None
                if _dev_fn is not None:
                    _dev_idx = _dev_fn()
                    if _dev_idx is not None and _dev_idx >= 0:
                        _idx = _dev_idx
                if _idx is None and _info_fn is not None:
                    _info = _info_fn()
                    _idx = int(_info.get("index", -1))
                    _in_name = str(_info.get("name", "?"))
                if _idx is None and _hapi_fn is not None:
                    _idx = int(_hapi_fn().get("defaultInputDevice", -1))
                if _idx is not None and _idx >= 0:
                    _in_idx, _in_name = _idx, str(
                        pa.get_device_info_by_index(_idx).get("name", "?"))
                else:
                    raise ValueError("this PyAudio build has no default-input API")
            except Exception as exc:
                _cause = str(exc).strip() or type(exc).__name__
                self._log(f"[stt] default-input lookup unavailable: {_cause} — "
                          f"capture keeps using the PortAudio default")
                try:
                    for _di in range(pa.get_device_count()):
                        _info = pa.get_device_info_by_index(_di)
                        if int(_info.get("maxInputChannels", 0)) > 0:
                            _in_idx, _in_name = _di, str(_info.get("name", "?"))
                            break
                except Exception as _scan_exc:
                    self._log(f"[stt] device scan also failed: {_scan_exc}")
            self._log(f"[stt] mic open: '{_in_name}' (default input index "
                      f"{_in_idx}, {self._chunk_samples}-sample chunks)")
            self._notify("muted" if self._pause_flag.is_set() else "listening")
            self._log(f"[stt] listening ({self.model_size} @ {SAMPLE_RATE} Hz)")
            self._capture_loop(stream)
        except Exception as exc:
            self._log(f"[stt] capture failed: {exc}")
            self._notify("error")
        finally:
            try:
                if stream is not None:
                    stream.stop_stream()
                    stream.close()
            except Exception:
                pass
            try:
                if pa is not None:
                    pa.terminate()
            except Exception:
                pass
            self._notify("off")

    # ------------------------------------------------------------------ #
    # capture + VAD
    # ------------------------------------------------------------------ #
    def _capture_loop(self, stream) -> None:
        preroll = deque(maxlen=self._preroll_chunks)
        utterance = []
        in_speech = False
        voiced_ms = 0
        silence_ms = 0
        noise_floor = 0.01

        while not self._stop_event.is_set():
            try:
                raw = stream.read(self._chunk_samples, exception_on_overflow=False)
            except OSError as exc:
                self._log(f"[stt] stream read error: {exc}")
                time.sleep(0.25)
                continue

            if self._pause_flag.is_set():
                if in_speech:
                    in_speech = False
                    utterance, voiced_ms, silence_ms = [], 0, 0
                preroll.clear()
                continue

            frame = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
            energy = float(np.sqrt(np.mean(np.square(frame))))
            # v1.6.5: absolute floor 0.004 → 0.010 — SoloCast headphone bleed
            # sits at 0.004–0.008 RMS and minted phantom onsets in ambient
            # mode; real speech measures ≥ ~0.02 (calibration onset 0.0421).
            threshold = max(noise_floor * 3.0, 0.010)

            if not in_speech:
                if energy < threshold:   # adapt to room noise while idle
                    noise_floor = max(0.95 * noise_floor + 0.05 * energy, 0.001)
                preroll.append(frame)
                if energy > threshold:  # speech onset — include pre-roll
                    in_speech = True
                    utterance = list(preroll) + [frame]
                    voiced_ms, silence_ms = self.chunk_ms, 0
                    self._log(f"[stt] speech onset "
                              f"(energy {energy:.4f} > threshold {threshold:.4f})")
            else:
                utterance.append(frame)
                if energy > threshold:
                    voiced_ms += self.chunk_ms
                    silence_ms = 0
                else:
                    silence_ms += self.chunk_ms
                total_ms = len(utterance) * self.chunk_ms
                if silence_ms >= self._silence_end_ms or total_ms >= self._max_utterance_ms:
                    if voiced_ms >= self._min_speech_ms:
                        audio = np.concatenate(utterance)
                        self._log(f"[stt] transcribing {total_ms} ms capture ...")
                        _t0 = time.monotonic()
                        text = self._transcribe(audio)
                        self._log(f"[stt] transcribe done in "
                                  f"{int((time.monotonic() - _t0) * 1000)} ms")
                        if text and self._final_text_callback:
                            try:
                                self._final_text_callback(text)
                            except Exception as exc:
                                self._log(f"[stt] dispatch error: {exc}")
                    in_speech = False
                    utterance, voiced_ms, silence_ms = [], 0, 0
                    preroll.clear()

    # ------------------------------------------------------------------ #
    # transcription
    # ------------------------------------------------------------------ #
    def _transcribe(self, audio: np.ndarray) -> str:
        self._notify("processing")
        try:
            segments, _info = self._model.transcribe(
                audio,
                language=self.language,
                beam_size=1,
                vad_filter=True,   # Silero VAD bundled with faster-whisper
                vad_parameters={"min_silence_duration_ms": 500},
                condition_on_previous_text=False,   # isolated utterance: no context looping
            )
            text = " ".join(seg.text.strip() for seg in segments).strip()
            if not text:
                self._log("[stt] empty transcription — Silero/whisper returned "
                          "no speech text for a voiced segment")
            elif _is_junk_transcript(text):
                self._log(f"[stt] degenerate transcription ignored: "
                          f"{text[:40]!r}")
                return ""
            return text
        except Exception as exc:
            self._log(f"[stt] transcription error: {exc}")
            return ""
        finally:
            self._notify("muted" if self._pause_flag.is_set() else "listening")

    def _init_model(self) -> bool:
        try:
            from faster_whisper import WhisperModel
        except Exception as exc:
            self._log(f"[stt] faster-whisper unavailable ({exc}); listener disabled")
            self._notify("error")
            return False
        attempts = ([(self.device, self.compute_type), ("cpu", "int8")]
                     if self.device != "cpu" else [("cpu", "int8")])
        for dev, compute in attempts:
            try:
                self._model = WhisperModel(self.model_size, device=dev, compute_type=compute)
                self._log(f"[stt] whisper '{self.model_size}' ready on {dev}/{compute}")
                return True
            except Exception as exc:
                self._log(f"[stt] model init on {dev}/{compute} failed: {exc}")
        self._notify("error")
        return False

    def _notify(self, state: str) -> None:
        if self._status_callback:
            try:
                self._status_callback(state)
            except Exception:
                pass