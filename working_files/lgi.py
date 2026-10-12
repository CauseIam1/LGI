"""
LGI — Looking Glass Interface
lgi.py: Entry point + orchestrator for the floating HUD Executive Supervisor
Client on the Alienware X17.

Module map:
  hud_ui.py            frameless always-on-top PyQt6 HUD (LEDs, exchange, alerts)
  audio_stt.py         continuous open-mic listener -> final utterance text
  audio_tts.py         local Kokoro TTS (CUDA, 24 kHz, non-blocking playback)
  vision_supervisor.py periodic desktop/webcam/clipboard audit -> gateway verdict
  screen_capture.py    rolling DXcam "dashcam" -> on-demand JPEG frames
  webcam_perception.py continuous webcam tracking + gestures + spatial radar
  local_vlm.py         webcam-analyze VLM turns via Ollama (polaris-ai:latest)
  gateway_client.py    Polaris gateway HTTP client (text/JSON only)
  gateway_feed.py      v1.4 always-listening Socket.IO feed: gateway identity
                       room (chat seed + finals + stream deltas) and 'status'
                       room (task feed) over ONE client + ConversationStore
  gesture_teaching.py  v1.6.0 taught-gesture store + voice-lesson engine
  hud_ui.py            frameless always-on-top PyQt6 HUD (LEDs, exchange, alerts)
  * Daemon workers (STT, TTS, vision, heartbeat, chat executor) NEVER touch
    Qt. They push events onto queues; a 100 ms QTimer in the Qt main thread
    drains the queues and mutates the HUD — single-threaded UI updates.
  * Global hotkeys (keyboard lib) fire on their own thread and push command
    strings into the command queue, drained by the same timer.

Voice routing:
  * Utterances beginning with the wake word ("polaris ..." — punctuation right
    after the name is tolerated) are sent to POST /api/chat; the reply is
    displayed and spoken through Kokoro.
  * Conversation talk window (v1.3): each successful exchange opens/renews a
    wake-free window (LGI_TALK_WINDOW, default 180 s) — follow-up utterances
    route straight to chat until it expires; "polaris, sleep" closes it early.
    v1.6.4: a user mic-mute freezes the countdown (remaining seconds are
    restored on unmute), and expiry now returns to ambient listening with a
    one-shot console + HUD cue.
  * Wake-word utterances that reference what is on screen (VISION_INTENT_RE)
    become desktop-vision turns: the latest dashcam JPEG + optional local
    OCR text ride inside the same JSON payload (images never stream raw).
  * Camera-directed utterances (WEBCAM_ANALYZE_RE — "analyze this
    component") take the webcam-analyze path: the latest perception frame
    goes straight to Ollama (polaris-ai:latest) for a short spoken
    description — no gateway chat stack involved.
  * Gesture events from the perception stack (palm = mic toggle, fist =
    media play/pause) arrive on the command queue exactly like hotkeys —
    v1.3.2: source-tagged ("mic:gesture", "mic:hotkey") and every mic
    mute/resume trace records who muted (HUD button / hotkey / gesture).
  * Taught gestures (v1.6.0): "Polaris, teach a gesture" starts a voice
    lesson (show pose ~2–3 s → name what it means); profiles live in
    taught_gestures.json and at runtime (LGI_TAUGHT_GESTURES) recognition
    dispatches "taught:<name>:<action>" commands (media | mic | stop_voice |
    chat); "Polaris, forget gesture <name>" and "Polaris, list gestures"
    manage them by voice.
  * Local voice commands ("stop talking", "be quiet") silence the TTS
    immediately without hitting the network.
  * Other utterances appear as ambient transcript only (never sent).
  * Supervisor audits returning SUPERVISOR_ALERT light the red LED, raise
    the HUD banner, and speak the alert category + reason (🔊 toggle, 🔇 default).

Environment overrides (all optional):
  LGI_POLARIS_URL, LGI_OPERATOR_USER, LGI_STT_MODEL, LGI_STT_DEVICE,
  LGI_STT_COMPUTE, LGI_TTS_DEVICE, LGI_WAKE_WORD, LGI_WAKE_REQUIRED,
  LGI_TALK_WINDOW, LGI_FEED, LGI_FEED_PER_SIDE, LGI_FEED_FALLBACK,
  LGI_HUD_FONT, LGI_HUD_OPACITY,
  LGI_AUDIT_INTERVAL, LGI_HEARTBEAT, LGI_WEBCAM, LGI_SCREENSHOT,
  LGI_VISION_CHAT, LGI_VISION_FPS, LGI_VISION_WIDTH, LGI_VISION_QUALITY,
  LGI_OCR, LGI_DUCK_WHILE_SPEAKING, LGI_SPEAK_ALERTS,
  LGI_WEBCAM_TRACK, LGI_GESTURES, LGI_ATTENTION_GATE, LGI_RADAR,
  LGI_RADAR_URL, LGI_WEBCAM_FPS, LGI_VLM, LGI_VLM_URL, LGI_VLM_MODEL,
  LGI_VLM_TIMEOUT, LGI_TAUGHT_GESTURES
"""

from __future__ import annotations

import os
import re
import sys
import time

# --- pythonw stdout/stderr guard ------------------------------------------ #
# pythonw has no console: sys.stdout/stderr are None, which breaks libraries
# that log at import time (kokoro chain fails with "Cannot log to objects of
# type 'NoneType'" -> voice silently disabled). Attach lazily-flushed fallback
# files so console-less launches still have real streams.
if sys.stdout is None or sys.stderr is None:
    boot_log = open(
        os.path.join(os.environ.get("TEMP", "."), "lgi_console.log"),
        "a",
        buffering=1,
        encoding="utf-8",
        errors="replace",
    )
    if sys.stdout is None:
        sys.stdout = boot_log
    if sys.stderr is None:
        sys.stderr = boot_log


def _uncaught_hook(etype, evalue, tb) -> None:
    """Custom excepthook — PyQt6 aborts via qFatal (0xc0000409) when the
    default hook is in place; a custom hook makes it log-and-survive instead."""
    import traceback
    stamp = datetime.now().strftime("%H:%M:%S")
    try:
        print(f"=== UNCAUGHT EXCEPTION {stamp} ===", file=sys.stderr, flush=True)
        traceback.print_exception(etype, evalue, tb, file=sys.stderr)
        sys.stderr.flush()
    except Exception as inner:
        try:
            print(f"[hook] exc during logging: {inner!r}", file=sys.stderr, flush=True)
        except Exception:
            pass


sys.excepthook = _uncaught_hook

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from difflib import SequenceMatcher
from queue import Empty, Queue
from typing import Any, Dict, Optional, Tuple

from PyQt6.QtCore import QObject, QTimer

from audio_stt import MicListener
from audio_tts import KokoroTTS
from gateway_client import PolarisClient
from gateway_feed import ConversationStore, GatewayFeedClient, parse_history_pairs
from gesture_teaching import GestureTeacher
from hud_ui import HUDWindow
from local_vlm import LocalVLM
from screen_capture import ScreenRoller
from vision_supervisor import VisionSupervisor
from webcam_perception import WebcamPerception, one_shot_jpeg_b64


def _log(message: str) -> None:
    print(f"[LGI {datetime.now().strftime('%H:%M:%S')}] {message}", flush=True)


# --- session liveness marker (v1.5.0 guarded-watchdog contract) ------------ #
# The re-armed `LGI Watchdog` task relaunches LGI only while this marker file
# exists: written right after a successful start, removed on graceful exit.
# A force-kill or native crash leaves it behind -> guarded resurrection; an
# operator quit or a boot (file absent) -> the watchdog honors the intent and
# stays down. Marker failures are logged and non-fatal (the watchdog then
# errs on the side of staying down).
SESSION_MARKER = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              ".session_active")


