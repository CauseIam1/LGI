"""
LGI — Looking Glass Interface
gateway_feed.py: Always-listening Socket.IO feed client + conversation store.

Phase 3 "Always Listening": the LGI HUD becomes a full Looking-Glass peer.
The gateway (Oct 1, 2026) broadcasts every outgoing Polaris reply and stream
delta to the sender's persistent per-identity room (`room=user_identity`)
and replays the last 40 messages on `request_history` — LGI joins that room
plus the 'status' room (task feed) over ONE persistent Socket.IO connection:

  * GatewayFeedClient — daemon-thread Socket.IO client modeled on the radar
    SpatialReporter (webcam_perception.py). NEVER touches Qt: every event is
    pushed into a queue drained by the 100 ms QTimer in lgi.py (single
    UI-writer contract). Reconnection is UNLIMITED (the dashboard v1.0.4
    chat-repair lesson: a capped reconnectionAttempts counter turned every
    gunicorn worker recycle into permanent silence) and every (re)connect
    re-announces identity + re-seeds history + rejoins the task room, so
    messages missed during an outage render on return.

  * ConversationStore — the HUD's rolling "latest 5 per side" window
    (Looking-Glass seed v2 semantics) carrying the dashboard v1.0.4 dedupe
    policy:
      - user-side echoes collapse by (sender, content) within a 150 s window
        (fresh broadcast timestamps vs seeded ones never stack);
      - Polaris keeps the strict (sender, content, timestamp) triple so
        genuinely repeated short replies still render;
      - live stream partials render while typing and promote EXACTLY ONCE —
        the trailing final `chat_message` of the same stream is suppressed
        via a promoted-content digest, so typed-out text never double-fires.
    Pure stdlib — importable and unit-testable without PyQt6.

Queue contracts (consumed by LGIApp._drain() in lgi.py):
  feed_q: ("history",      [row dicts])                      — seed / fallback replay
          ("chat_message", {sender, content, timestamp, ...})— final messages
          ("stream",       stream_id, chunk, is_complete)   — live deltas
          ("tool",         {tool_calls, results})           — console-log only
          ("tasks",        [active task dicts])             — status-room join snapshot
          ("task",         kind, task dict)                 — task_* lifecycle events
  status_q: ("feed", state) with state in
          online / connecting / offline                       — HUD ⌁ LED
"""

from __future__ import annotations

import hashlib
import threading
import time
from datetime import datetime
from queue import Queue
from typing import Any, Callable, Dict, List, Optional, Tuple

POLARIS_SENDERS = {"polaris", "assistant"}
ROW_KEEP_LIMIT = 40           # rolling store keeps the seed-size tail
STREAM_DIGEST_TTL_S = 600.0   # promoted stream content suppresses finals once
ECHO_WINDOW_S = 150.0         # dashboard v1.0.4 user-echo collapse window

# Task-feed row styles (kind -> (icon, color)) — mirrors the dashboard colors:
# cyan task started, green completed, red failed, gray registered/progress.
TASK_ROW_STYLES = {
    "task_registered": ("◇", "#93c5fd"),
    "task_started": ("▶", "#22d3ee"),
    "task_progress": ("⟳", "#f59e0b"),
    "task_completed": ("✓", "#22c55e"),
    "task_failed": ("✗", "#ef4444"),
    "chat_tool": ("⚙", "#c084fc"),   # v1.4.2: LGI's ⚙ CHAT_TOOL row (gateway tool runs)
}


def _norm_sender(sender: Any) -> str:
    return str(sender or "").strip().lower()


def is_polaris_row(sender: Any, role: Any = "") -> bool:
    """Side detection: everything not Polaris counts as operator/user side."""
    return _norm_sender(sender) in POLARIS_SENDERS or _norm_sender(role) in POLARIS_SENDERS


