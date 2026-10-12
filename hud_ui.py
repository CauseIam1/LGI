"""
LGI — Looking Glass Interface
hud_ui.py: Frameless floating HUD overlay (PyQt6).

v1.4 "Always Listening" (Phase 3): the v1.3 glass-panel HUD (header + LEDs +
current exchange + alert + supervisor line + control buttons) now carries
three new bands below the — intentionally unchanged — current-exchange area:

  * ⚙ settings pull-down (⋯ kebab in the header) — HUD font-size + HUD
    transparency sliders, live-applied and persisted in QSettings together
    with window geometry and the tactical collapse state.
    v1.4.1: transparency is whole-window setWindowOpacity — one knob fades
    the ENTIRE HUD (every surface, border, LED and text) uniformly; the
    painted QSS alphas stay fixed at the v1.3 glass values.

  * ⌁ LAST EXCHANGES — the always-listening rolling window (latest 5 per
    side, Looking-Glass seed v2 semantics), fed exclusively by the gateway
    feed queues; its own ⌁ LED shows the pipe's health, so messages typed on
    the phone or the dashboard type out live here.

  * ▼ TACTICAL OPERATIONS — the dashboard's bottom-half mirror, read-only:
    TASK FEED (rolling 50-event ▶/✓/✗ stream from the gateway 'status' room
    + an active-tasks snapshot line) and the MEMORY VAULT browser
    (category filter / search / open doc — no NEW/EDIT/REINDEX on the HUD).

Threading contract unchanged: HUDWindow mutators must only be called from
the Qt main thread. Worker threads communicate through queues drained by a
QTimer in lgi.py — single-threaded UI mutation, no cross-thread Qt calls.
"""

from __future__ import annotations

import time
from datetime import datetime
from html import escape as _html_escape
from typing import Any, Callable, Dict, List, Optional

from gateway_feed import TASK_ROW_STYLES  # task-row icon/color map (single source)

from PyQt6.QtCore import QSettings, Qt, QTimer
from PyQt6.QtGui import QColor, QFont, QGuiApplication
from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSizeGrip,
    QSlider,
    QStackedWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

BASE_FONT_PX = 14
BASE_PANEL_PCT = 94.0
FONT_MIN_PX = 10
FONT_MAX_PX = 24
PANEL_MIN_PCT = 40
PANEL_MAX_PCT = 100

HISTORY_PER_SIDE = 5
TASKFEED_WINDOW = 50

VAULT_CATEGORIES = [
    ("", "ALL"), ("core", "CORE"), ("architecture", "ARCHITECTURE"),
    ("user", "USER"), ("lesson", "LESSONS"), ("journal", "JOURNAL"),
    ("attachment", "ATTACHMENTS"),
]