def _write_session_marker() -> None:
    """Mark an actively ignited session (guarded-watchdog resurrect contract)."""
    try:
        with open(SESSION_MARKER, "w", encoding="utf-8") as fh:
            fh.write(f"{datetime.now().isoformat(timespec='seconds')} "
                     f"pid={os.getpid()}\n")
        _log(f"session marker written: {SESSION_MARKER}")
    except Exception as exc:
        _log(f"session marker write failed ({exc}) — watchdog stays passive "
             "for this session")


def _clear_session_marker() -> None:
    """Graceful exit: tell the watchdog this shutdown is intentional."""
    try:
        os.remove(SESSION_MARKER)
        _log("session marker cleared — watchdog treats this exit as intended")
    except FileNotFoundError:
        pass
    except Exception as exc:
        _log(f"session marker cleanup failed ({exc})")


# --- second-instance veto (v1.5.1) ------------------------------------------ #
# Two LGI sessions racing the same GPU/audio native stack heap-corrupt within
# seconds of coexistence (pythonw 0xc0000374 in ntdll, observed live Oct 4
# 15:25:20→21 when a fresh ignition booted beside a live older session). The
# Global\ named mutex refuses a second launch BEFORE any heavy import, GPU,
# audio or GUI init; the OS releases it when the owning process dies, so a
# crash (or force-kill) can never wedge a guarded restart.
INSTANCE_MUTEX = "Global\\LGI_X17_SingleInstance"


def _acquire_instance_lock() -> bool:
    """True when this launch may proceed; False when a live session already
    holds the Global mutex (second instance — log + exit instead of GPU-race
    corruption). Failures degrade to unguarded startup, never to a blocked
    ignite: the watchdog contract above stays intact either way."""
    try:
        import ctypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        # Initial owner = True; the handle is deliberately never released —
        # it dies with the process, which is exactly the liveness we want.
        _acquire_instance_lock._handle = kernel32.CreateMutexW(None, True,
                                                               INSTANCE_MUTEX)
        if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
            _log("second instance refused — LGI already running; exiting "
                 "before GPU/audio init")
            return False
        _log("instance lock acquired — second-instance veto armed")
    except Exception as exc:  # non-Windows platform or API hiccup
        _log(f"instance lock unavailable ({exc}) — proceeding unguarded")
    return True


def _env_opacity_pct(key: str, default: float) -> float:
    """LGI_HUD_OPACITY accepts legacy 0..1 floats or 40..100 percentages;
    clamps to the slider range. Unparseable values fall back to the default."""
    raw = os.environ.get(key)
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    if value <= 1.0:
        value *= 100.0
    return min(100.0, max(40.0, value))


@dataclass
class LGIConfig:
    polaris_url: str = "http://192.168.50.51:8082"
    operator_user: str = "rich"
    wake_word: str = "polaris"
    wake_word_required: bool = True
    talk_window_s: float = 180.0       # wake-free conversation window per reply; 0 disables
    stt_model: str = "deepdml/faster-whisper-large-v3-turbo-ct2"
    stt_device: str = "cuda"
    stt_compute: str = "float16"
    tts_voice: str = "bf_emma"
    tts_speed: float = 1.2
    tts_device: str = "cuda"
    audit_interval_s: float = 30.0
    heartbeat_s: float = 15.0
    # -- v1.4 always-listening HUD (Phase 3) --------------------------------- #
    feed_enabled: bool = True          # ONE socketio link: chat identity room + task 'status' room
    feed_per_side: int = 5             # rolling window: latest 5 per side (Looking-Glass seed v2)
    feed_fallback_s: float = 15.0      # REST paired-history poll while the feed link is down (0 = off)
    hud_font_px: int = 14              # HUD base font — settings panel persists via QSettings
    hud_panel_pct: float = 94.0        # HUD panel solidness (transparency knob; LGI_HUD_OPACITY
                                       # accepts legacy 0..1 floats or 40..100 percentages)
    speak_responses: bool = True
    speak_alerts: bool = False         # verbal SUPERVISOR_ALERT speech (visual alert always fires); 🔇 default + HUD 🔊 toggle
    duck_while_speaking: bool = False   # OFF by default: playback must never stall the mic
    # -- v1.6.1 talk-window self-echo guard ---------------------------------- #
    echo_guard: bool = True             # drop near-verbatim TTS-echo transcripts
    echo_guard_tail_s: float = 1.5     # grace seconds after playback finish
    echo_guard_retain_s: float = 20.0  # late-finish exact-match retention window
    echo_guard_ratio: float = 0.75     # SequenceMatcher fuzzy-match threshold
    screen_capture_enabled: bool = True
    webcam_enabled: bool = True
    vision_chat_enabled: bool = True    # attach dashcam frame to visual utterances
    vision_fps: float = 1.0             # ScreenRoller dashcam rate (frames/s)
    vision_width: int = 1920            # max JPEG width sent on vision turns
    vision_quality: int = 70            # JPEG quality sent on vision turns
    ocr_enabled: bool = True            # local Tesseract OCR context (if installed)
    hotkey_audit: str = "ctrl+shift+s"
    hotkey_mic: str = "ctrl+shift+m"
    alert_led_hold_s: float = 60.0
    # -- v1.2 webcam perception ("Her Seeing Me") -------------------------- #
    webcam_tracking: bool = True       # continuous perception stack (camera LED on)
    gestures_enabled: bool = True       # palm = mic toggle, fist = media play/pause
    taught_gestures: bool = True        # v1.6.0: taught-gesture recognition + voice lessons
    attention_gate: bool = True        # supervisor audits gated on operator attention
    radar_enabled: bool = True         # spatial telemetry -> Burst Receiver :8083
    radar_url: str = "http://192.168.50.51:8083"
    webcam_fps: float = 15.0          # perception capture rate (frames/s)
    vlm_enabled: bool = True          # webcam-analyze turns via Ollama
    vlm_url: str = "http://192.168.50.51:11434"
    vlm_model: str = "polaris-ai:latest"
    vlm_timeout_s: float = 90.0

    @classmethod
    def from_env(cls) -> "LGIConfig":
        def env_str(key: str, default: str) -> str:
            return os.environ.get(key) or default

        def env_bool(key: str, default: bool) -> bool:
            raw = os.environ.get(key)
            return default if raw is None else raw.strip().lower() in ("1", "true", "yes", "on")

        def env_float(key: str, default: float) -> float:
            raw = os.environ.get(key)
            try:
                return float(raw) if raw else default
            except ValueError:
                return default

        def env_int(key: str, default: int) -> int:
            raw = os.environ.get(key)
            try:
                return int(raw) if raw else default
            except ValueError:
                return default

        return cls(
            polaris_url=env_str("LGI_POLARIS_URL", cls.polaris_url),
            operator_user=env_str("LGI_OPERATOR_USER", cls.operator_user),
            wake_word=env_str("LGI_WAKE_WORD", cls.wake_word).lower(),
            wake_word_required=env_bool("LGI_WAKE_REQUIRED", cls.wake_word_required),
            talk_window_s=env_float("LGI_TALK_WINDOW", cls.talk_window_s),
            stt_model=env_str("LGI_STT_MODEL", cls.stt_model),
            stt_device=env_str("LGI_STT_DEVICE", cls.stt_device),
            stt_compute=env_str("LGI_STT_COMPUTE", cls.stt_compute),
            tts_device=env_str("LGI_TTS_DEVICE", cls.tts_device),
            audit_interval_s=env_float("LGI_AUDIT_INTERVAL", cls.audit_interval_s),
            heartbeat_s=env_float("LGI_HEARTBEAT", cls.heartbeat_s),
            feed_enabled=env_bool("LGI_FEED", cls.feed_enabled),
            feed_per_side=env_int("LGI_FEED_PER_SIDE", cls.feed_per_side),
            feed_fallback_s=env_float("LGI_FEED_FALLBACK", cls.feed_fallback_s),
            hud_font_px=env_int("LGI_HUD_FONT", cls.hud_font_px),
            hud_panel_pct=_env_opacity_pct("LGI_HUD_OPACITY", cls.hud_panel_pct),
            duck_while_speaking=env_bool("LGI_DUCK_WHILE_SPEAKING", cls.duck_while_speaking),
            echo_guard=env_bool("LGI_ECHO_GUARD", cls.echo_guard),
            echo_guard_tail_s=env_float("LGI_ECHO_GUARD_TAIL", cls.echo_guard_tail_s),
            echo_guard_retain_s=env_float("LGI_ECHO_GUARD_RETAIN", cls.echo_guard_retain_s),
            screen_capture_enabled=env_bool("LGI_SCREENSHOT", cls.screen_capture_enabled),
            webcam_enabled=env_bool("LGI_WEBCAM", cls.webcam_enabled),
            vision_chat_enabled=env_bool("LGI_VISION_CHAT", cls.vision_chat_enabled),
            vision_fps=env_float("LGI_VISION_FPS", cls.vision_fps),
            vision_width=env_int("LGI_VISION_WIDTH", cls.vision_width),
            vision_quality=env_int("LGI_VISION_QUALITY", cls.vision_quality),
            ocr_enabled=env_bool("LGI_OCR", cls.ocr_enabled),
            speak_alerts=env_bool("LGI_SPEAK_ALERTS", cls.speak_alerts),
            webcam_tracking=env_bool("LGI_WEBCAM_TRACK", cls.webcam_tracking),
            gestures_enabled=env_bool("LGI_GESTURES", cls.gestures_enabled),
            taught_gestures=env_bool("LGI_TAUGHT_GESTURES", cls.taught_gestures),
            attention_gate=env_bool("LGI_ATTENTION_GATE", cls.attention_gate),
            radar_enabled=env_bool("LGI_RADAR", cls.radar_enabled),
            radar_url=env_str("LGI_RADAR_URL", cls.radar_url),
            webcam_fps=env_float("LGI_WEBCAM_FPS", cls.webcam_fps),
            vlm_enabled=env_bool("LGI_VLM", cls.vlm_enabled),
            vlm_url=env_str("LGI_VLM_URL", cls.vlm_url),
            vlm_model=env_str("LGI_VLM_MODEL", cls.vlm_model),
            vlm_timeout_s=env_float("LGI_VLM_TIMEOUT", cls.vlm_timeout_s),
        )


