"""
LGI — Looking Glass Interface
webcam_perception.py: Continuous webcam perception stack ("Her Seeing Me").

Design (v1.2 — the X17 webcam replaces MissPi as the sole spatial sensor):
  * Single device owner — cv2.VideoCapture(0, CAP_DSHOW) is opened once at
    start and held while LGI runs, so the camera hardware LED stays ON
    whenever tracking is enabled (honest light). LGI_WEBCAM_TRACK=0 falls
    back to the legacy per-audit open/release cycle with the LED off.
  * Rolling latest-frame buffer guarded by a lock (ScreenRoller pattern);
    latest_jpeg_b64() feeds vision-supervisor audits and webcam-analyze VLM
    turns without ever re-opening the device.
  * MediaPipe FaceMesh -> head-pose proxy -> user_attentive flag. Consumers
    apply fail-open semantics: None (unknown) never blocks anything.
  * MediaPipe Hands -> gesture control: palm held 2 s CONTINUOUSLY pushes
    "mic" (mic mute toggle), a held fist (1 s) pushes "media" (media
    play/pause). v1.3.2 hardening: dwell resets on ANY frame without a
    confirmed large hand pose (no cumulative dwell across flicker gaps), the
    fire path clears dwell even when cooldown-suppressed (a palm held in
    place can never double-toggle the mic), one fire per fresh dwell +
    6 s cooldown; LGI_GESTURES=0 disables.
  * MediaPipe Hands -> gesture control: palm held 2 s CONTINUOUSLY pushes
    "mic" (mic mute toggle), a held fist (1 s) pushes "media" (media
    play/pause). v1.3.2 hardening: dwell resets on ANY frame without a
    confirmed large hand pose (no cumulative dwell across flicker gaps), the
    fire path clears dwell even when cooldown-suppressed (a palm held in
    place can never double-toggle the mic), one fire per fresh dwell +
    6 s cooldown; LGI_GESTURES=0 disables.
  * v1.6.0 taught gestures — ONE shared Hands pass (`_update_hands`) feeds
    the built-in dwell logic AND the GestureTeacher (gesture_teaching.py):
    during a voice lesson it captures landmark signature frames; otherwise
    (LGI_TAUGHT_GESTURES + >=1 profile) it matches live hands against the
    taught profiles and dispatches "taught:<name>:<action>". With
    LGI_GESTURES=0 and nothing taught the Hands pass is skipped entirely
    (zero landmark processing — pre-v1.6.0 behavior).
  * YOLOv8n .track(persist=True, bytetrack.yaml) -> persistent person IDs,
    normalized x/y, per-ID delta velocity, bbox height as a depth proxy.
  * SpatialReporter (python-socketio client) -> Polaris Burst Receiver
    (:8083): vision_connect on (re)connect, vision_frame @ ~2 Hz,
    radar_motion @ ~1 Hz carrying explicit x/y/z/velocity + people_count
    (the receiver passes real coordinates through untouched; legacy MissPi
    intensity-driven payloads keep the synthetic path).
  * Degrade, never crash: every stage (camera, FaceMesh, Hands, YOLO,
    reporter) is independently guarded — a missing lib or busy camera
    degrades that stage only; the CAM LED drops to "degraded" / "off".

Thread model:
  * One daemon thread owns the camera; latest_jpeg_b64() / user_attentive /
    people_count may be called from any worker thread (lock-guarded state).
"""

from __future__ import annotations

import base64
import math
import threading
import time
from typing import Any, Callable, Dict, Optional

# v1.6.0 taught-gesture helpers — defensive import (a missing module only
# disables the taught pass; built-in gestures, tracking and radar continue).
try:
    from gesture_teaching import build_signature, match_best
except Exception:                                  # pragma: no cover
    build_signature = None                         # type: ignore[assignment]
    match_best = None                              # type: ignore[assignment]

RADAR_DEFAULT_URL = "http://192.168.50.51:8083"
DEVICE_ID = "x17-webcam"


