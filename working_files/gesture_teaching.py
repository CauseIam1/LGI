"""
LGI — Looking Glass Interface
gesture_teaching.py: v1.6.0 taught-gesture store + voice-lesson state machine.

The operator teaches LGI NEW hand poses by voice: "Polaris, teach a gesture"
→ she prompts "Show me the pose" → the operator holds it ~2–3 s while the
perception thread (webcam_perception.py's shared MediaPipe Hands pass) feeds
landmark frames here → "What should this mean?" → the next utterance is the
label → the profile is saved to taught_gestures.json next to lgi.py. At
runtime (LGI_TAUGHT_GESTURES, auto-inactive until at least one profile
exists) the perception pass matches live hand signatures against the saved
profiles and fires GestureTeacher.fire_dispatch → lgi.py's
"taught:<name>:<action>" command (media | mic | stop_voice | chat).

Design invariants:
  * Pure stdlib — no Qt / cv2 / mediapipe imports; landmark points arrive as
    plain [(x, y), ...] (image-normalized MediaPipe coords for 21 joints) or
    None when no hand is in view.
  * Additive only: taught profiles never shadow the built-in palm/fist
    mappings; reserved names are refused at save time.
  * One lesson at a time; while a lesson is live, recognition dispatch is
    suppressed (the capture pass owns the hand) and every utterance routes
    into the lesson (no wake word needed — the operator is mid-lesson).
  * Degrade, never crash: callbacks are optional and every storage error
    degrades to "no teaching" with a log line.
  * Thread model: handle_utterance() runs on the Qt drain thread, on_frame()
    on the perception thread — all mutable state is one RLock. Deadlines use
    self._clock (time.monotonic by default; tests may pin it).

Run `python gesture_teaching.py` for the offline self-test (synthetic palm /
fist landmark sets — no camera or mediapipe needed).
"""

from __future__ import annotations

import json
import math
import os
import re
import threading
import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

STORE_FILENAME = "taught_gestures.json"
STORE_VERSION = 1
MIN_HAND_SIZE = 0.10              # same far-hand gate as the built-in dwell
ACTIONS = ("media", "mic", "stop_voice", "chat")
DEFAULT_THRESHOLD = 0.88          # match floor (score = 0.45*ext + 0.55*cos)
DEFAULT_COOLDOWN_S = 4.0          # per-profile re-fire cooldown
RESERVED_NAMES = frozenset({"palm", "fist", "gesture", "gestures"})

PROMPT_TRIGGER_ACK = "Show me the pose — hold it steady for about three seconds."
PROMPT_MEANING = "Got it. What should this mean?"
PROMPT_CANCELLED = "Lesson cancelled — nothing saved."
PROMPT_TIMEOUT_POSE = "No pose seen — lesson cancelled."
PROMPT_TIMEOUT_LABEL = "Lesson timed out — nothing saved."
PROMPT_TOO_SHORT = "I didn't see the pose long enough — lesson cancelled."
PROMPT_DISABLED = "Taught gestures are disabled — set LGI_TAUGHT_GESTURES to 1 to use them."
PROMPT_RENAME = "Give me a short name for the pose."
PROMPT_RESERVED = "That name is reserved for the built-in poses — pick another."

ACTION_PHRASE = {
    "media": "Showing it will toggle media playback.",
    "mic": "Showing it will toggle the mic mute.",
    "stop_voice": "Showing it will stop my voice.",
    "chat": "Showing it will send that phrase to the gateway.",
}

# Spoken labels that map onto LGI's local command vocabulary; anything else
# becomes a "chat" profile (the pose replays the phrase to the gateway).
_MEDIA_LABELS = frozenset({
    "media", "play", "pause", "play media", "pause media", "play pause",
    "play/pause", "media pause", "pause video", "pause youtube",
    "pause music", "toggle media", "toggle playback", "pause playback",
    "play video", "pause netflix", "pause spotify",
})
_MIC_LABELS = frozenset({
    "mic", "mute", "unmute", "mute mic", "mic mute", "mute microphone",
    "toggle mic", "toggle microphone", "mic mute toggle", "mute my mic",
    "mute the mic", "mute the microphone",
})
_STOP_LABELS = frozenset({
    "stop talking", "be quiet", "stop voice", "quiet please", "shut up",
    "silence",
})