VOICE_STOP_COMMANDS = {"stop talking", "be quiet", "quiet please", "silence",
                      "stop voice", "shut up"}

# v1.4: strip controls — voice-matched pause/resume of Polaris speech (same
# interception path as VOICE_STOP_COMMANDS; fires regardless of wake window).
VOICE_PAUSE_COMMANDS = {"pause talking", "hold on", "hold your speech",
                        "pause voice"}
VOICE_RESUME_COMMANDS = {"resume talking", "resume speech", "resume voice"}

# v1.3: spoken commands that close the conversation talk window early (matched
# after wake-word strip, or directly while the window is open). Plain "sleep"
# after the wake word ("polaris, sleep") also closes it.
TALK_WINDOW_CLOSE_COMMANDS = {"go to sleep", "end the conversation",
                              "stop the conversation", "close the talk window"}

# Desktop-vision trigger: utterances that reference what is on screen attach the
# latest dashcam frame to the chat payload (Polaris Desktop Vision). Matched on
# the lowercased utterance — tune the pattern here.
VISION_INTENT_RE = re.compile(
    r"\b(?:"
    r"look (?:at|on|over)"                                            # look at this layout
    r"|(?:check|review|describe|inspect|see) (?:this|that|the|my)"   # check this CSS
    r"|what(?:'?s| is)? (?:wrong|missing|off|broken|unclear)"        # what's wrong with...
    r"|what do you see"
    r"|(?:this|that|the|my) (?:screen|layout|window|page|css|code|ui|dashboard"
    r"|design|form|table|chart|graph|site|app|monitor)"
    r"|on (?:my|the) screen"
    r"|(?:nav|navigation|top|menu|tab) (?:bar|menu|tabs|strip)"      # top navigation bar
    r"|(?:screen ?shot|screen capture)"
    r")\b"
)

# Webcam-analyze trigger (v1.2 "Her Seeing Me"): utterances that ask Polaris
# to look through the WEBCAM at the physical world. Checked BEFORE
# VISION_INTENT_RE — camera phrases are more specific than screen phrases
# ("what do you see" alone remains a desktop-vision phrase).
WEBCAM_ANALYZE_RE = re.compile(
    r"\b(?:"
    r"analyze (?:this|the|my) (?:component|part|object|thing|item|board|hardware|device)"
    r"|what(?:'?s| is)? (?:in front of|around|beside) (?:you|me)"
    r"|what am i holding"
    r"|who(?:'?s| is| are) (?:there|behind me)"
    r"|look at (?:me|my hands?|my desk)"
    r"|describe (?:what|who)(?:'?s| is)? (?:in front of|around) (?:you|me)"
    r"|use (?:your|the) (?:webcam|camera|eyes)"
    r"|what (?:do|does) (?:you|the camera) see (?:on|with|through|in) (?:your|the) (?:webcam|camera)"
    r"|camera (?:view|check)"
    r")\b"
)