def build_hud_qss(font_px: int = BASE_FONT_PX, panel_pct: float = BASE_PANEL_PCT) -> str:
    """Parameterized HUD stylesheet (v1.4; transparency ownership v1.4.1).

    font_px   — base UI font; every font-size scales off the 14px v1.3
                baseline (labels, bubbles, browsers, buttons).
    panel_pct — accepted for call-site compatibility only: since v1.4.1
                this QSS paints fixed v1.3 glass alphas and is NOT scaled
                by the transparency knob. Whole-HUD transparency is window
                opacity — HUDWindow._apply_style_now() applies
                setWindowOpacity(panel_pct / 100) so one slider fades the
                entire HUD (surfaces, borders, LEDs and text) uniformly.
    """
    fpx = max(FONT_MIN_PX, min(FONT_MAX_PX, int(font_px)))
    scale = fpx / 14.0

    def px(base: int) -> int:
        return max(9, int(round(base * scale)))

    def a(alpha: int) -> int:
        # Fixed v1.3 glass alphas: 94 keeps the v1.3 look; never scaled —
        # the transparency knob is whole-window opacity (no double-dim).
        return max(0, min(255, int(alpha)))

    return f"""QDialog#hud {{
    background-color: rgba(10, 14, 20, {a(240)});
    border: 1px solid rgba(0, 229, 255, {a(70)});
    border-radius: 14px;
}}
QLabel {{ color: #dbe7f3; font-family: 'Segoe UI'; font-size: {px(14)}px; }}
QLabel#title {{ color: #00e5ff; font-weight: bold; font-size: {px(14)}px; letter-spacing: 2px; }}
QLabel#sectitle {{ color: #00e5ff; font-weight: bold; font-size: {px(10)}px; letter-spacing: 1px; }}
QLabel#ledtag {{ color: #64748b; font-size: {px(9)}px; }}
QLabel#you {{
    color: #f8fafc; background-color: rgba(3, 7, 13, {a(120)});
    border: 1px solid rgba(0, 229, 255, {a(30)}); border-radius: 8px; padding: 6px 8px;
}}
QLabel#supline {{ color: #64748b; font-size: {px(12)}px; }}
QLabel#alert {{
    background-color: rgba(239, 68, 68, {a(45)});
    border: 1px solid #ef4444; border-radius: 8px;
    color: #fecaca; padding: 6px; font-size: {px(14)}px;
}}
QWidget#settings {{
    background-color: rgba(15, 23, 42, {a(210)});
    border: 1px solid rgba(0, 229, 255, {a(45)}); border-radius: 10px;
}}
QTextBrowser {{ color: #9ae6ff; background-color: rgba(3, 7, 13, {a(150)});
    border: 1px solid rgba(0, 229, 255, {a(45)}); border-radius: 8px;
    padding: 8px; font-family: 'Segoe UI'; font-size: {px(12)}px;
    selection-background-color: #164e63; }}
QTextBrowser#taskfeed {{ font-size: {px(11)}px; }}
QListWidget {{ color: #dbe7f3; background-color: rgba(3, 7, 13, {a(150)});
    border: 1px solid rgba(0, 229, 255, {a(45)}); border-radius: 8px;
    padding: 4px; font-family: 'Segoe UI'; font-size: {px(12)}px; outline: none; }}
QListWidget::item {{ padding: 4px 6px; border-radius: 6px; }}
QListWidget::item:selected {{ background-color: rgba(0, 229, 255, {a(60)}); color: #e0f7ff; }}
QListWidget::item:hover {{ background-color: rgba(0, 229, 255, {a(35)}); }}
QComboBox {{ background-color: rgba(15, 23, 42, {a(200)}); color: #93c5fd;
    border: 1px solid rgba(147, 197, 253, {a(60)}); border-radius: 8px;
    padding: 4px 8px; font-size: {px(12)}px; font-family: 'Segoe UI'; }}
QComboBox::drop-down {{ border: none; width: 18px; }}
QComboBox QAbstractItemView {{ background-color: rgba(10, 14, 20, 248); color: #93c5fd;
    border: 1px solid rgba(0, 229, 255, 80); selection-background-color: #164e63; }}
QLineEdit {{ background-color: rgba(3, 7, 13, {a(150)}); color: #e0f7ff;
    border: 1px solid rgba(0, 229, 255, {a(45)}); border-radius: 8px;
    padding: 4px 8px; font-size: {px(12)}px; font-family: 'Segoe UI'; }}
QLineEdit:focus {{ border-color: #00e5ff; }}
QPushButton {{ background-color: rgba(15, 23, 42, {a(200)}); color: #93c5fd;
    border: 1px solid rgba(147, 197, 253, {a(60)}); border-radius: 8px;
    padding: 5px 12px; font-size: {px(12)}px; font-family: 'Segoe UI'; }}
QPushButton:hover {{ border-color: #00e5ff; color: #00e5ff; }}
QPushButton#btn-settings {{ font-size: {px(14)}px; font-weight: bold; padding: 2px 8px; }}
QPushButton#btn-quit {{ color: #94a3b8; }}
QPushButton#ops_tab {{ color: #64748b; padding: 3px 10px; font-size: {px(11)}px; }}
QPushButton#ops_tab:checked {{ color: #00e5ff; border-color: rgba(0, 229, 255, 120); }}
QSlider {{ height: 16px; }}
QSlider::groove:horizontal {{ height: 4px; background: rgba(0, 229, 255, {a(60)}); border-radius: 2px; }}
QSlider::handle:horizontal {{ background: #00e5ff; width: 14px; margin: -6px 0; border-radius: 7px; }}
QScrollBar:vertical {{ background: rgba(15, 23, 42, {a(120)}); width: 8px; border-radius: 4px; margin: 0; }}
QScrollBar::handle:vertical {{ background: rgba(0, 229, 255, {a(90)}); border-radius: 4px; min-height: 24px; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}"""


class StatusDot(QLabel):
    """Round LED indicator with a fixed state->color map."""

    COLORS = {
        "off": "#4b5563",
        # mic
        "listening": "#22c55e", "muted": "#ef4444", "processing": "#f59e0b",
        # cam (webcam perception)
        "tracking": "#22c55e", "degraded": "#f59e0b",
        # supervisor
        "idle": "#4b5563", "scanning": "#0ea5e9", "ok": "#22c55e", "alert": "#ef4444",
        # polaris
        "online": "#22c55e", "offline": "#ef4444", "unknown": "#4b5563",
        # feed (always-listening link, v1.4)
        "link-up": "#22c55e", "link-wait": "#f59e0b", "link-down": "#ef4444",
    }

    def __init__(self, tooltip: str = "", parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setFixedSize(12, 12)
        self.setToolTip(tooltip)
        self.set_state("off")

    def set_state(self, state: str) -> None:
        color = self.COLORS.get(state, self.COLORS["off"])
        self.setStyleSheet(
            f"background-color: {color}; border-radius: 6px;"
            " min-width: 12px; min-height: 12px;"
        )


def _short_ts(ts: Any) -> str:
    """ISO/Z gateway stamps → a short 'HH:MM' clock tag (cosmetic only)."""
    text = str(ts or "").strip()
    if not text:
        return ""
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).strftime("%H:%M")
    except ValueError:
        return "…"