def _ts_epoch(ts: Any) -> Optional[float]:
    """Parse a gateway timestamp (Z-suffixed ISO / naive ISO / epoch float)
    into epoch seconds. Returns None for unparseable values — callers treat
    None as 'assume duplicate' (echo suppression is fail-closed)."""
    raw = str(ts or "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        pass
    text = raw.replace("Z", "+00:00")
    for candidate in (text, text.replace(" ", "T")):
        try:
            return datetime.fromisoformat(candidate).timestamp()
        except ValueError:
            continue
    return None


def parse_history_pairs(payload: Any) -> List[Dict[str, str]]:
    """Normalize the REST seed fallback (GET /api/history/<user> — the paired
    unification envelope) into flat feed rows. Defensive: accepts several
    envelope/field spellings, tolerates junk entries, never raises."""
    if not isinstance(payload, dict):
        return []
    items = payload.get("history")
    if not isinstance(items, list):
        items = payload.get("messages")
    if not isinstance(items, list):
        items = payload.get("pairs")
    if not isinstance(items, list):
        return []
    rows: List[Dict[str, str]] = []
    for item in items:
        if isinstance(item, str):
            item = {"user_message": item}
        if not isinstance(item, dict):
            continue
        user_text = str(item.get("user_message") or item.get("user") or item.get("prompt") or "").strip()
        ai_text = str(item.get("ai_response") or item.get("polaris") or item.get("response") or "").strip()
        if not user_text and not ai_text:
            continue
        user_ts = str(item.get("timestamp") or item.get("user_timestamp") or "").strip()
        ai_ts = str(item.get("ai_timestamp") or user_ts).strip()
        if user_text:
            rows.append({"sender": "user", "content": user_text, "timestamp": user_ts})
        if ai_text:
            rows.append({"sender": "polaris", "content": ai_text, "timestamp": ai_ts})
    return rows


def _digest_key(content: str) -> str:
    return hashlib.sha1(content.encode("utf-8", "ignore")).hexdigest()


class ConversationStore:
    """Rolling latest-N-per-side conversation window (pure logic, Qt-free).

    Rows are ``{"sender": str, "content": str, "timestamp": str}`` (+ a
    transient ``"partial"`` flag on in-flight stream renders). Side split:
    sender/role in {"polaris", "assistant"} = Polaris; anything else (rich,
    matt, dashboard, operator, user, ...) = operator side.
    """

    def __init__(self, per_side: int = 5, echo_window_s: float = ECHO_WINDOW_S,
                 log: Optional[Callable[[str], None]] = None) -> None:
        self.per_side = max(1, int(per_side))
        self.echo_window_s = float(echo_window_s)
        self._log = log
        self._rows: List[Dict[str, Any]] = []
        self._streams: Dict[str, Dict[str, Any]] = {}
        self._digests: Dict[str, float] = {}

    # -- seed -------------------------------------------------------------- #
    def seed(self, rows: List[Dict[str, Any]]) -> None:
        """Replace window content with a history replay (WS 'chat_history'
        seed or the REST paired fallback). In-flight partials are cleared:
        a reconnect means the previous worker died mid-stream and its
        deltas are gone forever."""
        self._streams.clear()
        fresh: List[Dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            content = str(row.get("content") or row.get("message") or "").strip()
            if not content:
                continue
            sender = str(row.get("sender") or row.get("role") or "user").strip()
            if is_polaris_row(sender, row.get("role")):
                sender = "polaris"
            fresh.append({"sender": sender, "content": content,
                          "timestamp": str(row.get("timestamp") or "")})
        self._rows = fresh[-ROW_KEEP_LIMIT:]

    # -- live events -------------------------------------------------------- #
    def apply_chat_message(self, msg: Dict[str, Any]) -> bool:
        """Absorb one 'chat_message' broadcast. Returns True only when the
        visible window actually changes — silent duplicates never re-render."""
        content = str(msg.get("content") or msg.get("message") or "").strip()
        if not content:
            return False
        sender_raw = str(msg.get("sender") or msg.get("role") or "user").strip()
        polaris = is_polaris_row(sender_raw, msg.get("role"))
        sender = "polaris" if polaris else sender_raw
        timestamp = str(msg.get("timestamp") or "")

        if polaris and self._digest_hit(content):
            return False  # trailing final of an already-promoted stream

        incoming = _ts_epoch(timestamp)
        if polaris:
            # v1.0.4: Polaris keeps the strict (sender, content, timestamp)
            # triple — genuinely repeated short replies still render.
            for row in reversed(self._rows):
                if (row["sender"] == "polaris" and row["content"] == content
                        and row["timestamp"] == timestamp):
                    return False
        else:
            # v1.0.4: user echoes collapse by (sender, content) inside the
            # 150 s window; an exact older pair is a genuine resend.
            for row in reversed(self._rows):
                if row["sender"] != sender or row["content"] != content:
                    continue
                row_epoch = _ts_epoch(row["timestamp"])
                if incoming is None or row_epoch is None:
                    return False  # unparseable pair — fail closed
                if abs(incoming - row_epoch) < self.echo_window_s:
                    return False
                break

        self._rows.append({"sender": sender, "content": content, "timestamp": timestamp})
        self._rows = self._rows[-ROW_KEEP_LIMIT:]
        return True

    def apply_stream(self, stream_id: Any, chunk: str, is_complete: bool) -> bool:
        """Absorb one 'chat_stream' delta. True → re-render the window; the
        partial row types out live and promotes exactly once at completion."""
        sid = str(stream_id or "")
        if not sid:
            return False
        buf = self._streams.get(sid)
        if buf is None:
            buf = {"text": "", "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
            self._streams[sid] = buf
        if chunk:
            buf["text"] = (buf["text"] + str(chunk)).strip()
        if is_complete:
            text = buf["text"]
            self._streams.pop(sid, None)
            if text:
                self._rows.append({"sender": "polaris", "content": text,
                                   "timestamp": buf["timestamp"]})
                self._rows = self._rows[-ROW_KEEP_LIMIT:]
                self._digests[_digest_key(text)] = time.time()
            return bool(text)
        return True

    # -- visibility ---------------------------------------------------------- #
    def visible(self) -> List[Dict[str, Any]]:
        """Latest N per side, interleaved chronologically (Looking-Glass seed
        v2), with any in-flight partial pinned as the trailing Polaris row."""
        user_tail = [r for r in self._rows if r["sender"] != "polaris"][-self.per_side:]
        polaris_tail = [r for r in self._rows if r["sender"] == "polaris"][-self.per_side:]
        keep = {id(r) for r in user_tail} | {id(r) for r in polaris_tail}
        out = [dict(r) for r in self._rows if id(r) in keep]
        for buf in self._streams.values():
            if buf["text"]:
                out.append({"sender": "polaris", "content": buf["text"],
                            "timestamp": buf["timestamp"], "partial": True})
        return out

    # -- dedupe internals ---------------------------------------------------- #
    def _digest_hit(self, content: str) -> bool:
        now = time.time()
        for key in [k for k, ts in self._digests.items() if now - ts > STREAM_DIGEST_TTL_S]:
            self._digests.pop(key, None)
        key = _digest_key(content)
        if key in self._digests:
            self._digests.pop(key, None)   # suppress exactly once
            return True
        return False

    # -- test surface -------------------------------------------------------- #
    def row_count(self) -> int:
        return len(self._rows)


class GatewayFeedClient:
    """ONE persistent Socket.IO connection serving the chat feed AND the task
    feed (modeled on the radar SpatialReporter in webcam_perception.py).

    Join sequence — re-fired on EVERY reconnect (dashboard v1.0.4 lesson):
      1. announce identity      → gateway joins us to the identity room
                                 (all devices of that operator)
      2. emit 'request_history' → 40-message seed replay
      3. emit 'join_tasks'      → 'status' room for the task feed
    Events funnel into feed_q; status_q carries ("feed", state) LED states.
    Reconnection is UNLIMITED and re-seeds on return, so an outage renders
    its missed messages when the link comes back. This thread NEVER touches
    Qt — the drain timer in lgi.py stays the single UI writer.
    """

    DEVICE_ID = "lgi-hud-x17"

    def __init__(self, base_url: str, identity: str = "rich",
                 feed_q: Optional[Queue] = None, status_q: Optional[Queue] = None,
                 log: Callable[[str], None] = print) -> None:
        self.base_url = str(base_url or "").rstrip("/")
        self.identity = str(identity or "rich").strip()
        self.feed_q = feed_q if feed_q is not None else Queue()
        self.status_q = status_q if status_q is not None else Queue()
        self._log = log
        self._sio = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # -- lifecycle ---------------------------------------------------------- #
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="lgi-feed", daemon=True)
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
        sio = self._sio
        return bool(sio is not None and sio.connected)

    # -- internals ------------------------------------------------------------ #
    def _run(self) -> None:
        try:
            import socketio
        except ImportError:
            self._log("[feed] python-socketio missing — always-listening disabled")
            self._put_status("offline")
            return
        # reconnection_attempts=0 ⇒ UNLIMITED (never the silent-death cap)
        self._sio = socketio.Client(reconnection=True, reconnection_attempts=0,
                                    request_timeout=10)
        self._sio.on("connect", self._on_connect)
        self._sio.on("disconnect", self._on_disconnect)
        self._sio.on("chat_history", self._on_chat_history)
        self._sio.on("chat_message", self._on_chat_message)
        self._sio.on("message", self._on_message)
        self._sio.on("tool_execution", self._on_tool_execution)
        self._sio.on("chat_tool_report", self._on_chat_tool_report)
        self._sio.on("active_tasks", self._on_active_tasks)
        for ev in ("task_registered", "task_started", "task_progress",
                   "task_completed", "task_failed"):
            self._sio.on(ev, self._make_task_handler(ev))
        while not self._stop.is_set():
            try:
                self._put_status("connecting")
                self._sio.connect(self.base_url)
                self._sio.wait()          # blocks while linked; exits on stop/down
            except Exception as exc:
                self._log(f"[feed] gateway feed down ({type(exc).__name__}) — retrying in 10 s")
            self._stop.wait(10.0)
        self._put_status("offline")

    def _on_connect(self) -> None:
        """Announce + seed + join — fires on every reconnect too."""
        self._put_status("online")
        display = self.identity.title()        # gateway names are 'Rich'/'Matt'
        self._safe_emit("message", {"type": "identity", "identity": display,
                                    "device": self.DEVICE_ID})
        self._safe_emit("request_history", {"device_id": display})
        self._safe_emit("join_tasks", {"room": "status"})
        self._log(f"[feed] always-listening link up ({self.base_url}) — identity "
                  f"'{display}' announced, seed + task room requested")

    def _on_disconnect(self, *_args) -> None:
        self._put_status("offline")
        self._log("[feed] link down — unlimited reconnect + seed-on-return active")

    # -- event pumps ---------------------------------------------------------- #
    def _on_chat_history(self, data: Any) -> None:
        if not isinstance(data, dict):
            return
        msgs = data.get("messages")
        if not isinstance(msgs, list):
            if data.get("status") == "error":
                self._log(f"[feed] history seed refused: {data.get('error')}")
            return
        rows = []
        for item in msgs:
            if not isinstance(item, dict):
                continue
            rows.append({
                "sender": str(item.get("sender") or item.get("role") or "user"),
                "content": str(item.get("content") or item.get("message") or ""),
                "timestamp": str(item.get("timestamp") or ""),
            })
        self.feed_q.put(("history", rows))

    def _on_chat_message(self, data: Any) -> None:
        if isinstance(data, dict):
            self.feed_q.put(("chat_message", data))

    def _on_message(self, data: Any) -> None:
        if not isinstance(data, dict):
            return
        msg_type = str(data.get("type") or "")
        if msg_type == "chat_stream":
            self.feed_q.put(("stream", data.get("stream_id"),
                             str(data.get("content") or ""),
                             bool(data.get("is_complete"))))
        elif msg_type == "user_chat":
            # WS user-to-user path — identical row semantics for the window
            self.feed_q.put(("chat_message", {
                "sender": str(data.get("senderName") or "user"),
                "content": str(data.get("content") or ""),
                "timestamp": str(data.get("timestamp") or ""),
            }))
        elif msg_type == "error":
            self._log(f"[feed] gateway error: {data.get('content', 'unknown')}")

    def _on_tool_execution(self, data: Any) -> None:
        if isinstance(data, dict):
            self.feed_q.put(("tool", data))

    def _on_chat_tool_report(self, data: Any) -> None:
        if isinstance(data, dict):
            self.feed_q.put(("tool_report", data))

    def _on_active_tasks(self, data: Any) -> None:
        self.feed_q.put(("tasks", data if isinstance(data, list) else []))

    def _make_task_handler(self, kind: str) -> Callable[[Any], None]:
        def handler(data: Any) -> None:
            if isinstance(data, dict):
                self.feed_q.put(("task", kind, data))
        return handler

    # -- helpers ----------------------------------------------------------------- #
    def _safe_emit(self, event: str, payload: Dict[str, Any]) -> None:
        try:
            sio = self._sio
            if sio is not None and sio.connected:
                sio.emit(event, payload)
        except Exception as exc:
            self._log(f"[feed] emit '{event}' failed: {exc}")

    def _put_status(self, state: str) -> None:
        try:
            self.status_q.put(("feed", state))
        except Exception:
            pass