class SpatialReporter:
    """socketio client shipping perception telemetry to the Burst Receiver."""

    def __init__(
        self,
        base_url: str = RADAR_DEFAULT_URL,
        resolution: str = "1280x720",
        fps: float = 15.0,
        log: Callable[[str], None] = print,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.resolution = resolution
        self.fps = fps
        self._log = log
        self._sio = None                  # socketio.Client (lazy import)
        self._stop = threading.Event()
        self._connected = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # -- lifecycle ------------------------------------------------------- #
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="lgi-radar", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        try:
            if self._sio is not None:
                self._sio.disconnect()
        except Exception:
            pass

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    # -- internals -------------------------------------------------------- #
    def _run(self) -> None:
        try:
            import socketio
        except ImportError:
            self._log("[radar] python-socketio missing — spatial reporting disabled")
            return
        self._sio = socketio.Client(reconnection=True, request_timeout=10)
        self._sio.on("connect", self._on_connect)
        while not self._stop.is_set():
            try:
                self._sio.connect(self.base_url)
                self._sio.wait()          # blocks while connected; exits on shutdown
            except Exception as exc:
                self._log(f"[radar] burst receiver unreachable ({exc}) — retrying in 10 s")
            self._stop.wait(10.0)
        self._connected.clear()

    def _on_connect(self) -> None:
        """Announce the stream (fires on every reconnect, receiver re-acks)."""
        self._connected.set()
        self.emit("vision_connect", {
            "device": DEVICE_ID,
            "camera_id": "front",
            "resolution": self.resolution,
            "fps": self.fps,
        })
        self._log(f"[radar] burst receiver link up ({self.base_url}) — vision_connect sent")

    # -- public ------------------------------------------------------------ #
    def emit(self, event: str, data: Dict[str, Any]) -> None:
        """Best-effort emit; drops the event when the link is down (never raises)."""
        sio = self._sio
        if sio is None:
            return
        try:
            if sio.connected:
                sio.emit(event, data)
        except Exception as exc:
            self._log(f"[radar] emit '{event}' failed: {exc}")



class WebcamPerception(threading.Thread):
    """Continuous webcam tracking, gesture control, and spatial reporting."""

    def __init__(
        self,
        device_index: int = 0,
        width: int = 1280,
        height: int = 720,
        fps: float = 15.0,
        gestures_enabled: bool = True,
        teacher: Optional[Any] = None,      # v1.6.0 GestureTeacher (lessons)
        taught_enabled: bool = True,        # LGI_TAUGHT_GESTURES switch
        gesture_palm_hold_s: float = 2.0,   # v1.3.2 hardened (was 1.5)
        gesture_fist_hold_s: float = 1.0,   # v1.3.2 hardened (was 0.6)
        gesture_cooldown_s: float = 6.0,    # v1.3.2 hardened (was 3.0)
        radar_enabled: bool = True,
        radar_url: str = RADAR_DEFAULT_URL,
        radar_motion_hz: float = 1.0,
        vision_frame_hz: float = 2.0,
        attention_yaw_deg: float = 28.0,
        attention_pitch_deg: float = 26.0,
        command_callback: Optional[Callable[[str], None]] = None,
        state_callback: Optional[Callable[[str], None]] = None,
        log: Callable[[str], None] = print,
    ) -> None:
        super().__init__(name="lgi-perception", daemon=True)
        self.device_index = device_index
        self.width = width
        self.height = height
        self.fps = max(1.0, fps)
        self.gestures_enabled = gestures_enabled
        self._teacher = teacher                    # v1.6.0 taught-gesture engine
        self._taught_enabled = taught_enabled
        self.palm_hold_s = gesture_palm_hold_s
        self.fist_hold_s = gesture_fist_hold_s
        self.cooldown_s = gesture_cooldown_s
        self.attention_yaw_deg = attention_yaw_deg
        self.attention_pitch_deg = attention_pitch_deg
        self.motion_hz = max(0.1, radar_motion_hz)
        self.vframe_hz = max(0.1, vision_frame_hz)
        self._command = command_callback
        self._state = state_callback
        self._log = log

        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._cap = None
        self._frame = None
        self._frame_ts: float = 0.0
        self._attentive: Optional[bool] = None
        self._people: Dict[int, Dict[str, Any]] = {}
        self._facemesh = None
        self._hands = None
        self._yolo = None
        self._frame_count = 0
        self._mode = "off"

        # gesture dwell bookkeeping
        self._palm_since: Optional[float] = None
        self._fist_since: Optional[float] = None
        self._gesture_last: float = 0.0

        # v1.6.0 taught-gesture recognition bookkeeping: one fire per
        # continuous hold, per-profile cooldown, pose must leave/re-enter to
        # re-arm (mirrors the v1.3.2 anti-flap doctrine of the built-ins)
        self._taught_cooldown_s: float = 4.0   # default profile cooldown
        self._taught_any_gap_s: float = 1.0    # min gap between ANY taught fires
        self._taught_hold_s: float = 0.35      # min continuous hold before fire
        self._taught_rearm_s: float = 0.6      # unmatched this long -> re-arm
        self._taught_hold_name: Optional[str] = None
        self._taught_hold_since: Optional[float] = None
        self._taught_lost_since: Optional[float] = None
        self._taught_fire_ts: Dict[str, float] = {}
        self._taught_any_last: float = 0.0
        self._taught_suppress_log: float = 0.0

        # telemetry rate limiters
        self._last_frame_emit: float = 0.0
        self._last_motion_emit: float = 0.0

        self._reporter = (
            SpatialReporter(base_url=radar_url,
                            resolution=f"{width}x{height}", fps=self.fps, log=log)
            if radar_enabled else None
        )

    # -- external control -------------------------------------------------- #
    def stop(self) -> None:
        self._stop_event.set()
        if self._reporter:
            self._reporter.stop()

    # -- public state (any thread) ----------------------------------------- #
    @property
    def user_attentive(self) -> Optional[bool]:
        """True/False when known; None until the face pipeline reports."""
        with self._lock:
            return self._attentive

    @property
    def people_count(self) -> int:
        with self._lock:
            return len(self._people)

    @property
    def mode(self) -> str:
        return self._mode

    def latest_jpeg_b64(self, max_width: int = 640, quality: int = 50) -> str:
        """Compressed base64 JPEG of the latest frame ("" when unavailable)."""
        with self._lock:
            frame = None if self._frame is None else self._frame.copy()
        if frame is None:
            return ""
        try:
            import cv2
            ok, buf = cv2.imencode(".jpg", _resize(frame, max_width),
                                   [cv2.IMWRITE_JPEG_QUALITY, int(quality)])
            return base64.b64encode(buf.tobytes()).decode("ascii") if ok else ""
        except Exception as exc:
            self._log(f"[perception] JPEG encode failed: {exc}")
            return ""

    # -- thread main -------------------------------------------------------- #
    def run(self) -> None:
        try:
            import cv2
        except ImportError:
            self._set_mode("off")
            self._log("[perception] opencv missing — webcam perception disabled")
            return
        # Guarded camera bring-up: a reset race (the dying instance briefly
        # holds the camera) or a transient DShow failure must degrade to
        # "off" — never kill the thread (Oct 3, 2026 crash: unguarded
        # cap.set at boot raced the old instance's camera release).
        # Retries: 3 attempts, 2 s apart, camera released between tries.
        self._cap = None
        for attempt in range(3):
            if attempt:
                time.sleep(2.0)
            if self._stop_event.is_set():
                return
            try:
                self._cap = cv2.VideoCapture(self.device_index, cv2.CAP_DSHOW)
                if not self._cap.isOpened():
                    self._cap.release()
                    self._cap = cv2.VideoCapture(self.device_index)  # generic backend retry
                if not self._cap.isOpened():
                    self._cap = None
                    continue
                try:
                    self._cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
                    self._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
                    self._cap.set(cv2.CAP_PROP_FPS, self.fps)
                except Exception as exc:
                    # Resolution/FPS props are nice-to-have — refuse → native mode.
                    self._log(f"[perception] camera property set refused "
                              f"({exc}) — running native mode")
                break
            except Exception as exc:
                if self._cap is not None:
                    try:
                        self._cap.release()
                    except Exception:
                        pass
                self._cap = None
                self._log(f"[perception] camera init error: {exc}")
        if self._cap is None:
            self._set_mode("off")
            self._log("[perception] camera unavailable/busy — webcam perception off")
            return

        self._init_stacks()
        if self._reporter:
            self._reporter.start()

        self._set_mode("tracking" if self._yolo is not None else "degraded")
        self._log("[perception] running — camera LED stays ON while tracking "
                  f"(face {'yes' if self._facemesh else 'no'}, "
                  f"hands {'yes' if self._hands else 'no'}, "
                  f"yolo {'yes' if self._yolo else 'no'})")

        frame_dt = 1.0 / self.fps
        while not self._stop_event.is_set():
            try:
                ok, frame = self._cap.read()
            except Exception as exc:
                self._log(f"[perception] camera read failed: {exc}")
                ok, frame = False, None
            if not ok or frame is None:
                self._set_mode("degraded")
                self._stop_event.wait(0.5)
                continue

            with self._lock:
                self._frame = frame
                self._frame_ts = time.monotonic()

            self._frame_count += 1
            try:
                self._process_frame(frame)
            except Exception as exc:
                self._log(f"[perception] pipeline error: {exc}")
            try:
                self._emit_telemetry(frame)
            except Exception as exc:
                self._log(f"[perception] telemetry error: {exc}")

            if self._mode == "degraded" and self._yolo is not None:
                self._set_mode("tracking")
            self._stop_event.wait(frame_dt)

        try:                            # release the device → LED off on shutdown
            if self._cap is not None:
                self._cap.release()
        except Exception:
            pass
        self._cap = None
        self._set_mode("off")
        self._log("[perception] stopped — camera released")

    def _init_stacks(self) -> None:
        """Lazy-load MediaPipe + YOLO; each failure degrades that stage only."""
        try:
            import mediapipe as mp
            self._facemesh = mp.solutions.face_mesh.FaceMesh(
                max_num_faces=1, refine_landmarks=False,
                min_detection_confidence=0.5, min_tracking_confidence=0.5)
            self._hands = mp.solutions.hands.Hands(
                max_num_hands=1, min_detection_confidence=0.6,
                min_tracking_confidence=0.5)
        except ImportError:
            self._facemesh = self._hands = None
            self._log("[perception] mediapipe missing — attention/gestures "
                      "disabled (tracking continues)")
        except Exception as exc:
            self._facemesh = self._hands = None
            self._log(f"[perception] mediapipe init failed ({exc}) — "
                      "attention/gestures disabled")
        try:
            from ultralytics import YOLO
            self._yolo = YOLO("yolov8n.pt")   # auto-downloads on first use
            self._log("[perception] YOLOv8n loaded (ByteTrack persistent IDs)")
        except ImportError:
            self._yolo = None
            self._log("[perception] ultralytics missing — person tracking/radar "
                      "disabled (frame sharing still works)")
        except Exception as exc:
            self._yolo = None
            self._log(f"[perception] YOLO init failed ({exc}) — tracking disabled")

    # -- frame pipeline ----------------------------------------------------- #
    def _process_frame(self, frame) -> None:
        """Cadenced perception passes (FaceMesh ~5 Hz, Hands ~7.5 Hz, YOLO 15 Hz)."""
        if self._frame_count % 3 == 0:
            self._update_attention(frame)
        # v1.6.0 — ONE Hands pass feeds every hand consumer (built-in
        # palm/fist dwell + taught-gesture lessons/recognition). With
        # LGI_GESTURES=0 and nothing taught (no active lesson, no profiles)
        # the pass is skipped entirely — zero landmark processing.
        if self._hands is not None and self._frame_count % 2 == 0 and (
                self.gestures_enabled or self._taught_pass_on()):
            self._update_hands(frame)
        if self._yolo is not None:
            self._update_tracking(frame)

    def _taught_pass_on(self) -> bool:
        """True when the teaching stack needs the Hands pass right now."""
        t = self._teacher
        if t is None:
            return False
        try:
            if t.active:                    # lesson capture always needs it
                return True
            return bool(self._taught_enabled) and bool(t.recognition_ready())
        except Exception:
            return False

    def _update_hands(self, frame) -> None:
        """ONE MediaPipe Hands process -> built-in dwell + taught stack."""
        if self._hands is None:
            return
        now = time.monotonic()
        try:
            import cv2
            res = self._hands.process(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
        except Exception as exc:
            self._log(f"[perception] gesture pipeline error: {exc}")
            return
        hand = (res.multi_hand_landmarks[0].landmark
                if res.multi_hand_landmarks else None)
        if self.gestures_enabled:
            self._builtin_dwell(hand, now)
        if self._teacher is not None:
            points = ([(lm.x, lm.y) for lm in hand]
                      if hand is not None else None)
            try:
                self._teacher.on_frame(points, now)   # lesson capture (no-op idle)
            except Exception as exc:
                self._log(f"[perception] lesson capture error: {exc}")
            if not self._teacher.active:
                # Recognition pauses while a lesson captures — one owner.
                try:
                    self._update_taught(hand, now)
                except Exception as exc:
                    self._log(f"[perception] taught recognition error: {exc}")

    def _builtin_dwell(self, hand, now: float) -> None:
        """v1.3.2 palm/fist dwell (semantics unchanged) fed by _update_hands:
        dwell resets on ANY frame without a confirmed large open/closed pose."""
        if hand is None:
            self._palm_since = self._fist_since = None
            return
        wrist, middle_mcp = hand[0], hand[9]
        hand_size = math.hypot(middle_mcp.x - wrist.x, middle_mcp.y - wrist.y)
        if hand_size < 0.10:   # too small / too far away — ignore
            self._palm_since = self._fist_since = None   # dwell never survives here
            return

        def reach(idx: int) -> float:
            return math.hypot(hand[idx].x - wrist.x, hand[idx].y - wrist.y)

        # thumb: tip (4) further from the pinky MCP (17) than its IP (3)
        thumb = (math.hypot(hand[4].x - hand[17].x, hand[4].y - hand[17].y)
                 > math.hypot(hand[3].x - hand[17].x, hand[3].y - hand[17].y) * 1.05)
        extended = sum(1 for tip, pip in ((8, 6), (12, 10), (16, 14), (20, 18))
                       if reach(tip) > reach(pip) * 1.15)
        total = extended + (1 if thumb else 0)

        if total >= 4:                                   # open palm
            self._fist_since = None
            if self._palm_since is None:
                self._palm_since = now
            elif now - self._palm_since >= self.palm_hold_s:
                self._fire_gesture("mic", "palm -> mic toggle",
                                   dwell_s=now - self._palm_since)
        elif total == 0:                                 # closed fist
            self._palm_since = None
            if self._fist_since is None:
                self._fist_since = now
            elif now - self._fist_since >= self.fist_hold_s:
                self._fire_gesture("media", "fist -> media play/pause",
                                   dwell_s=now - self._fist_since)
        else:                                            # ambiguous pose
            self._palm_since = self._fist_since = None

    def _update_attention(self, frame) -> None:
        """FaceMesh head-pose proxy -> user_attentive (False when no face)."""
        if self._facemesh is None:
            return
        try:
            import cv2
            res = self._facemesh.process(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
            if not res.multi_face_landmarks:
                with self._lock:
                    self._attentive = False
                return
            lm = res.multi_face_landmarks[0].landmark
            left, right, nose = lm[234], lm[454], lm[1]
            brow, chin = lm[10], lm[152]
            face_w = abs(right.x - left.x) or 1.0
            face_h = abs(chin.y - brow.y) or 1.0
            yaw = (nose.x - (left.x + right.x) / 2.0) / face_w * 90.0
            pitch = (nose.y - (brow.y + chin.y) / 2.0) / face_h * 90.0
            attentive = (abs(yaw) <= self.attention_yaw_deg
                         and abs(pitch) <= self.attention_pitch_deg
                         and face_h >= 0.12)   # close enough to be "at the desk"
            with self._lock:
                self._attentive = attentive
        except Exception as exc:
            self._log(f"[perception] face pipeline error: {exc}")

    # -- v1.6.0 taught gestures ------------------------------------------------ #
    def _update_taught(self, hand, now: float) -> None:
        """Match the live hand against taught profiles (LGI_TAUGHT_GESTURES)."""
        if not self._taught_enabled or build_signature is None or match_best is None:
            return
        sig = None
        if hand is not None:
            try:
                sig = build_signature([(lm.x, lm.y) for lm in hand])
            except Exception as exc:
                self._log(f"[perception] taught signature error: {exc}")
                return
        best = None
        if sig is not None:
            try:
                best = match_best(self._teacher.profiles(), sig)
            except Exception as exc:
                self._log(f"[perception] taught match error: {exc}")
                return
        name = None
        profile: Dict[str, Any] = {}
        score = 0.0
        if best:
            name, profile, score = best
        threshold = float(profile.get("threshold") or 0.88)
        if name is None or score < threshold:
            self._taught_note_unmatched(now)
            return
        if name != self._taught_hold_name:
            self._taught_hold_name = name
            self._taught_hold_since = now
            self._taught_lost_since = None
            return
        if (self._taught_hold_since is None
                or now - self._taught_hold_since < self._taught_hold_s):
            return
        if now - self._taught_any_last < self._taught_any_gap_s:
            return
        cooldown = float(profile.get("cooldown") or self._taught_cooldown_s)
        remain = cooldown - (now - self._taught_fire_ts.get(name, -1e12))
        if remain > 0:
            if now - self._taught_suppress_log >= 2.0:
                self._taught_suppress_log = now
                self._log(f"[perception] taught '{name}' held — cooldown "
                          f"({remain:.1f}s left)")
            return
        self._taught_fire_ts[name] = now
        self._taught_any_last = now
        self._taught_hold_since = None      # re-fire needs leave + fresh hold
        action = str(profile.get("action") or "chat")
        self._log(f"[perception] taught gesture: '{name}' -> {action} "
                  f"(score {score:.2f})")
        self._teacher.fire_dispatch(name, action)

    def _taught_note_unmatched(self, now: float) -> None:
        """A held pose that stops matching re-arms its fire after rearm_s."""
        if self._taught_hold_name is None:
            return
        if self._taught_lost_since is None:
            self._taught_lost_since = now
        elif now - self._taught_lost_since >= self._taught_rearm_s:
            self._taught_hold_name = None
            self._taught_hold_since = None
            self._taught_lost_since = None

    def _fire_gesture(self, command: str, label: str, dwell_s: float = 0.0) -> None:
        # v1.3.2: the dwell is cleared on EVERY fire attempt — fired or
        # cooldown-suppressed — so a palm held in place (waiting for visible
        # feedback) can never re-fire through the cooldown and flap the mic
        # mute state twice. Re-firing requires a fresh continuous hold.
        now = time.monotonic()
        self._palm_since = self._fist_since = None
        remain = self.cooldown_s - (now - self._gesture_last)
        if remain > 0:
            self._log(f"[perception] gesture {label} suppressed "
                      f"(cooldown, {remain:.1f}s left)")
            return
        self._gesture_last = now
        self._log(f"[perception] gesture: {label} (held {dwell_s:.1f}s)")
        if self._command:
            try:
                self._command(command)
            except Exception as exc:
                self._log(f"[perception] gesture dispatch failed: {exc}")

    def _update_tracking(self, frame) -> None:
        """YOLOv8n + ByteTrack -> persistent person dict (normalized coords)."""
        results = self._yolo.track(frame, persist=True, tracker="bytetrack.yaml",
                                   classes=[0], verbose=False, conf=0.35)
        now = time.monotonic()
        people: Dict[int, Dict[str, Any]] = {}
        try:
            boxes = results[0].boxes
            if boxes is not None and boxes.id is not None:
                h_img, w_img = frame.shape[:2]
                for box, tid, conf in zip(boxes.xyxy, boxes.id, boxes.conf):
                    x1, y1, x2, y2 = (float(v) for v in box)
                    tid = int(tid)
                    cx = (x1 + x2) / 2.0 / w_img
                    cy = (y1 + y2) / 2.0 / h_img
                    bh = max(0.0, (y2 - y1) / h_img)
                    prev = self._people.get(tid)
                    if prev and now > prev["last"]:
                        dt = max(1e-3, now - prev["last"])
                        vx = (cx - prev["x"]) / dt
                        vy = (cy - prev["y"]) / dt
                    else:
                        vx = vy = 0.0
                    people[tid] = {
                        "x": cx, "y": cy, "h": bh, "vx": vx, "vy": vy,
                        "conf": float(conf), "bbox": [x1, y1, x2, y2],
                        "last": now, "first": prev["first"] if prev else now,
                    }
        except Exception as exc:
            self._log(f"[perception] tracking error: {exc}")
            return
        for tid, prev in self._people.items():   # keep just-lost IDs ~1 s
            if tid not in people and now - prev["last"] < 1.0:
                people[tid] = prev
        with self._lock:
            self._people = people

    def _emit_telemetry(self, frame) -> None:
        """Rate-limited vision_frame (~2 Hz) + radar_motion (~1 Hz) emission."""
        if self._reporter is None or not self._reporter.connected:
            return
        now = time.monotonic()

        if now - self._last_frame_emit >= 1.0 / self.vframe_hz:
            self._last_frame_emit = now
            with self._lock:
                people = {tid: dict(p) for tid, p in self._people.items()}
            detections = []
            for tid, p in sorted(people.items(), key=lambda kv: -kv[1]["h"]):
                x1, y1, x2, y2 = p["bbox"]
                detections.append({
                    "class": "person", "class_id": 0,
                    "confidence": round(p["conf"], 3),
                    "bbox": [round(x1, 1), round(y1, 1),
                             round(x2, 1), round(y2, 1)],
                    "center": [round((x1 + x2) / 2.0, 1),
                               round((y1 + y2) / 2.0, 1)],
                    "track_id": tid,
                    "position_desc": _position_desc(p["x"], p["y"]),
                })
            count = len(detections)
            narration = (f"I see {count} {'person' if count == 1 else 'people'}"
                         + (f", {detections[0]['position_desc']}" if count else ""))
            self._reporter.emit("vision_frame", {
                "timestamp": time.time(),
                "frame_id": self._frame_count,
                "detections": detections,
                "narration": narration,
                "inference_time_ms": 0.0,   # local pipeline; not separately timed
                "camera_settings": {"resolution": self._reporter.resolution,
                                    "fps": self.fps},
                "device_id": DEVICE_ID,
            })

        if now - self._last_motion_emit >= 1.0 / self.motion_hz:
            self._last_motion_emit = now
            primary = None
            count_now = 0
            with self._lock:
                count_now = len(self._people)
                if self._people:
                    primary = max(self._people.values(), key=lambda p: p["h"])
            if primary is not None:
                speed = math.hypot(primary["vx"], primary["vy"])
                depth = min(4.0, 0.35 / max(0.08, primary["h"]))
                self._reporter.emit("radar_motion", {
                    "timestamp": time.time(),
                    "confidence": round(primary["conf"], 3),
                    "intensity": round(min(1.0, speed * 2.0), 3),
                    "duration": round(max(0.0, now - primary["first"]), 1),
                    "device_id": DEVICE_ID,
                    "x": round(primary["x"] * 2.0 - 1.0, 3),  # normalized [-1, 1]
                    "y": round(primary["y"] * 2.0 - 1.0, 3),
                    "z": round(depth, 2),                       # bbox-height proxy
                    "velocity": round(speed, 3),
                    "people_count": count_now,
                })

    def _set_mode(self, mode: str) -> None:
        if mode == self._mode:
            return
        self._mode = mode
        if self._state:
            try:
                self._state(mode)
            except Exception:
                pass


def _position_desc(nx: float, ny: float) -> str:
    """Human grid position ("center-left", "top-right", ...) for a detection."""
    col = "left" if nx < 0.37 else ("right" if nx > 0.63 else "center")
    row = "top" if ny < 0.37 else ("bottom" if ny > 0.63 else "center")
    if col == "center" and row == "center":
        return "center"
    if col == "center":
        return f"{row}-center"
    return f"{row}-{col}"


def _resize(img, max_width: int):
    h, w = img.shape[:2]
    if w <= max_width:
        return img
    import cv2
    scale = max_width / float(w)
    return cv2.resize(img, (max_width, int(h * scale)), interpolation=cv2.INTER_AREA)


def one_shot_jpeg_b64(max_width: int = 640, quality: int = 50,
                      device_index: int = 0) -> str:
    """Legacy fallback: open -> grab one frame -> release (LED blinks once)."""
    try:
        import cv2
        cap = cv2.VideoCapture(device_index, cv2.CAP_DSHOW)
        try:
            if not cap.isOpened():
                return ""
            ok, frame = cap.read()
            if not ok or frame is None:
                return ""
        finally:
            cap.release()
        ok, buf = cv2.imencode(".jpg", _resize(frame, max_width),
                               [cv2.IMWRITE_JPEG_QUALITY, int(quality)])
        return base64.b64encode(buf.tobytes()).decode("ascii") if ok else ""
    except Exception:
        return ""