def _history_html(rows: List[Dict[str, Any]]) -> str:
    """Rolling-window HTML — two-line bubbles (sender · clock, content),
    latest at the bottom; live stream partials type out with a ▌ cursor.
    Only colors ride the HTML inline: font size + surfaces come from the
    widget stylesheet, so the ⚙ font/transparency sliders scale everything."""
    parts: List[str] = []
    for row in rows or []:
        polaris = str(row.get("sender") or "") == "polaris"
        name = "POLARIS" if polaris else str(row.get("sender") or "you").upper()
        content = _html_escape(str(row.get("content") or "")) or "&nbsp;"
        color = "#00b0d4" if polaris else "#f8fafc"
        bg = "rgba(3,7,13,150)" if polaris else "rgba(3,7,13,95)"
        if row.get("partial"):
            content += "▌"
            color = "#00e5ff"
        when = _short_ts(row.get("timestamp"))
        when_tail = f' <span style="color:#546a7d;">· {when}</span>' if when else ""
        parts.append(
            f'<div style="margin-bottom:6px;">'
            f'<span style="color:#64748b;">{name}</span>{when_tail}<br/>'
            f'<span style="color:{color}; background-color:{bg}; padding:2px 6px;">'
            f"{content}</span></div>"
        )
    return "".join(parts) or (
        '<div style="color:#64748b;">no messages yet — the feed seeds on link-up</div>'
    )