# Voice triggers (matched on the lowercased utterance; the wake word has
# already been stripped by lgi.py when present — both forms are tolerated).
TEACH_TRIGGER_RE = re.compile(
    r"\bteach\b[^.?!]*\bgestures?\b|\bgesture (?:lesson|teaching)\b"
    r"|\bnew gesture\b|\blearn a (?:new )?gesture\b")
FORGET_RE = re.compile(
    r"\bforget (?:the |my |that |your )?gesture "
    r"(?:called |named )?(?P<name>[a-z0-9'’\- ]{1,40})")
LIST_RE = re.compile(
    r"\b(?:what|which) (?:gestures?|hand poses?)\b"
    r"|\blist (?:your |the |taught |all )?gestures?\b"
    r"|\bname (?:your |the )?gestures?\b|\btaught gestures\b")

# While a lesson is live these phrases cancel it (never saved as labels —
# mirrors the built-in talk-window close phrases so "sleep" stays control).
CANCEL_PHRASES = frozenset({
    "cancel", "cancel lesson", "cancel the lesson", "cancel teaching",
    "forget it", "nevermind", "never mind", "stop the lesson", "stop teaching",
    "sleep", "go to sleep", "end the conversation", "stop the conversation",
    "close the talk window",
})

# filler prefix stripped from a label answer ("this means pause the music")
_LABEL_PREFIX_RE = re.compile(
    r"^(?:it |this |that |she |her )+"
    r"(?:means? |should mean |will mean |is |does |calls? for |makes? )?")

_PAIRS: Tuple[Tuple[int, int], ...] = tuple(
    (i, j) for i in range(21) for j in range(i + 1, 21))   # C(21,2) = 210
_EXT_PAIRS: Tuple[Tuple[int, int], ...] = ((8, 6), (12, 10), (16, 14), (20, 18))