class LGIApp(QObject):
    """Wires all LGI engines together and owns the Qt event loop glue."""

    def __init__(self, cfg: LGIConfig) -> None:
        super().__init__()
        self.cfg = cfg
        self._stop = threading.Event()

        # Event queues (worker threads -> Qt main-thread drain timer)
        self.status_q: Queue = Queue()       # (target, state) LED updates
        self.command_q: Queue = Queue()      # "mic" | "audit" | "stop_voice" | "pause_voice" | "resume_voice" | "media" | "alert_voice" | "taught:<name>:<action>"
        self.stt_q: Queue = Queue()          # final utterance text
        self.chat_q: Queue = Queue()         # ("chat" | "webcam", user_text, result)
        self.supervisor_q: Queue = Queue()   # audit result dicts
        self.lesson_q: Queue = Queue()       # ("say" | "note", text) — v1.6.0 gesture lessons

        # Engines
        self.client = PolarisClient(base_url=cfg.polaris_url, log=_log)

        self.hud = HUDWindow(font_px=cfg.hud_font_px, panel_pct=cfg.hud_panel_pct)
        self.hud.on_mic_toggle = self._on_mic_toggle
        self.hud.on_force_audit = self._force_audit
        self.hud.on_stop_voice = self._stop_voice
        self.hud.on_pause_voice = self._pause_voice
        self.hud.on_resume_voice = self._resume_voice
        self.hud.on_quit = self._quit
        self.hud.on_toggle_alert_voice = self._toggle_alert_voice
        self.hud.set_alert_voice_button(cfg.speak_alerts)  # boot state: 🔇 default / LGI_SPEAK_ALERTS
        self.hud.on_vault_browse = self._on_vault_browse
        self.hud.on_vault_search = self._on_vault_search
        self.hud.on_vault_open = self._on_vault_open

        # v1.4 always-listening feed: ONE Socket.IO connection to the gateway
        # identity room (chat seed/finals/deltas) + 'status' room (task feed).
        # Pure queue-funnel worker — UI writes stay on the drain timer.
        self.store = ConversationStore(per_side=cfg.feed_per_side, log=_log)
        self.feed_q: Queue = Queue()
        self.vault_q: Queue = Queue()
        self.feed = (
            GatewayFeedClient(base_url=cfg.polaris_url, identity=cfg.operator_user,
                              feed_q=self.feed_q, status_q=self.status_q, log=_log)
            if cfg.feed_enabled else None
        )

        self.tts = KokoroTTS(
            voice=cfg.tts_voice, speed=cfg.tts_speed, device=cfg.tts_device,
            status_callback=lambda state: self.status_q.put(("tts", state)),
            log=_log,
        )
        self.listener = MicListener(
            model_size=cfg.stt_model, device=cfg.stt_device,
            compute_type=cfg.stt_compute,
            final_text_callback=lambda text: self.stt_q.put(text),
            status_callback=lambda state: self.status_q.put(("mic", state)),
            log=_log,
        )
        # v1.6.0 — gesture teaching: landmark-capture/label voice lessons over
        # the perception stack; runtime taught-gesture recognition behind
        # LGI_TAUGHT_GESTURES (profiles persist in taught_gestures.json).
        # Speech/prompts funnel through lesson_q → Qt drain (TTS + HUD stay
        # on the Qt main thread); fires return via "taught:<name>:<action>".
        self.teacher = (
            GestureTeacher(
                base_dir=os.path.dirname(os.path.abspath(__file__)),
                enabled=cfg.taught_gestures,
                say=lambda text: self.lesson_q.put(("say", text)),
                note=lambda text: self.lesson_q.put(("note", text)),
                dispatch=lambda name, action: self.command_q.put(
                    f"taught:{name}:{action}"),
                log=_log,
            )
            if cfg.webcam_tracking else None
        )
        # v1.2 — continuous webcam perception ("Her Seeing Me"); constructed
        # before the supervisor so it can share frames and gate audits.
        self.perception = (
            WebcamPerception(
                fps=cfg.webcam_fps,
                gestures_enabled=cfg.gestures_enabled,
                teacher=self.teacher,
                taught_enabled=cfg.taught_gestures,
                radar_enabled=cfg.radar_enabled,
                radar_url=cfg.radar_url,
                command_callback=lambda cmd: self.command_q.put(f"{cmd}:gesture"),
                state_callback=lambda state: self.status_q.put(("cam", state)),
                log=_log,
            )
            if cfg.webcam_tracking else None
        )
        self.vlm = (
            LocalVLM(base_url=cfg.vlm_url, model=cfg.vlm_model,
                     timeout_s=cfg.vlm_timeout_s, log=_log)
            if cfg.vlm_enabled else None
        )
        self.vision = VisionSupervisor(
            client=self.client, interval_s=cfg.audit_interval_s, user=cfg.operator_user,
            screen_capture_enabled=cfg.screen_capture_enabled,
            webcam_enabled=cfg.webcam_enabled,
            webcam_provider=self.perception,
            attention_gate=cfg.attention_gate,
            result_callback=lambda result: self.supervisor_q.put(result),
            status_callback=lambda state: self.status_q.put(("supervisor", state)),
            log=_log,
        )

        self.screen = (
            ScreenRoller(fps=cfg.vision_fps, log=_log) if cfg.vision_chat_enabled else None
        )

        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="lgi-chat")
        self._heartbeat_thread: Optional[threading.Thread] = None
        self._drain_timer = QTimer(self)
        self._drain_timer.setInterval(100)
        self._drain_timer.timeout.connect(self._drain)
        # v1.3.1 — Qt main-thread liveness tick: one console line per minute; a
        # gap while STT/audit traces keep flowing proves a frozen Qt drain.
        self._qt_ticks = 0
        self._tick_timer = QTimer(self)
        self._tick_timer.setInterval(60_000)
        self._tick_timer.timeout.connect(self._qt_tick)
        self._alert_epoch = 0
        self._mic_muted_by_user = False

        # Conversation talk window (v1.3/v1.4.2): monotonic deadline,
        # 0 ⇒ closed, opened SILENTLY by the bare wake word and every reply.
        self._talk_until = 0.0
        # v1.6.4: mic-mute freeze — when the operator's mic is muted (HUD
        # button / hotkey / gesture) the wake-free countdown is suspended:
        # remaining seconds are banked here and restored from the unmute
        # moment, so a mute mid-conversation can never silently burn the
        # window away.
        self._talk_frozen_remaining: Optional[float] = None
        # v1.6.4: previous open-state for the drain-timer expiry cue — the
        # lapse back to ambient listening is announced once (console + HUD).
        self._talk_open_prev = False
        self._last_chat_user = ""   # v1.4.2: left side for tool-report exchange swaps
        # Wake-word prefix tolerant to punctuation right after the name —
        # whisper renders direct address as "Polaris, …" far more often than
        # "Polaris …", and the old exact-space prefix silently demoted those
        # utterances to ambient-only (the "have to say it twice" symptom).
        _w = re.escape(cfg.wake_word)
        self._wake_prefix_re = re.compile(
            rf"^(?:hey\s+)?{_w}\s*[,.!?;:]*\s+", re.IGNORECASE)
        self._wake_solo_re = re.compile(
            rf"^(?:hey\s+)?{_w}\s*[,.!?;:]*$", re.IGNORECASE)

    # -- lifecycle ---------------------------------------------------------- #
    def start(self) -> None:
        _log(f"starting — gateway {self.cfg.polaris_url} (user '{self.cfg.operator_user}')")
        self.hud.set_led("polaris", "unknown")
        self.tts.start()          # pipeline warms up off-thread
        self.listener.start()
        if self.screen:
            self.screen.start()    # rolling dashcam (dxcam, 1 FPS default)
        if self.perception:
            self.perception.start()   # webcam stack — camera LED stays ON
            _log("webcam perception running — say 'Polaris, analyze this "
                 "component'; LGI_WEBCAM_TRACK=0 reverts to legacy capture")
        self.vision.start()
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat, name="lgi-heartbeat", daemon=True
        )
        self._heartbeat_thread.start()
        if self.feed:
            self.feed.start()
            _log("always-listening feed starting — identity room + task feed "
                 f"(window: {self.cfg.feed_per_side} per side)")
            if self.cfg.feed_fallback_s > 0:
                self._feed_fallback_thread = threading.Thread(
                    target=self._feed_fallback_loop, name="lgi-feed-fallback", daemon=True)
                self._feed_fallback_thread.start()
        self._register_hotkeys()
        self._drain_timer.start()
        self._tick_timer.start()
        self.hud.show()
        _log("LGI online. Say 'Polaris <question>' — Ctrl+Shift+S forces an audit.")

    def shutdown(self) -> None:
        _log("shutting down…")
        self._stop.set()
        self._drain_timer.stop()
        if self.feed:
            self.feed.stop()
        engines = [self.listener, self.vision, self.tts]
        if self.screen:
            engines.append(self.screen)
        if self.perception:
            engines.append(self.perception)
        for engine in engines:
            try:
                engine.stop()
            except Exception:
                pass
        try:
            self.tts.shutdown()
        except Exception:
            pass
        self.executor.shutdown(wait=False)
        _log("LGI stopped.")

    # -- queue drain (Qt main thread only) ---------------------------------- #
    def _drain(self) -> None:
        # v1.6.4: the talk window now lapses OUT LOUD — an open→closed edge
        # (expiry, not a spoken "sleep" close) is announced once in the
        # console + HUD ambient line, so the operator can see that the
        # session returned to wake-word-only ambient listening.
        now_open = self._talk_window_open()
        if self._talk_open_prev and not now_open:
            _log("[talk] window expired — ambient listening (say 'Polaris')")
            self.hud.set_ambient("(ambient listening — say 'Polaris')")
        self._talk_open_prev = now_open
        try:
            while True:
                target, state = self.status_q.get_nowait()
                if target == "tts":
                    self._on_tts_state(state)
                else:
                    self.hud.set_led(target, state)
                    if target == "mic":
                        self.hud.set_mic_button(state == "listening")
        except Empty:
            pass
        try:
            while True:
                self._dispatch_command(self.command_q.get_nowait())
        except Empty:
            pass
        try:
            while True:
                self._dispatch_utterance(self.stt_q.get_nowait())
        except Empty:
            pass
        try:
            while True:
                _kind, user_text, result = self.chat_q.get_nowait()
                if _kind == "webcam":
                    self._handle_webcam_result(user_text, result)
                else:
                    self._handle_chat_result(user_text, result)
        except Empty:
            pass
        try:
            while True:
                self._handle_audit(self.supervisor_q.get_nowait())
        except Empty:
            pass
        try:
            while True:
                self._dispatch_feed_event(self.feed_q.get_nowait())
        except Empty:
            pass
        try:
            while True:
                kind, arg, category, result = self.vault_q.get_nowait()
                self._dispatch_vault_result(kind, arg, category, result)
        except Empty:
            pass
        try:
            while True:
                kind, text = self.lesson_q.get_nowait()
                self._handle_lesson_event(kind, text)
        except Empty:
            pass

    def _qt_tick(self) -> None:
        """v1.3.1 Qt main-thread liveness — console heartbeat for blackout
        forensics (drain/HUD freeze vs STT-side silence)."""
        self._qt_ticks += 1
        _log(f"[qt] main-thread tick {self._qt_ticks}")

    # -- conversation talk window (v1.3) -------------------------------------- #
    def _talk_window_open(self) -> bool:
        """True while LGI_TALK_WINDOW keeps wake-free conversation active.
        v1.6.4: while the mic is user-muted the countdown is FROZEN — the
        banked remaining seconds keep the window alive past its deadline."""
        if self._talk_frozen_remaining is not None:
            return self._talk_frozen_remaining > 0.0
        return (self.cfg.talk_window_s > 0
                and time.monotonic() < self._talk_until)

    def _talk_window_renew(self) -> None:
        """Open or renew the talk window after a successful Polaris exchange.
        v1.4.2: every open/renew is silent — no spoken cue, HUD countdown only.
        v1.6.4: a renew landing while muted banks a fresh window for unmute."""
        if self._talk_frozen_remaining is not None:
            self._talk_frozen_remaining = self.cfg.talk_window_s
            _log(f"[talk] muted — fresh {int(self.cfg.talk_window_s)}s window "
                 "banked for unmute ('polaris, sleep' still closes early)")
            return
        self._talk_until = time.monotonic() + self.cfg.talk_window_s
        _log(f"[talk] talk window active for {int(self.cfg.talk_window_s)}s "
             f"('polaris, sleep' closes it)")

    def _talk_window_close(self) -> None:
        """Close the window early via spoken command — HUD note only, no TTS."""
        self._talk_until = 0.0
        self._talk_frozen_remaining = None   # v1.6.4: a close drops any bank too
        self._talk_open_prev = False         # v1.6.4: no expiry cue after a close
        _log("[talk] talk window closed — wake word required again")
        self.hud.set_ambient("(talk window closed — say 'Polaris')")

    # -- v1.6.1 talk-window self-echo guard ----------------------------------- #
    # The open mic hears Polaris's own Kokoro playback (principle 6: ducking is
    # off, playback never stalls the mic). While the conversation talk window
    # is open those transcripts route wake-free straight into /api/chat (the
    # v1.3 design), so a report spoken once came back as two "user" turns:
    # near-verbatim TTS-echo transcripts are dropped BEFORE the routing below.
    # Barge-in commands are matched earlier and unaffected; genuine speech
    # never near-verbatim-duplicates the text that was just spoken; the
    # gateway double-emit (§7) stays store-only and is deliberately untouched.
    @staticmethod
    def _echo_norm(text: str) -> str:
        """Normalize for echo comparison — lower, single-spaced, outer punctuation stripped."""
        t = re.sub(r"\s+", " ", (text or "").lower()).strip()
        return t.strip(" .,!?;:\"'’”…")

    def _echo_close(self, transcript: str, spoken: str) -> bool:
        """Near-verbatim test — exact match, long containment, or fuzzy ratio."""
        if not transcript or not spoken:
            return False
        if transcript == spoken:
            return True
        short = min(transcript, spoken, key=len)
        if len(short) >= 15 and (transcript in spoken or spoken in transcript):
            return True
        tail = spoken[-160:]
        if len(transcript) >= 15 and (transcript in tail or tail in transcript):
            return True
        if len(transcript) >= 20:
            if SequenceMatcher(None, transcript, spoken).ratio() >= self.cfg.echo_guard_ratio:
                return True
            if len(tail) >= 20 and SequenceMatcher(
                    None, transcript, tail).ratio() >= self.cfg.echo_guard_ratio:
                return True
        return False

    def _is_self_echo(self, norm: str) -> bool:
        """v1.6.1 — True when `norm` is near-verbatim audio just voiced by the TTS."""
        tts = getattr(self, "tts", None)
        if tts is None:
            return False
        transcript = self._echo_norm(norm)
        if not transcript:
            return False
        now = time.monotonic()

        # (1) actively speaking — compare against the utterance in playback now
        try:
            active = tts.is_speaking
        except Exception:
            active = False
        candidates = []
        if active:
            cur = self._echo_norm(tts.current_text)
            if cur:
                candidates.append(cur)

        # (2) just finished — inside the tail window the mic may still be
        #     finalizing the very thing Polaris said (matches the last few
        #     utterances joined, so split/skewed STT finals also cancel).
        finished_at = float(getattr(tts, "finished_at", 0.0) or 0.0)
        rec = self._echo_norm(tts.spoken_recent_text())
        if finished_at and (now - finished_at) <= self.cfg.echo_guard_tail_s and rec:
            candidates.append(rec)

        for cand in candidates:
            if self._echo_close(transcript, cand):
                return True

        # (3) late-finish retention — exact only (skews longer than the tail)
        if (finished_at and (now - finished_at) <= self.cfg.echo_guard_retain_s
                and rec and transcript == rec):
            return True
        return False

    # -- utterance routing ---------------------------------------------------- #
    def _dispatch_utterance(self, text: str) -> None:
        text = " ".join(str(text or "").split()).strip()
        if not text:
            return
        norm = text.lower().rstrip(" .!?,")

        if norm in VOICE_STOP_COMMANDS:
            self._stop_voice()
            self.hud.set_ambient(f"{text}  (voice stopped)")
            return
        if norm in VOICE_PAUSE_COMMANDS:
            self._pause_voice()
            self.hud.set_ambient(f"{text}  (voice paused)")
            return
        if norm in VOICE_RESUME_COMMANDS:
            self._resume_voice()
            self.hud.set_ambient(f"{text}  (voice resumed)")
            return

        # v1.6.1 talk-window self-echo guard — before ANY routing (wake regex,
        # teacher hook, talk window): our own spoken reply must not re-enter.
        if self.cfg.echo_guard and self._is_self_echo(norm):
            _log(f"[voice] echo-dropped text={text[:160]!r}")
            self.hud.set_ambient("(self-echo suppressed — not sent)")
            return

        m = self._wake_prefix_re.match(norm)
        body = text[m.end():].strip() if m else ""
        solo = not m and bool(self._wake_solo_re.match(norm))
        in_window = self._talk_window_open()
        # v1.3.1 — utterance routing trace (machine-local console only): every
        # final transcription plus its verdict, so silence is diagnosable.
        _log(f"[voice] text={text[:100]!r} wake={bool(m)} solo={solo} "
             f"in_window={in_window}")

        # v1.6.0 — gesture-teaching hook: while a lesson is live it owns every
        # utterance (no wake word — the operator is mid-lesson); when idle it
        # claims only the explicit teach/forget/list phrases, gated by the
        # same wake/window policy as chat. ("Sleep" mid-lesson cancels it —
        # handled inside the teacher — so a label can never be "sleep".)
        if self.teacher is not None:
            lesson_text = body if m else text
            if self.teacher.active:
                if self.teacher.handle_utterance(lesson_text):
                    return
            elif m or in_window or not self.cfg.wake_word_required:
                if self.teacher.handle_utterance(lesson_text):
                    return

        # "polaris, sleep" — or a bare close phrase while the window is open —
        # ends wake-free conversation early.
        spoken = body.lower() if m else (norm if solo else "")
        if spoken == "sleep" or spoken in TALK_WINDOW_CLOSE_COMMANDS:
            if m or solo or in_window:
                self._talk_window_close()
                return

        if solo:  # v1.4.2: bare wake word = SILENT wake-free window (was spoken
                  # "standing by"; follow-ups without the wake word now transmit)
            self._talk_until = time.monotonic() + self.cfg.talk_window_s
            _log(f"[talk] wake word alone — silent {int(self.cfg.talk_window_s)}s "
                 "wake-free window ('polaris, sleep' closes it)")
            self.hud.set_ambient("(listening — no wake word needed)")
            return
        if self.cfg.wake_word_required and not m and not in_window:
            self.hud.set_ambient(text)  # ambient transcript only — never sent
            return

        payload = body if m else text  # wake-strip when present; raw in-window
        if self._webcam_analyze_intent(text):
            self._send_webcam_analysis(payload)
        else:
            self._send_chat(payload, vision=self._vision_intent(text))

        if in_window:  # HUD countdown of the remaining wake-free time
            remaining = max(0, int(self._talk_until - time.monotonic()))
            self.hud.set_ambient(f"(talk window {remaining // 60}:{remaining % 60:02d})")

    def _vision_intent(self, text: str) -> bool:
        """True when the utterance references what is on screen (VISION_INTENT_RE)."""
        if not self.cfg.vision_chat_enabled or self.screen is None:
            return False
        return bool(VISION_INTENT_RE.search(text.lower()))

    def _gather_vision_context(self) -> tuple:
        """Dashcam JPEG + OCR text + active window title for a vision turn."""
        if self.screen is None:
            return "", "", ""
        image_b64 = self.screen.latest_jpeg_b64(
            max_width=self.cfg.vision_width, quality=self.cfg.vision_quality)
        ocr_text = self.screen.ocr_text() if self.cfg.ocr_enabled else ""
        active_window = ""
        try:
            import pygetwindow
            window = pygetwindow.getActiveWindow()
            if window is not None:
                active_window = str(window.title)[:200]
        except Exception:
            pass
        return image_b64, ocr_text, active_window

    def _webcam_analyze_intent(self, text: str) -> bool:
        """True when the utterance asks Polaris to look through the webcam."""
        if self.vlm is None:
            return False
        return bool(WEBCAM_ANALYZE_RE.search(text.lower()))

    def _webcam_frame_b64(self) -> str:
        """Latest perception frame, or a legacy one-shot when the stack is off."""
        if self.perception is not None:
            jpeg = self.perception.latest_jpeg_b64(max_width=640, quality=50)
            if jpeg:
                return jpeg
        return one_shot_jpeg_b64(max_width=640, quality=50)

    def _send_webcam_analysis(self, text: str) -> None:
        if not text:
            return
        self.hud.set_exchange(f"{text} 👁", "… looking")

        def work() -> None:
            result = self.vlm.analyze(self._webcam_frame_b64())
            self.chat_q.put(("webcam", text, result))

        self.executor.submit(work)

    def _handle_webcam_result(self, user_text: str, result: Dict[str, Any]) -> None:
        display = f"{user_text} 👁"
        if result.get("ok"):
            response = str(result.get("response", ""))
            self.hud.set_exchange(display, response)
            self._talk_window_renew()
            _log(f"[webcam] {self.vlm.model}: {response[:120]}")
            if self.cfg.speak_responses:
                self.tts.speak(response)
        else:
            error = str(result.get("error", "unknown webcam analysis error"))
            self.hud.set_exchange(display, f"⚠ webcam: {error}")
            self.tts.speak("I could not analyze the camera view.")
            _log(f"[webcam] {error}")

    def _send_chat(self, text: str, vision: bool = False) -> None:
        if not text:
            return
        image_b64 = ocr_text = active_window = ""
        if vision:
            image_b64, ocr_text, active_window = self._gather_vision_context()
            if not image_b64:
                _log("[vision] no frame available — sending text-only prompt")
        self.hud.set_exchange(
            f"{text} 📷" if image_b64 else text,
            "… looking" if image_b64 else "… thinking")

        def work() -> None:
            result = self.client.chat(
                text,
                image_b64=image_b64 or None,
                ocr_text=ocr_text or None,
                active_window=active_window or None,
            )
            self.chat_q.put(("chat", text, result))

        self.executor.submit(work)

    def _handle_chat_result(self, user_text: str, result: Dict[str, Any]) -> None:
        self._last_chat_user = user_text   # v1.4.2: swap-in text for tool reports
        if result.get("ok"):
            response = str(result.get("response", ""))
            data = result.get("data") or {}
            if data.get("vision"):
                _log(f"[chat] vision turn answered by {data.get('vision_model', 'unknown')}")
            self.hud.set_exchange(user_text, response)
            self._talk_window_renew()
            if self.cfg.speak_responses:
                self.tts.speak(response)
        else:
            error = str(result.get("error", "unknown gateway error"))
            self.hud.set_exchange(user_text, f"⚠ gateway: {error}")
            _log(f"[chat] {error}")

    # -- always-listening feed (v1.4) ----------------------------------------- #
    def _dispatch_feed_event(self, event: Tuple) -> None:
        """Route one GatewayFeedClient event into the HUD (Qt main thread).
        The rolling window is 100% feed-driven; LGI's own voice exchanges
        arrive through the same identity-room broadcast, so there is exactly
        one writer per surface — no echo-dedupe on the top exchange."""
        kind = event[0]
        if kind == "history":
            rows = event[1] if len(event) > 1 else []
            self.store.seed(rows)
            self.hud.set_history(self.store.visible())
        elif kind == "chat_message":
            msg = event[1] if len(event) > 1 else {}
            if self.store.apply_chat_message(msg):
                self.hud.set_history(self.store.visible())
        elif kind == "stream":
            _, stream_id, chunk, is_complete = event
            if self.store.apply_stream(stream_id, chunk, bool(is_complete)):
                self.hud.set_history(self.store.visible())
        elif kind == "tasks":
            self.hud.set_tasks_snapshot(event[1] if len(event) > 1 else [])
        elif kind == "task":
            self.hud.append_task(str(event[1]), event[2] if len(event) > 2 else {})
        elif kind == "tool":
            # v1.4.2: gateway tool runs render as ⚙ CHAT_TOOL rows in
            # ▼ TACTICAL OPERATIONS (was console-log only).
            self._render_tool_execution(event[1] if len(event) > 1 else {})
        elif kind == "tool_report":
            # v1.4.2: the truthful tool report from /api/chat's feedback loop
            # — spoken/exchanged only when this device asked for the tools.
            self._handle_tool_report(event[1] if len(event) > 1 else {})
        else:
            _log(f"[feed] unhandled event kind {kind!r}")

    def _render_tool_execution(self, tools: Dict) -> None:
        """⚙ chat_tool rows in ▼ TACTICAL OPERATIONS for gateway tool runs.
        One row per call; the matching result supplies an ok/error tail.
        Never speaks — tool chatter stays visual (other-device privacy rule)."""
        tools = tools if isinstance(tools, dict) else {}
        calls = tools.get("tool_calls") if isinstance(tools.get("tool_calls"), list) else []
        results = tools.get("results") if isinstance(tools.get("results"), list) else []
        when = str(tools.get("timestamp") or "")
        for i, call in enumerate(calls if calls else [{}]):
            name = str(call.get("name") or "tool") if isinstance(call, dict) else "tool"
            args = call.get("arguments") if isinstance(call, dict) else None
            if not isinstance(args, dict):
                args = {}
            arg_summary = ", ".join(f"{k}={str(v)[:40]}" for k, v
                                    in list(args.items())[:3]) or "—"
            res = results[i] if i < len(results) and isinstance(results[i], dict) else {}
            outcome = "ok" if res.get("success") else str(
                res.get("error") or res.get("reason") or "no result")
            self.hud.append_task("chat_tool", {
                "task_id": name,
                "description": f"{arg_summary} → {outcome}",
                "timestamp": when,
            })
        _log(f"[feed] tool_execution: "
             f"{', '.join(str(c.get('name')) for c in calls if isinstance(c, dict)) or 'unknown'}")

    def _handle_tool_report(self, msg: Dict) -> None:
        """Truthful tool report — speak + swap into the exchange ONLY when this
        device asked (origin_device == our DEVICE_ID); everything else stays
        silent per the never-speak-other-device rule."""
        msg = msg if isinstance(msg, dict) else {}
        origin = str(msg.get("origin_device") or "")
        content = str(msg.get("content") or msg.get("response") or "").strip()
        if not content:
            return
        if origin != "lgi-hud-x17":   # keep in sync with gateway_feed.DEVICE_ID
            _log(f"[feed] tool_report from {origin!r} — not ours, not spoken")
            return
        # Refresh the ⌁ window row + swap the truthful report into the main
        # exchange (the ack already occupied the right side; now corrected).
        self.store.apply_chat_message({
            "sender": "polaris", "content": content,
            "timestamp": str(msg.get("timestamp") or "")})
        self.hud.set_history(self.store.visible())
        self.hud.set_exchange(self._last_chat_user or "(tool report)", content)
        self._talk_window_renew()
        if self.cfg.speak_responses:
            self.tts.speak(content)

    def _feed_fallback_loop(self) -> None:
        """REST seed poller for feed outages: while the socket is down, refresh
        the paired history every feed_fallback_s so the window still advances
        (legacy freeroam safety net — WS push remains primary). Failures stay
        silent: the POL LED already reports gateway health."""
        interval = max(5.0, float(self.cfg.feed_fallback_s))
        while not self._stop.is_set():
            self._stop.wait(interval)
            if self._stop.is_set() or not self.feed or self.feed.connected:
                continue
            result = self.client.chat_history_paired(self.cfg.operator_user)
            if result.get("ok"):
                rows = parse_history_pairs(result.get("data"))
                if rows:
                    self.feed_q.put(("history", rows))

    # -- memory vault mirror (read-only, executor fan-out) --------------------- #
    def _on_vault_browse(self, category: str) -> None:
        self.hud.set_vault_state("browsing…")
        self.executor.submit(self._vault_work, "list", "", category or "")

    def _on_vault_search(self, query: str, category: str) -> None:
        query = str(query or "").strip()
        if not query:
            self._on_vault_browse(category)
            return
        self.hud.set_vault_state(f"searching '{query[:40]}'…")
        self.executor.submit(self._vault_work, "search", query, category or "")

    def _on_vault_open(self, path: str) -> None:
        path = str(path or "").strip()
        if not path:
            return
        self.hud.set_vault_state("opening doc…")
        self.executor.submit(self._vault_work, "doc", path, "")

    def _vault_work(self, kind: str, arg: str, category: str) -> None:
        """Executor-thread REST fan-out for the vault panels (never Qt-touching;
        results loop back through vault_q → _dispatch_vault_result)."""
        try:
            if kind == "list":
                result = self.client.vault_list(category=category)
            elif kind == "search":
                result = self.client.vault_search(
                    arg, categories=[category] if category else None)
            else:
                result = self.client.vault_doc(arg)
            self.vault_q.put((kind, arg, category, result))
        except Exception as exc:
            self.vault_q.put((kind, arg, category, {"ok": False, "error": str(exc)}))

    def _dispatch_vault_result(self, kind: str, arg: str, category: str,
                               result: Dict[str, Any]) -> None:
        if not result.get("ok"):
            self.hud.set_vault_state(f"✗ {str(result.get('error', 'vault error'))[:60]}")
            return
        data = result.get("data") or {}
        if kind == "list":
            docs = [d for d in (data.get("documents") or []) if isinstance(d, dict)]
            self.hud.set_vault_docs(docs)
            self.hud.set_vault_state(f"{len(docs)} docs • {category or 'ALL'}")
        elif kind == "search":
            docs = [d for d in (data.get("results") or []) if isinstance(d, dict)]
            self.hud.set_vault_docs(docs)
            self.hud.set_vault_state(f"{data.get('count', len(docs))} result(s) • "
                                     f"{category or 'all categories'}")
        else:
            doc = data.get("document") or {}
            meta = doc.get("meta") or {}
            title = str(meta.get("title") or doc.get("path") or arg)
            self.hud.set_vault_doc_view(
                str(doc.get("path") or arg), title,
                str(doc.get("content") or ""), str(doc.get("modified") or ""))
            self.hud.set_vault_state(f"opened {title[:44]}")

    # -- supervisor results ----------------------------------------------------- #
    def _handle_audit(self, result: Dict[str, Any]) -> None:
        if not result.get("ok"):
            self.hud.set_supervisor_line(f"audit failed — {str(result.get('error', ''))[:70]}")
            self.hud.set_led("supervisor", "idle")
            return

        verdict = str(result.get("verdict", "")).upper()
        data = result.get("data", {})
        category = str(data.get("category", "general"))
        triage = data.get("triage") or {}
        reason = ""
        for key in ("llm", "heuristic"):
            part = triage.get(key) if isinstance(triage, dict) else None
            if isinstance(part, dict) and part.get("reason"):
                reason = str(part["reason"])
                break

        if verdict == "SUPERVISOR_ALERT":
            _log(f"SUPERVISOR_ALERT [{category}] {reason}")
            self.hud.set_led("supervisor", "alert")
            self.hud.show_alert(category, reason)
            self.hud.set_supervisor_line(f"ALERT {category}")
            self._alert_epoch += 1
            epoch = self._alert_epoch
            QTimer.singleShot(int(self.cfg.alert_led_hold_s * 1000),
                              lambda: self._clear_alert(epoch))
            spoken = f"Supervisor alert. Category: {category}."
            if reason:
                spoken += f" {reason}"
            if self.cfg.speak_alerts:
                self.tts.speak(spoken)
        elif verdict == "LOG_ONLY":
            self.hud.set_led("supervisor", "ok")
            self.hud.set_supervisor_line(f"logged ({category})")
        else:  # NO_ACTION
            self.hud.set_led("supervisor", "ok")
            self.hud.set_supervisor_line("clear")

    def _clear_alert(self, epoch: int) -> None:
        if epoch == self._alert_epoch and not self._stop.is_set():
            self.hud.hide_alert()
            self.hud.set_led("supervisor", "idle")

    # -- gesture teaching (v1.6.0) ------------------------------------------- #
    def _handle_lesson_event(self, kind: str, text: str) -> None:
        """GestureTeacher prompts funnel here — TTS + HUD stay on the Qt main
        thread (the teacher runs on the drain + perception threads)."""
        label = str(text or "").strip()
        if not label:
            return
        self.hud.set_ambient(f"(teach) {label}")
        _log(f"[teach] {label}")
        if kind == "say" and self.cfg.speak_responses:
            self.tts.speak(label)

    def _dispatch_taught(self, name: str, action: str) -> None:
        """One taught-gesture fire: media | mic | stop_voice | chat action."""
        action = str(action or "chat").strip().lower() or "chat"
        name = str(name or "").strip()
        _log(f"[teach] gesture fired: '{name}' -> {action}")
        if action == "media":
            self._media_play_pause()
        elif action == "mic":
            self._on_mic_toggle(f"gesture:{name or 'taught'}")
        elif action == "stop_voice":
            self._stop_voice()
        else:
            self.hud.set_ambient(f"(gesture) {name}")
            self._send_chat(name)

    # -- commands / controls ------------------------------------------------------ #
    def _dispatch_command(self, command: str) -> None:
        # v1.3.2: commands may carry a ":source" suffix ("mic:hotkey",
        # "mic:gesture") so every mic log line records who muted/resumed.
        cmd, _, source = command.partition(":")
        if cmd == "mic":
            self._on_mic_toggle(source or "unattributed")
        elif cmd == "audit":
            self._force_audit()
        elif cmd == "stop_voice":
            self._stop_voice()
        elif cmd == "pause_voice":
            self._pause_voice()
        elif cmd == "resume_voice":
            self._resume_voice()
        elif cmd == "media":
            self._media_play_pause()
        elif cmd == "taught":
            # v1.6.0: "taught:<name>:<action>" from the taught-gesture stack
            name, _, action = source.partition(":")
            self._dispatch_taught(name, action)
        elif cmd == "alert_voice":
            self._toggle_alert_voice()

    def _on_mic_toggle(self, source: str = "hud") -> None:
        # v1.3.2: source attribution (HUD button / hotkey / gesture) — a
        # phantom mute is now assignable from the trace log alone.
        # v1.6.4: muting FREEZES the talk-window countdown (remaining banked
        # and suspended) and unmuting restores it from the unmute moment —
        # a mute mid-conversation can never silently burn the 180 s away.
        if self.listener.is_paused:
            self._mic_muted_by_user = False
            self.listener.resume()
            _log(f"mic resumed (source={source})")
            frozen = self._talk_frozen_remaining
            self._talk_frozen_remaining = None
            if frozen is not None and frozen > 0.0:
                self._talk_until = time.monotonic() + frozen
                mm, ss = int(frozen) // 60, int(frozen) % 60
                _log(f"[talk] window resumed — {mm}:{ss:02d} remaining frozen time")
                self.hud.set_ambient(f"(talk window {mm}:{ss:02d})")
        else:
            self._mic_muted_by_user = True
            self.listener.pause()
            _log(f"mic muted (source={source})")
            if self._talk_window_open():
                remaining = max(0.0, self._talk_until - time.monotonic())
                self._talk_frozen_remaining = remaining
                mm, ss = int(remaining) // 60, int(remaining) % 60
                _log(f"[talk] window paused at {mm}:{ss:02d} — countdown "
                     "frozen while mic muted")
                self.hud.set_ambient("(talk window paused — mic muted)")

    def _force_audit(self) -> None:
        _log("forcing supervisor audit")
        self.vision.trigger()

    def _stop_voice(self) -> None:
        self.tts.stop()
        _log("voice output stopped")

    def _pause_voice(self) -> None:
        self.tts.pause()
        _log("voice output paused")

    def _resume_voice(self) -> None:
        self.tts.resume()
        _log("voice output resumed")

    def _toggle_alert_voice(self) -> None:
        """🔇/🔊 Advisories — verbal SUPERVISOR_ALERT speech toggle (visual
        LED/banner/SUP line always fire on every alert verdict)."""
        self.cfg.speak_alerts = not self.cfg.speak_alerts
        self.hud.set_alert_voice_button(self.cfg.speak_alerts)
        _log(f"verbal advisories {'ON' if self.cfg.speak_alerts else 'OFF'}"
             " — visual alert channels unaffected")

    def _media_play_pause(self) -> None:
        """Media play/pause — fist gesture (best-effort OS media key)."""
        try:
            import keyboard
            keyboard.send("play/pause media")
            _log("media play/pause")
        except Exception as exc:
            _log(f"media key unavailable ({exc})")

    def _quit(self) -> None:
        self.hud.close()

    # -- optional ducking (pause STT while Polaris speaks) ------------------------- #
    def _on_tts_state(self, state: str) -> None:
        # v1.4: drive the ⏸/⏵ strip on every engine transition (disabled at
        # rest), then apply ducking. "paused" releases the mic — Polaris holds
        # silently, so the operator stays free to talk; resume re-ducks.
        self.hud.set_voice_playback(state)
        if not self.cfg.duck_while_speaking:
            return
        if state == "speaking":
            if not self.listener.is_paused:
                self.listener.pause()
                _log("[tts] ducking — mic paused while Polaris speaks")
        elif state in ("idle", "ready", "error", "paused"):
            if self.listener.is_paused and not self._mic_muted_by_user:
                self.listener.resume()
                _log("[tts] ducking released — mic resumed")

    # -- heartbeat / hotkeys -------------------------------------------------------- #
    def _heartbeat(self) -> None:
        while not self._stop.is_set():
            result = self.client.supervisor_status()
            self.status_q.put(("polaris", "online" if result.get("ok") else "offline"))
            self._stop.wait(self.cfg.heartbeat_s)

    def _register_hotkeys(self) -> None:
        try:
            import keyboard  # global hotkey hook (Windows)
            keyboard.add_hotkey(self.cfg.hotkey_audit, lambda: self.command_q.put("audit"))
            keyboard.add_hotkey(self.cfg.hotkey_mic,
                                lambda: self.command_q.put("mic:hotkey"))
            _log(f"hotkeys: {self.cfg.hotkey_audit} = force audit, "
                 f"{self.cfg.hotkey_mic} = toggle mic")
        except Exception as exc:
            _log(f"global hotkeys unavailable ({exc}) — HUD buttons still work")


def main() -> int:
    # v1.5.1: refuse a second instance BEFORE any GPU/audio/GUI init — two
    # concurrent sessions heap-corrupt the native stack within seconds.
    if not _acquire_instance_lock():
        return 0
    from PyQt6.QtWidgets import QApplication

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(True)
    lgi = LGIApp(LGIConfig.from_env())
    lgi.start()
    # v1.5.0 guarded-watchdog contract: the marker means "operator ignited LGI
    # and it has not exited cleanly". Written only after start() succeeds, so a
    # boot-time failure never trips the watchdog; cleared in the finally below
    # so a graceful quit stays down, while a crash/force-kill leaves it behind.
    _write_session_marker()
    try:
        return app.exec()
    finally:
        _clear_session_marker()
        lgi.shutdown()


if __name__ == "__main__":
    sys.exit(main())