class HUDWindow(QDialog):
    """
    The floating LGI overlay window — v1.4 "Always Listening" edition.

    Top band unchanged (header LEDs, current voice exchange, alert + SUP).
    New: ⚙ settings pull-down (⋯ kebab), ⌁ last exchanges rolling window
    (latest 5 per side), and the ▼ TACTICAL OPERATIONS dashboard mirror
    (task feed + read-only vault). Operator-resizable via the bottom-right
    QSizeGrip; font size, transparency, collapse state and geometry persist
    via QSettings across restarts.
    """

    def __init__(self, font_px: int = BASE_FONT_PX, panel_pct: float = BASE_PANEL_PCT,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        # Handlers assigned by lgi.py after construction.
        self.on_mic_toggle: Callable[[], None] = lambda: None
        self.on_force_audit: Callable[[], None] = lambda: None
        self.on_toggle_alert_voice: Callable[[], None] = lambda: None
        # v1.6.3 — Voice is a persistent 🔇/🔊 master toggle; Cancel one-shots
        # the live playback (the v1.4 one-shot ⏹ Voice button is superseded).
        self.on_toggle_voice: Callable[[], None] = lambda: None
        self.on_cancel_voice: Callable[[], None] = lambda: None
        self.on_pause_voice: Callable[[], None] = lambda: None
        self.on_resume_voice: Callable[[], None] = lambda: None
        self._voice_on: bool = True       # echo of LGI voice_enabled (LGI is the source of truth)
        self._voice_paused: bool = False  # echo of TTS playback state (authoritative pause dispatch)
        self.on_quit: Callable[[], None] = lambda: None
        self.on_vault_browse: Callable[[str], None] = lambda category: None
        self.on_vault_search: Callable[[str, str], None] = lambda query, category: None
        self.on_vault_open: Callable[[str], None] = lambda path: None

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        # v1.4.1: the transparency knob IS whole-window opacity — one knob
        # fades the ENTIRE HUD (surfaces, borders, LEDs, text). The persisted
        # panel_pct value is applied by _apply_style_now() at boot below.
        self.setWindowOpacity(1.0)

        # QSettings persistence: font, transparency, ops collapse, geometry.
        self._settings = QSettings("Polaris", "LGI")
        stored_font = self._settings.value("hud/font_px", font_px, type=int)
        stored_pct = self._settings.value("hud/panel_pct", panel_pct, type=float)
        self._font_px = max(FONT_MIN_PX, min(FONT_MAX_PX, int(stored_font)))
        self._panel_pct = max(float(PANEL_MIN_PCT),
                              min(float(PANEL_MAX_PCT), float(stored_pct)))
        self._ops_collapsed = self._settings.value("hud/ops_collapsed", False, type=bool)

        self._drag_offset = None
        self._task_rows: List[str] = []
        self._task_active = 0
        self._style_timer = QTimer(self)
        self._style_timer.setSingleShot(True)
        self._style_timer.setInterval(150)
        self._style_timer.timeout.connect(self._apply_style_now)

        self._build_ui()
        self._restore_geometry()
        self._apply_style_now()

    # ------------------------------------------------------------------ #
    # UI construction
    # ------------------------------------------------------------------ #
    def _build_ui(self) -> None:
        self.setObjectName("hud")
        # Resizable overlay: larger default for the three-band HUD; resizing
        # still rides the bottom-right QSizeGrip (frameless window).
        self.resize(640, 920)
        self.setMinimumSize(480, 560)

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 10)
        root.setSpacing(6)

        # ── header: title + status LEDs + ⋯ settings kebab (top unchanged) ─ #
        header = QHBoxLayout()
        title = QLabel("◈ LGI — LOOKING GLASS")
        title.setObjectName("title")
        header.addWidget(title)
        header.addStretch(1)

        self._dot_mic = StatusDot("Microphone state")
        self._dot_cam = StatusDot("Webcam perception state")
        self._dot_sup = StatusDot("Supervisor audit state")
        self._dot_pol = StatusDot("Polaris gateway link")
        for tag_text, dot in (
            ("MIC", self._dot_mic), ("CAM", self._dot_cam),
            ("SUP", self._dot_sup), ("POL", self._dot_pol),
        ):
            header.addWidget(dot)
            tag = QLabel(tag_text)
            tag.setObjectName("ledtag")
            header.addWidget(tag)

        self._btn_settings = QPushButton("⋯")
        self._btn_settings.setObjectName("btn-settings")
        self._btn_settings.setFixedSize(34, 26)
        self._btn_settings.setToolTip("HUD settings — font size & transparency")
        self._btn_settings.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_settings.clicked.connect(self._toggle_settings)
        header.addWidget(self._btn_settings)
        root.addLayout(header)

        # ── ⚙ settings pull-down (hidden until ⋯ is clicked) ─────────────── #
        self._settings_panel = QWidget(self)
        self._settings_panel.setObjectName("settings")
        panel = QVBoxLayout(self._settings_panel)
        panel.setContentsMargins(10, 8, 10, 8)
        panel.setSpacing(5)

        panel_head = QLabel("⚙ HUD SETTINGS")
        panel_head.setObjectName("title")
        panel.addWidget(panel_head)

        font_row = QHBoxLayout()
        font_label = QLabel("FONT SIZE")
        font_label.setObjectName("sectitle")
        self._font_value = QLabel(f"{self._font_px} px")
        self._font_value.setObjectName("ledtag")
        self._font_slider = QSlider(Qt.Orientation.Horizontal)
        self._font_slider.setRange(FONT_MIN_PX, FONT_MAX_PX)
        self._font_slider.setValue(self._font_px)
        self._font_slider.valueChanged.connect(self._on_font_slider)
        font_row.addWidget(font_label)
        font_row.addWidget(self._font_slider, 1)
        font_row.addWidget(self._font_value)
        panel.addLayout(font_row)

        pct_row = QHBoxLayout()
        pct_label = QLabel("TRANSPARENCY")
        pct_label.setObjectName("sectitle")
        self._pct_value = QLabel(f"{int(self._panel_pct)}% solid")
        self._pct_value.setObjectName("ledtag")
        self._pct_slider = QSlider(Qt.Orientation.Horizontal)
        self._pct_slider.setRange(int(PANEL_MIN_PCT), int(PANEL_MAX_PCT))
        self._pct_slider.setValue(int(self._panel_pct))
        self._pct_slider.valueChanged.connect(self._on_pct_slider)
        pct_row.addWidget(pct_label)
        pct_row.addWidget(self._pct_slider, 1)
        pct_row.addWidget(self._pct_value)
        panel.addLayout(pct_row)
        root.addWidget(self._settings_panel)
        self._settings_panel.hide()

        # ── live voice exchange (top band unchanged, v1.3 layout) ────────── #
        self._you_line = QLabel("You: …")
        self._you_line.setObjectName("you")
        self._you_line.setWordWrap(True)
        self._you_line.setMaximumHeight(110)
        root.addWidget(self._you_line)

        # Polaris transcript: read-only browser so long replies wrap AND
        # scroll instead of being truncated to a single short label.
        self._polaris_line = QTextBrowser()
        self._polaris_line.setObjectName("polaris")
        self._polaris_line.setReadOnly(True)
        self._polaris_line.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._polaris_line.setOpenExternalLinks(False)
        self._polaris_line.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._polaris_line.document().setDefaultFont(QFont("Segoe UI", 10))
        self._polaris_line.setTextColor(QColor("#9ae6ff"))
        self._polaris_line.setMinimumHeight(110)
        root.addWidget(self._polaris_line, 2)   # absorbs extra window height

        # Alert banner (hidden until a SUPERVISOR_ALERT lands)
        self._alert_banner = QLabel("")
        self._alert_banner.setObjectName("alert")
        self._alert_banner.setWordWrap(True)
        self._alert_banner.hide()
        root.addWidget(self._alert_banner)

        # Supervisor verdict line
        self._sup_line = QLabel("SUP: idle")
        self._sup_line.setObjectName("supline")
        root.addWidget(self._sup_line)

        # ── ⌁ LAST EXCHANGES — always-listening rolling window (v1.4) ────── #
        hist_head = QHBoxLayout()
        hist_title = QLabel("⌁ LAST EXCHANGES — 5 PER SIDE (ALWAYS LISTENING)")
        hist_title.setObjectName("sectitle")
        hist_head.addWidget(hist_title)
        hist_head.addStretch(1)
        self._dot_feed = StatusDot("Gateway feed link (chat + task stream)")
        self._dot_feed.set_state("off")
        feed_tag = QLabel("FEED")
        feed_tag.setObjectName("ledtag")
        hist_head.addWidget(self._dot_feed)
        hist_head.addWidget(feed_tag)
        root.addLayout(hist_head)

        self._history_browser = QTextBrowser()
        self._history_browser.setObjectName("history")
        self._history_browser.setReadOnly(True)
        self._history_browser.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._history_browser.setOpenExternalLinks(False)
        self._history_browser.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._history_browser.setHtml(_history_html([]))
        self._history_browser.setMinimumHeight(120)
        root.addWidget(self._history_browser, 2)

        # ── ▼ TACTICAL OPERATIONS — dashboard bottom-half mirror (v1.4) ──── #
        self._ops_toggle = QPushButton("▼ TACTICAL OPERATIONS")
        self._ops_toggle.setObjectName("ops_tab")
        self._ops_toggle.setCheckable(True)
        self._ops_toggle.setChecked(not self._ops_collapsed)
        self._ops_toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self._ops_toggle.clicked.connect(self._toggle_ops)
        self._tab_task = QPushButton("TASK FEED")
        self._tab_task.setObjectName("ops_tab")
        self._tab_task.setCheckable(True)
        self._tab_task.setChecked(True)
        self._tab_task.clicked.connect(lambda: self._switch_ops_tab(0))
        self._tab_vault = QPushButton("MEMORY VAULT")
        self._tab_vault.setObjectName("ops_tab")
        self._tab_vault.setCheckable(True)
        self._tab_vault.clicked.connect(lambda: self._switch_ops_tab(1))

        ops_head = QHBoxLayout()
        ops_head.addWidget(self._ops_toggle)
        ops_head.addStretch(1)
        ops_head.addWidget(self._tab_task)
        ops_head.addWidget(self._tab_vault)
        root.addLayout(ops_head)

        # TASK FEED page: activity snapshot line + 50-event rolling browser
        self._task_status = QLabel("…")
        self._task_status.setObjectName("ledtag")
        self._task_browser = QTextBrowser()
        self._task_browser.setObjectName("taskfeed")
        self._task_browser.setReadOnly(True)
        self._task_browser.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._task_browser.setOpenExternalLinks(False)
        self._task_browser.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._task_browser.setMinimumHeight(90)
        self._task_browser.setHtml(
            '<div style="color:#64748b;">waiting for the status room…</div>')
        task_page = QWidget()
        task_layout = QVBoxLayout(task_page)
        task_layout.setContentsMargins(0, 0, 0, 0)
        task_layout.setSpacing(4)
        task_layout.addWidget(self._task_status)
        task_layout.addWidget(self._task_browser, 1)

        # MEMORY VAULT page (read-only mirror): status, toolbar, list, viewer
        self._vault_status = QLabel("GATEWAY OFFLINE")
        self._vault_status.setObjectName("ledtag")
        self._vault_cat = QComboBox()
        for _value, _label in VAULT_CATEGORIES:
            self._vault_cat.addItem(_label, _value)
        self._vault_cat.currentIndexChanged.connect(self._emit_vault_browse)
        self._vault_search = QLineEdit()
        self._vault_search.setPlaceholderText("Search the vault… (hybrid)")
        self._vault_search.returnPressed.connect(self._emit_vault_search)
        self._btn_vault_browse = QPushButton("BROWSE")
        self._btn_vault_browse.clicked.connect(self._emit_vault_browse)
        self._btn_vault_search = QPushButton("SEARCH")
        self._btn_vault_search.clicked.connect(self._emit_vault_search)

        vault_toolbar = QHBoxLayout()
        vault_toolbar.addWidget(self._vault_cat, 1)
        vault_toolbar.addWidget(self._vault_search, 2)
        vault_toolbar.addWidget(self._btn_vault_browse)
        vault_toolbar.addWidget(self._btn_vault_search)

        self._vault_list = QListWidget()
        self._vault_list.itemClicked.connect(self._emit_vault_open)
        self._vault_list.setToolTip("Click a document to open it (read-only HUD mirror)")
        self._vault_view = QTextBrowser()
        self._vault_view.setObjectName("vault_view")
        self._vault_view.setReadOnly(True)
        self._vault_view.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._vault_view.setOpenExternalLinks(False)
        self._vault_view.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._vault_view.setHtml('<div style="color:#64748b;">select a document…</div>')

        vault_page = QWidget()
        vault_layout = QVBoxLayout(vault_page)
        vault_layout.setContentsMargins(0, 0, 0, 0)
        vault_layout.setSpacing(4)
        vault_layout.addWidget(self._vault_status)
        vault_layout.addLayout(vault_toolbar)
        vault_layout.addWidget(self._vault_list, 1)
        vault_layout.addWidget(self._vault_view, 1)

        self._ops_container = QWidget(self)
        self._ops_stack = QStackedWidget(self._ops_container)
        self._ops_stack.addWidget(task_page)      # index 0 — TASK FEED
        self._ops_stack.addWidget(vault_page)     # index 1 — MEMORY VAULT
        ops_layout = QVBoxLayout(self._ops_container)
        ops_layout.setContentsMargins(0, 0, 0, 0)
        ops_layout.setSpacing(0)
        ops_layout.addWidget(self._ops_stack)
        self._ops_container.setMinimumHeight(170)
        root.addWidget(self._ops_container, 3)

        # ── manual control buttons (unchanged) ───────────────────────────── #
        buttons = QHBoxLayout()
        self._btn_mic = QPushButton("🎤 Mic On")
        self._btn_mic.clicked.connect(self._mic_clicked)
        self._btn_audit = QPushButton("⚡ Audit")
        self._btn_audit.clicked.connect(lambda: self.on_force_audit())
        self._btn_alert_voice = QPushButton("🔇 Advisories")
        self._btn_alert_voice.setToolTip("Toggle verbal supervisor alerts (LED + banner always shown)")
        self._btn_alert_voice.clicked.connect(lambda: self.on_toggle_alert_voice())
        self._btn_pause = QPushButton("⏸ Pause")
        self._btn_pause.setToolTip("Hold Polaris speech mid-sentence (▶ resumes at the exact point later)")
        self._btn_pause.clicked.connect(self._pause_clicked)
        self._btn_pause.setEnabled(False)   # state-driven via set_voice_playback()
        # v1.6.3 — 🔇/🔊 Voice master toggle (persistent) + ⏹ Cancel one-shot.
        self._btn_voice = QPushButton("🔊 Voice")
        self._btn_voice.setToolTip(
            "Toggle POLARIS speech on/off — 🔇 stops current playback and mutes ALL future replies (LEDs/alerts/mic unaffected)")
        self._btn_voice.clicked.connect(lambda: self.on_toggle_voice())
        self._btn_cancel = QPushButton("⏹ Cancel")
        self._btn_cancel.setToolTip(
            "Cancel playback now — stops the current utterance and drops everything queued (future speech stays enabled)")
        self._btn_cancel.clicked.connect(lambda: self.on_cancel_voice())
        self._btn_cancel.setEnabled(False)  # state-driven via set_voice_playback()
        self._btn_quit = QPushButton("✕")
        self._btn_quit.setObjectName("btn-quit")
        self._btn_quit.setFixedWidth(32)
        self._btn_quit.setToolTip("Quit LGI")
        self._btn_quit.clicked.connect(lambda: self.on_quit())
        for btn in (self._btn_mic, self._btn_audit, self._btn_alert_voice,
                    self._btn_voice, self._btn_pause, self._btn_cancel):
            buttons.addWidget(btn)
        buttons.addStretch(1)
        buttons.addWidget(self._btn_quit)
        root.addLayout(buttons)

        # Resize grip — works on frameless translucent Tool windows.
        grip_row = QHBoxLayout()
        grip_row.addStretch(1)
        _grip = QSizeGrip(self)
        _grip.setFixedSize(20, 20)
        grip_row.addWidget(_grip)
        root.addLayout(grip_row)

        # Boot state: tactical collapse per persisted preference.
        if self._ops_collapsed:
            self._ops_container.setVisible(False)
            self._ops_toggle.setText("▶ TACTICAL OPERATIONS")

    # ------------------------------------------------------------------ #
    # public mutators (Qt main thread only — driven by lgi.py's drain timer)
    # ------------------------------------------------------------------ #
    def set_led(self, target: str, state: str) -> None:
        if target == "feed":
            state = {"online": "link-up", "connecting": "link-wait",
                     "offline": "link-down"}.get(state, "off")
        dot = {
            "mic": self._dot_mic,
            "cam": self._dot_cam,
            "supervisor": self._dot_sup,
            "polaris": self._dot_pol,
            "feed": self._dot_feed,
        }.get(target)
        if dot is not None:
            dot.set_state(state)

    def set_exchange(self, you: Optional[str], polaris: Optional[str]) -> None:
        if polaris is not None:
            self._polaris_line.setPlainText("Polaris: " + _elide(polaris, 4000))
            self._polaris_line.verticalScrollBar().setValue(0)
        if you is not None:
            self._you_line.setText("You: " + _elide(you, 300))

    def set_ambient(self, text: str) -> None:
        self._you_line.setText("Ambient: " + _elide(text, 300))

    def set_supervisor_line(self, text: str) -> None:
        self._sup_line.setText("SUP: " + _elide(text, 90))

    def show_alert(self, category: str, reason: str) -> None:
        text = f"⚠ {str(category).upper()}"
        if reason:
            text += f" — {_elide(reason, 140)}"
        self._alert_banner.setText(text)
        self._alert_banner.show()

    def hide_alert(self) -> None:
        self._alert_banner.clear()
        self._alert_banner.hide()

    def set_mic_button(self, listening: bool) -> None:
        self._btn_mic.setText("🎤 Mic On" if listening else "🎤 Muted")

    def set_alert_voice_button(self, enabled: bool) -> None:
        self._btn_alert_voice.setText("🔊 Advisories" if enabled else "🔇 Advisories")

    def set_voice_button(self, enabled: bool) -> None:
        """v1.6.3 — 🔇/🔊 Voice echo; LGI flips the model and reports back."""
        self._voice_on = enabled
        self._btn_voice.setText("🔊 Voice" if enabled else "🔇 Voice")

    def set_voice_playback(self, state: str) -> None:
        """State-aware ⏸/⏹ strip — disabled at rest, live while speaking/paused."""
        paused = state == "paused"
        live = state in ("speaking", "paused")
        self._voice_paused = paused  # authoritative dispatch state; text is cosmetic
        self._btn_pause.setEnabled(live)
        self._btn_pause.setText("▶ Resume" if paused else "⏸ Pause")
        self._btn_cancel.setEnabled(live)

    def _pause_clicked(self) -> None:
        # v1.6.3 — dispatch on the authoritative paused flag, not the cosmetic
        # text: a pause→re-click racing the state echo previously re-sent
        # pause() (a silent no-op) instead of resume().
        if self._voice_paused:
            self.on_resume_voice()
        else:
            self.on_pause_voice()

    # -- v1.4 always-listening feed mutators --------------------------------- #
    def set_history(self, rows: List[Dict[str, Any]]) -> None:
        """Re-render the ⌁ window; the view follows fresh content only when
        already at the bottom (reading up is preserved)."""
        browser = self._history_browser
        scroll = browser.verticalScrollBar()
        at_bottom = scroll.value() >= scroll.maximum() - 8
        browser.setHtml(_history_html(rows))
        if at_bottom:
            scroll.setValue(scroll.maximum())

    def set_tasks_snapshot(self, tasks: List[Dict]) -> None:
        self._task_active = len(tasks or [])
        self._render_task_status()

    def _render_task_status(self) -> None:
        self._task_status.setText(
            f"⌁ feed live — {self._task_active} active task(s) — "
            f"rolling {TASKFEED_WINDOW}-event window")

    def append_task(self, kind: str, task: Dict) -> None:
        """One task_* lifecycle event — newest on top (dashboard parity)."""
        icon, color = TASK_ROW_STYLES.get(kind, ("•", "#64748b"))
        task_id = str(task.get("task_id") or task.get("id")
                      or task.get("name") or "unknown task")
        when = _short_ts(task.get("timestamp")) or time.strftime("%H:%M:%S")
        label = str(task.get("description") or "").strip()
        label_html = (f' — <span style="color:#8fa8bd;">{_html_escape(label[:90])}</span>'
                      if label else "")
        self._task_rows.append(
            f'<div style="margin-bottom:4px;">'
            f'<span style="color:{color}; font-weight:bold;">{icon}</span> '
            f'<span style="color:{color};">{_html_escape(kind.replace("task_", "").upper())}</span> '
            f'<span style="color:#dbe7f3;">{_html_escape(task_id[:60])}</span>'
            f'<span style="color:#64748b;"> · {when}</span>{label_html}</div>'
        )
        self._task_rows = self._task_rows[-TASKFEED_WINDOW:]
        self._task_browser.setHtml("".join(reversed(self._task_rows)))

    # -- v1.4 memory vault mutators (read-only mirror) -------------------------- #
    def set_vault_state(self, text: str) -> None:
        self._vault_status.setText(_elide(text, 110))

    def set_vault_docs(self, docs: List[Dict]) -> None:
        """Doc index rows — dashboard renderVaultDocs() parity (read-only)."""
        self._vault_list.clear()
        for doc in docs:
            path = str(doc.get("path") or "")
            title = str(doc.get("title") or path or "untitled")
            meta: List[str] = []
            if doc.get("category"):
                meta.append(str(doc["category"]))
            if doc.get("updated"):
                meta.append(str(doc["updated"]).replace("T", " ")[:16])
            if doc.get("score") is not None:
                meta.append(f"score {doc.get('score')}"
                            + (f" ({doc.get('match_type')})" if doc.get("match_type") else ""))
            meta_text = " • ".join(meta)
            snippet = str(doc.get("snippet") or "")[:160]
            tail = f"\n{snippet}" if snippet else ""
            item = QListWidgetItem(f"{title}\n{meta_text}{tail}")
            item.setToolTip(snippet or title)
            item.setData(Qt.ItemDataRole.UserRole, path)
            self._vault_list.addItem(item)

    def set_vault_doc_view(self, path: str, title: str, content: str,
                           modified: str) -> None:
        head = (
            f'<div style="color:#00e5ff; font-weight:bold;">{_html_escape(title[:120])}</div>'
            f'<div style="color:#64748b;">{_html_escape(path)} · mod '
            f'{_html_escape(str(modified).replace("T", " ")[:16])}</div><hr/>'
        )
        body = _html_escape(content).replace("\n", "<br/>")
        self._vault_view.setHtml(head + (body or "(empty document)"))

    # ------------------------------------------------------------------ #
    # internals
    # ------------------------------------------------------------------ #
    def _emit_vault_browse(self, *_ignored) -> None:
        self.on_vault_browse(self._vault_cat.currentData() or "")

    def _emit_vault_search(self, *_ignored) -> None:
        self.on_vault_search(self._vault_search.text().strip(),
                             self._vault_cat.currentData() or "")

    def _emit_vault_open(self, item: QListWidgetItem) -> None:
        path = item.data(Qt.ItemDataRole.UserRole)
        if path:
            self.on_vault_open(path)

    def _mic_clicked(self) -> None:
        # Text is corrected by the listener status callback after the toggle
        # propagates; flip optimistically for instant feedback.
        self._btn_mic.setText("🎤 Muted" if "Mic On" in self._btn_mic.text() else "🎤 Mic On")
        self.on_mic_toggle()

    # -- ⚙ settings panel (v1.4) -------------------------------------------- #
    def _toggle_settings(self) -> None:
        self._settings_panel.setVisible(not self._settings_panel.isVisible())

    def _on_font_slider(self, value: int) -> None:
        self._font_px = max(FONT_MIN_PX, min(FONT_MAX_PX, int(value)))
        self._font_value.setText(f"{self._font_px} px")
        self._style_timer.start()

    def _on_pct_slider(self, value: int) -> None:
        self._panel_pct = max(float(PANEL_MIN_PCT),
                              min(float(PANEL_MAX_PCT), float(value)))
        self._pct_value.setText(f"{int(self._panel_pct)}% solid")
        self._style_timer.start()

    def _switch_ops_tab(self, index: int) -> None:
        self._ops_stack.setCurrentIndex(index)
        self._tab_task.setChecked(index == 0)
        self._tab_vault.setChecked(index == 1)

    def _toggle_ops(self) -> None:
        collapsed = self._ops_container.isVisible()
        self._ops_container.setVisible(not collapsed)
        self._ops_toggle.setText("▶ TACTICAL OPERATIONS" if collapsed
                                 else "▼ TACTICAL OPERATIONS")
        self._ops_toggle.setChecked(not collapsed)
        self._ops_collapsed = collapsed
        self._settings.setValue("hud/ops_collapsed", collapsed)

    def _apply_style_now(self) -> None:
        """Rebuild the parameterized stylesheet (font knob) and re-apply the
        whole-HUD transparency knob: panel_pct % → window opacity, so one
        slider fades the ENTIRE HUD — every surface, border, LED and text —
        uniformly (v1.4.1). The polaris transcript's document-level font
        rides the slider too (pt ≈ 0.75 × px — the v1.3 14px/10pt pairing
        rescaled)."""
        self.setStyleSheet(build_hud_qss(self._font_px, self._panel_pct))
        self.setWindowOpacity(self._panel_pct / 100.0)
        self._polaris_line.document().setDefaultFont(
            QFont("Segoe UI", max(7, int(round(self._font_px * 0.75)))))
        self._settings.setValue("hud/font_px", self._font_px)
        self._settings.setValue("hud/panel_pct", self._panel_pct)

    # -- geometry (persisted) ------------------------------------------------- #
    def _restore_geometry(self) -> None:
        geo = self._settings.value("hud/geometry")
        if geo is not None:
            try:
                if self.restoreGeometry(geo):
                    return
            except Exception:
                pass
        self._position_top_right()

    def closeEvent(self, event) -> None:
        try:
            self._settings.setValue("hud/geometry", self.saveGeometry())
            self._settings.setValue("hud/font_px", self._font_px)
            self._settings.setValue("hud/panel_pct", self._panel_pct)
            self._settings.setValue("hud/ops_collapsed", self._ops_collapsed)
        except Exception:
            pass
        super().closeEvent(event)

    def _position_top_right(self) -> None:
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            self.move(1200, 24)
            return
        geo = screen.availableGeometry()
        self.move(geo.topRight().x() - self.width() - 24, geo.topRight().y() + 24)

    # -- frameless dragging -------------------------------------------------- #
    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._drag_offset is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_offset)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        self._drag_offset = None
        super().mouseReleaseEvent(event)


def _elide(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"