def _dist(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def build_signature(points: Optional[List[Tuple[float, float]]]
                    ) -> Optional[Tuple[List[float], List[float]]]:
    """21 hand landmarks -> (extension vector, normalized distance vector).

    Position/scale invariant: the 210 pairwise landmark distances are
    normalized by palm size (wrist -> middle MCP) and the finger-extension
    vector uses the same ratio rules as the built-in palm/fist dwell logic.
    Returns None for no/malformed input or a too-small (far away) hand.
    """
    if not points or len(points) < 21:
        return None
    pts = [(float(p[0]), float(p[1])) for p in points[:21]]
    if not all(math.isfinite(v) for p in pts for v in p):
        return None
    wrist, middle_mcp = pts[0], pts[9]
    size = _dist(wrist, middle_mcp)
    if not math.isfinite(size) or size < MIN_HAND_SIZE:
        return None

    def reach(idx: int) -> float:
        return _dist(pts[idx], wrist)

    thumb = _dist(pts[4], pts[17]) > _dist(pts[3], pts[17]) * 1.05
    ext = [1.0 if thumb else 0.0]
    ext += [1.0 if reach(tip) > reach(pip) * 1.15 else 0.0
            for tip, pip in _EXT_PAIRS]
    scale = 1.0 / size
    dists = [_dist(pts[i], pts[j]) * scale for i, j in _PAIRS]
    return ext, dists


def _cosine(a: List[float], b: List[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na <= 1e-9 or nb <= 1e-9:
        return 0.0
    return dot / (na * nb)


def match_score(profile: Dict[str, Any],
                sig: Tuple[List[float], List[float]]) -> float:
    """Score one profile against a live signature: [0..1], higher = closer."""
    pext = profile.get("ext") or []
    pdist = profile.get("dist") or []
    ext, dist = sig
    if len(pext) != 5 or len(pdist) != len(dist):
        return 0.0
    l1 = sum(abs(a - b) for a, b in zip(pext, ext))       # ≤1 bend tolerated
    ext_score = 1.0 - min(1.0, l1 / 5.0)
    return 0.45 * ext_score + 0.55 * _cosine(pdist, dist)


def match_best(profiles: List[Dict[str, Any]],
               sig: Tuple[List[float], List[float]]
               ) -> Optional[Tuple[str, Dict[str, Any], float]]:
    """Best-scoring profile above ITS OWN threshold, else None."""
    best: Optional[Tuple[str, Dict[str, Any], float]] = None
    for profile in profiles or []:
        score = match_score(profile, sig)
        threshold = float(profile.get("threshold") or DEFAULT_THRESHOLD)
        if score >= threshold:
            name = str(profile.get("name") or "")
            if name and (best is None or score > best[2]):
                best = (name, profile, score)
    return best


def _classify_action(label: str) -> str:
    label = label.strip().lower()
    if label in _MEDIA_LABELS:
        return "media"
    if label in _MIC_LABELS:
        return "mic"
    if label in _STOP_LABELS:
        return "stop_voice"
    if "mute" in label or "unmute" in label:
        return "mic"
    if any(w in label for w in ("pause", "play", "media", "playback")):
        return "media"
    if "stop" in label and any(w in label for w in ("talk", "voice", "speech")):
        return "stop_voice"
    return "chat"


def _parse_label(norm: str) -> str:
    """Answer text -> gesture name (filler stripped, short + sane)."""
    text = norm.strip().strip("\"'“”") or ""
    for _ in range(2):
        text = _LABEL_PREFIX_RE.sub("", text).strip()
    text = re.sub(r"^(?:the |a |an )+", "", text).strip(" .!?,")
    words = text.split()
    while words and (len(words) > 6 or len(" ".join(words)) > 40):
        words.pop()
    label = " ".join(words).strip()
    return label if 2 <= len(label) <= 40 else ""


def _valid_profile(profile: Dict[str, Any]) -> bool:
    """Shape/ sanity check for a stored profile (refuse garbage at the door)."""
    name = str(profile.get("name") or "").strip()
    if not (0 < len(name) <= 40) or name in RESERVED_NAMES:
        return False
    if str(profile.get("action") or "chat") not in ACTIONS:
        return False
    ext = list(profile.get("ext") or [])
    dist = list(profile.get("dist") or [])
    if len(ext) != 5 or len(dist) != len(_PAIRS):
        return False
    return all(isinstance(v, (int, float)) and math.isfinite(v)
               for v in ext + dist)


class GestureStore:
    """taught_gestures.json — tiny, validated, atomic-replace profile store.

    Corrupt/missing files degrade to "empty store" (logged, never raised);
    saves write a .tmp sibling then os.replace (atomic on Windows + POSIX).
    """

    def __init__(self, base_dir: str, log: Callable[..., None] = print) -> None:
        self.path = os.path.join(base_dir, STORE_FILENAME)
        self._log = log
        self._lock = threading.Lock()
        self._profiles: Dict[str, Dict[str, Any]] = {}
        self.load()

    # -- queries ----------------------------------------------------------- #
    def list(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(p) for p in self._profiles.values()]

    def names(self) -> List[str]:
        with self._lock:
            return list(self._profiles.keys())

    def count(self) -> int:
        with self._lock:
            return len(self._profiles)

    # -- mutations ----------------------------------------------------------- #
    def add(self, profile: Dict[str, Any]) -> bool:
        """Insert/overwrite one validated profile; True when it replaced."""
        if not _valid_profile(profile):
            self._log(f"[teach] refused invalid profile "
                      f"{profile.get('name')!r}")
            return False
        with self._lock:
            name = str(profile["name"])
            existed = name in self._profiles
            self._profiles[name] = dict(profile)
            self._save_locked()
            return existed

    def delete(self, name: str) -> bool:
        with self._lock:
            if name in self._profiles:
                del self._profiles[name]
                self._save_locked()
                return True
            return False

    # -- persistence ----------------------------------------------------------- #
    def load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                payload = json.load(fh)
        except FileNotFoundError:
            return                        # first run — nothing taught yet
        except Exception as exc:
            self._log(f"[teach] store read failed ({exc}) — starting empty")
            return
        profiles = (payload.get("profiles")
                    if isinstance(payload, dict) else None)
        kept = 0
        for entry in (profiles or []):
            if isinstance(entry, dict) and str(entry.get("name") or "").strip():
                if str(entry.get("action") or "chat") not in ACTIONS:
                    entry["action"] = "chat"
                if _valid_profile(entry):
                    self._profiles[str(entry["name"])] = entry
                    kept += 1
        if kept:
            self._log(f"[teach] store loaded — {kept} profile(s) "
                      f"from {self.path}")

    def _save_locked(self) -> None:
        try:
            payload = {"version": STORE_VERSION,
                       "profiles": list(self._profiles.values())}
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
        except Exception as exc:
            self._log(f"[teach] store save failed ({exc}) — memory kept")


class GestureTeacher:
    """Voice-lesson state machine + taught-gesture dispatch funnel.

    States: IDLE → AWAIT_POSE (show the pose) → CAPTURING (~2–3 s of landmark
    frames) → AWAIT_LABEL (next utterance names it) → IDLE (saved).
    handle_utterance() runs on the Qt drain thread; on_frame() is fed from
    the perception thread. All prompts flow through the say/note callbacks —
    lgi.py queues them to its Qt drain, so this module never touches Qt/TTS.
    """

    IDLE = "idle"
    AWAIT_POSE = "await_pose"
    CAPTURING = "capturing"
    AWAIT_LABEL = "await_label"

    def __init__(self, base_dir: str, enabled: bool = True,
                 say: Optional[Callable[[str], None]] = None,
                 note: Optional[Callable[[str], None]] = None,
                 dispatch: Optional[Callable[[str, str], None]] = None,
                 log: Callable[..., None] = print,
                 capture_samples: int = 12,
                 await_pose_s: float = 15.0,
                 capture_s: float = 10.0,
                 label_s: float = 25.0,
                 match_threshold: float = DEFAULT_THRESHOLD,
                 cooldown_s: float = DEFAULT_COOLDOWN_S) -> None:
        self.enabled = bool(enabled)          # LGI_TAUGHT_GESTURES master
        self.capture_samples = max(3, int(capture_samples))
        self.await_pose_s = float(await_pose_s)
        self.capture_s = float(capture_s)
        self.label_s = float(label_s)
        self.match_threshold = float(match_threshold)
        self.cooldown_s = float(cooldown_s)
        self._clock = time.monotonic          # pinnable for tests
        self._say = say
        self._note = note
        self._dispatch = dispatch
        self._log = log
        self._lock = threading.RLock()
        self._state = self.IDLE
        self._deadline = 0.0
        self._samples: List[Tuple[List[float], List[float]]] = []
        self._pending: Optional[Tuple[List[float], List[float], int]] = None
        self._hinted = False
        self._store = GestureStore(base_dir, log=log)
        self._log(f"[teach] init — store {self._store.path} "
                  f"({self._store.count()} profile(s)), recognition "
                  f"{'ON' if enabled else 'OFF (LGI_TAUGHT_GESTURES=0)'}")

    # -- queries ------------------------------------------------------------- #
    @property
    def path(self) -> str:
        return self._store.path

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    @property
    def active(self) -> bool:
        with self._lock:
            return self._state != self.IDLE

    def has_profiles(self) -> bool:
        return self._store.count() > 0

    def profiles(self) -> List[Dict[str, Any]]:
        return self._store.list()

    def recognition_ready(self) -> bool:
        """Recognition runs only when enabled, taught, and not mid-lesson."""
        return self.enabled and not self.active and self.has_profiles()

    # -- dispatch funnel -------------------------------------------------------- #
    def fire_dispatch(self, name: str, action: str) -> None:
        cb = self._dispatch
        if cb is None:
            return
        try:
            cb(name, action)
        except Exception as exc:
            self._log(f"[teach] dispatch failed for '{name}': {exc}")

    # -- perception-thread API ------------------------------------------------- #
    def on_frame(self, points: Optional[List[Tuple[float, float]]],
                 now: Optional[float] = None) -> None:
        """One hand (21 points, or None) per Hands slot; lesson capture only."""
        if now is None:
            now = self._clock()
        with self._lock:
            if self._state == self.IDLE:
                return
            if not self._check_deadline_locked(now):
                return
            if self._state == self.AWAIT_LABEL:
                return                    # capture done — labels are voice-only
            sig = build_signature(points) if points else None
            if self._state == self.AWAIT_POSE:
                if sig is None:
                    return
                self._state = self.CAPTURING
                self._samples = [sig]
                self._deadline = now + self.capture_s
                self._note_msg("capturing pose…")
                return
            # CAPTURING — collect, finalize on sample count or deadline
            if sig is not None:
                self._samples.append(sig)
            if (len(self._samples) >= self.capture_samples
                    or now >= self._deadline):
                self._finalize_locked(now)

    # -- utterance API (Qt drain thread) ---------------------------------------- #
    def handle_utterance(self, text: str) -> bool:
        """True when the utterance was consumed (lesson / trigger / forget /
        list). When False the caller routes onward (chat/ambient)."""
        norm = " ".join(str(text or "").split()).strip().lower().rstrip(" .!?,")
        if not norm:
            return False
        with self._lock:
            if not self._check_deadline_locked(self._clock()):
                return True               # a timeout just ended the lesson
            if self._state != self.IDLE:
                return self._lesson_utterance_locked(norm)
            return self._idle_utterance_locked(norm)

    def _lesson_utterance_locked(self, norm: str) -> bool:
        if norm in CANCEL_PHRASES:
            self._reset_locked()
            self._say_msg(PROMPT_CANCELLED)
            return True
        if self._state == self.AWAIT_POSE:
            if not self._hinted:
                self._hinted = True
                self._say_msg("Whenever you're ready — hold the pose in view.")
            return True
        if self._state == self.CAPTURING:
            return True                   # pose being held — speech ignored
        # AWAIT_LABEL → this utterance is the label
        label = _parse_label(norm)
        if not label:
            self._say_msg(PROMPT_RENAME)
            return True
        if label in RESERVED_NAMES:
            self._say_msg(PROMPT_RESERVED)
            return True
        action = _classify_action(label)
        existed = self._save_profile_locked(label, action)
        phrase = ACTION_PHRASE.get(action, "")
        self._reset_locked()
        if existed:
            self._say_msg(f"Updated the '{label}' gesture. {phrase}".strip())
        else:
            self._say_msg(f"Saved. {label}. {phrase}".strip())
        self._log(f"[teach] profile {'updated' if existed else 'saved'}: "
                  f"{label!r} -> {action}")
        return True

    def _idle_utterance_locked(self, norm: str) -> bool:
        trigger = (TEACH_TRIGGER_RE.search(norm) or FORGET_RE.search(norm)
                   or LIST_RE.search(norm))
        if trigger and not self.enabled:
            self._say_msg(PROMPT_DISABLED)
            return True
        if not trigger:
            return False
        if TEACH_TRIGGER_RE.search(norm):
            self._state = self.AWAIT_POSE
            self._deadline = self._clock() + self.await_pose_s
            self._samples = []
            self._pending = None
            self._hinted = False
            self._say_msg(PROMPT_TRIGGER_ACK)
            self._log("[teach] lesson started — awaiting pose "
                      f"({int(self.await_pose_s)}s window)")
            return True
        m = FORGET_RE.search(norm)
        if m:
            name = " ".join((m.group("name") or "").split())
            name = name.strip(" .!?,;:'\"’-").lower()
            if name and self._store.delete(name):
                self._say_msg(f"Forgot the {name} gesture.")
            else:
                self._say_msg(f"I don't have a '{name or 'that'}' gesture.")
            return True
        names = self._store.names()
        if names:
            self._say_msg("Taught gestures: " + ", ".join(names) + ".")
        else:
            self._say_msg("No taught gestures yet — say 'teach a gesture' "
                          "to start a lesson.")
        return True

    # -- internals (lock held) ---------------------------------------------- #
    def _finalize_locked(self, now: float) -> None:
        samples = list(self._samples)
        self._samples = []
        if len(samples) < 3:
            self._reset_locked()
            self._say_msg(PROMPT_TOO_SHORT)
            return
        self._begin_label_locked(samples, now)

    def _begin_label_locked(self,
                            samples: List[Tuple[List[float], List[float]]],
                            now: float) -> None:
        n = len(samples)
        ext = [sum(s[0][i] for s in samples) / n for i in range(5)]
        dist = [sum(s[1][i] for s in samples) / n for i in range(len(_PAIRS))]
        self._pending = (ext, dist, n)
        self._state = self.AWAIT_LABEL
        self._deadline = now + self.label_s
        self._say_msg(PROMPT_MEANING)
        self._log(f"[teach] pose captured ({n} frames) — awaiting label "
                  f"({int(self.label_s)}s)")

    def _save_profile_locked(self, label: str, action: str) -> bool:
        ext, dist, n = self._pending  # type: ignore[misc]
        return self._store.add({
            "name": label, "action": action,
            "ext": [round(v, 4) for v in ext],
            "dist": [round(v, 5) for v in dist],
            "samples": int(n),
            "threshold": round(self.match_threshold, 3),
            "cooldown": round(self.cooldown_s, 2),
            "created": datetime.now().isoformat(timespec="seconds"),
        })

    def _check_deadline_locked(self, now: float) -> bool:
        """False when a timeout just ended the lesson (True = may continue)."""
        if self._state == self.IDLE or now < self._deadline:
            return True
        state, samples = self._state, list(self._samples)
        self._reset_locked()
        if state == self.AWAIT_POSE:
            self._say_msg(PROMPT_TIMEOUT_POSE)
        elif state == self.CAPTURING:
            if len(samples) >= 3:
                self._begin_label_locked(samples, now)  # reuse what we got
                return True
            self._say_msg(PROMPT_TOO_SHORT)
        else:
            self._say_msg(PROMPT_TIMEOUT_LABEL)
        return False

    def _reset_locked(self) -> None:
        self._state = self.IDLE
        self._deadline = 0.0
        self._samples = []
        self._pending = None
        self._hinted = False

    def _say_msg(self, text: str) -> None:
        self._emit(self._say, text)

    def _note_msg(self, text: str) -> None:
        self._emit(self._note, text)

    def _emit(self, cb: Optional[Callable[[str], None]], text: str) -> None:
        if cb is None:
            return
        try:
            cb(text)
        except Exception as exc:
            self._log(f"[teach] callback failed ({exc}) — prompt dropped")


# --- offline self-test (no camera / no mediapipe) --------------------------- #
def _synth_open(jitter: float = 0.0) -> List[Tuple[float, float]]:
    """Synthetic open palm — 21 landmarks, image-normalized coords."""
    pts = [
        (0.50, 0.70),                                                # wrist
        (0.42, 0.66), (0.37, 0.62), (0.34, 0.57), (0.32, 0.52),      # thumb
        (0.42, 0.56), (0.41, 0.49), (0.405, 0.44), (0.40, 0.39),     # index
        (0.50, 0.55), (0.50, 0.47), (0.50, 0.41), (0.50, 0.35),      # middle
        (0.57, 0.56), (0.575, 0.49), (0.578, 0.43), (0.58, 0.38),    # ring
        (0.63, 0.58), (0.635, 0.52), (0.64, 0.47), (0.645, 0.43),    # pinky
    ]
    return [(x + jitter, y - jitter * 0.5) for x, y in pts]


def _synth_fist(jitter: float = 0.0) -> List[Tuple[float, float]]:
    """Synthetic closed fist — thumb tucked, fingertips pulled to the palm."""
    pts = [
        (0.50, 0.70),                                                # wrist
        (0.42, 0.66), (0.38, 0.63), (0.44, 0.64), (0.44, 0.63),      # thumb
        (0.42, 0.56), (0.41, 0.49), (0.42, 0.50), (0.425, 0.51),     # index
        (0.50, 0.55), (0.50, 0.47), (0.50, 0.48), (0.505, 0.51),     # middle
        (0.57, 0.56), (0.575, 0.49), (0.57, 0.50), (0.575, 0.515),   # ring
        (0.63, 0.58), (0.635, 0.52), (0.63, 0.53), (0.63, 0.53),     # pinky
    ]
    return [(x + jitter, y - jitter * 0.5) for x, y in pts]


def _self_test() -> int:
    """End-to-end lesson/replay/forget round trip on synthetic landmarks."""
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        events: List[tuple] = []
        t = GestureTeacher(
            base_dir=td, enabled=True,
            say=lambda s: events.append(("say", s)),
            note=lambda s: events.append(("note", s)),
            dispatch=lambda n, a: events.append(("dispatch", n, a)),
            log=lambda *a, **k: None,
        )
        t._clock = lambda: 1000.0            # pin the deadline domain
        assert t.handle_utterance("what gestures do you know") is True
        assert any(e[0] == "say" for e in events)
        assert t.handle_utterance("polaris, teach a gesture") is True
        assert t.active and t.state == t.AWAIT_POSE
        for i in range(14):                  # hold pose ~1.8 s of frames
            t.on_frame(_synth_open(jitter=i * 1e-4), 1000.0 + i * 0.13)
        assert t.state == t.AWAIT_LABEL, t.state
        assert t.handle_utterance("this means pause the music") is True
        assert not t.active
        profiles = t.profiles()
        assert len(profiles) == 1, profiles
        assert profiles[0]["name"] == "pause the music"
        assert profiles[0]["action"] == "media"
        hit = match_best(profiles, build_signature(_synth_open(0.004)))
        assert hit and hit[0] == "pause the music" and hit[2] >= 0.88, hit
        miss = match_best(profiles, build_signature(_synth_fist()))
        assert miss is None, miss
        assert t.handle_utterance("teach a new gesture") is True
        for i in range(14):
            t.on_frame(_synth_fist(jitter=i * 1e-4), 1000.0 + i * 0.13)
        assert t.state == t.AWAIT_LABEL, t.state
        assert t.handle_utterance("fist") is True            # reserved → re-ask
        assert t.state == t.AWAIT_LABEL
        assert t.handle_utterance("mute the mic") is True
        assert not t.active
        profiles = t.profiles()
        assert len(profiles) == 2, profiles
        mic_prof = [p for p in profiles if p["name"] == "mute the mic"][0]
        assert mic_prof["action"] == "mic"
        assert t.handle_utterance("forget the gesture pause the music") is True
        assert len(t.profiles()) == 1
        t2 = GestureTeacher(base_dir=td, enabled=True, log=lambda *a, **k: None)
        t2._clock = lambda: 1000.0
        assert t2.has_profiles() and len(t2.profiles()) == 1
        assert t2.handle_utterance("forget the gesture mute the mic") is True
        assert not t2.has_profiles()
        t3 = GestureTeacher(base_dir=td, enabled=False, log=lambda *a, **k: None)
        assert t3.handle_utterance("polaris teach a gesture") is True
        assert not t3.active
        t4 = GestureTeacher(base_dir=td, enabled=True, log=lambda *a, **k: None)
        t4._clock = lambda: 1000.0
        assert t4.handle_utterance("teach a gesture") is True
        assert t4.handle_utterance("nevermind") is True
        assert not t4.active
        t5 = GestureTeacher(base_dir=td, enabled=True, log=lambda *a, **k: None)
        t5._clock = lambda: 1000.0
        assert t5.handle_utterance("teach a gesture") is True
        t5._clock = lambda: 1100.0           # past the 15 s pose deadline
        assert t5.handle_utterance("hello") is True
        assert not t5.active
        assert not any(e[0] == "dispatch" for e in events)  # lessons never fire
    print("gesture_teaching self-test OK (capture, label, match, forget, "
          "persist, cancel, timeouts)")
    return 0


if __name__ == "__main__":
    raise SystemExit(_self_test())