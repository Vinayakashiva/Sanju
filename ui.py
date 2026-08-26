"""
ui.py — Sanju AI
• VRM avatar overlay (vrm_avatar/web) fills the card — no GIF, no green
  waveform/equalizer. Speech is shown via the avatar's own lip-sync.
• Still a small frameless floating overlay, same set_state/update_text API
  as before so main.py needs zero changes (update_text/stream_response are
  still no-ops that simply don't render anything).
"""

import json
import math
import os
import random
import time

from PyQt6.QtCore    import (Qt, QTimer, QPropertyAnimation, QPoint,
                              QEasingCurve, QSize, QRect, QRectF, QUrl,
                              QObject, pyqtSlot)
from PyQt6.QtGui     import (QMovie, QPainter, QColor, QPen, QFont,
                              QPainterPath, QFontMetrics, QLinearGradient,
                              QBrush)
from PyQt6.QtWidgets import QWidget, QApplication, QLabel, QVBoxLayout
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWebEngineCore    import QWebEngineSettings
from PyQt6.QtWebChannel       import QWebChannel


# ── Overall card geometry ───────────────────────────────────────────────────────
CARD_W, CARD_H = 300, 300     # square-ish card, GIF fills the whole thing now
CARD_RADIUS    = 16
CARD_FILL_ALPHA = 238          # 0=fully see-through, 255=fully solid

GIF_W, GIF_H = CARD_W, CARD_H  # GIF fills the entire card — no text column

WIN_W, WIN_H = CARD_W, CARD_H

WAVE_BAR_COUNT = 22
WAVE_AREA_H    = 26

STATE_PALETTE = {
    "IDLE":      (64,  140, 220),
    "LISTENING": (56,  190, 170),
    "THINKING":  (140, 110, 220),
    "SPEAKING":  (56,  180, 130),
    "SLEEP":     (40,   55,  80),
    "TEASING":   (210, 110, 150),
    "LOVING":    (200,  80,  95),
    "HAPPY":     (215, 170,  70),
    "SHY":       (210, 130,  95),
    # ── Companion mood states (companion_state.py) ───────────────────────
    # These render via the same procedural pulse fallback as any other
    # state without a matching assets/<state>.gif — drop in gifs later
    # (assets/excited.gif, etc.) and they'll be picked up automatically,
    # no code changes needed.
    "EXCITED":   (230, 140,  40),
    "FOCUSED":   (90,  120, 210),
    "RELAXED":   (100, 170, 150),
    "CONCERNED": (190,  90,  70),
    "PROUD":     (180, 150,  60),
    "CURIOUS":   (120, 160, 210),
}


# ── VRM avatar wiring ───────────────────────────────────────────────────────────
# vrm_avatar/web/index.html + app.js already exist in this project (built
# for the standalone assistant.py app) and expose window.playAnimation /
# setTalking / setMouthLevel / setViseme once window.__controllerReady is
# true — see vrm_bridge.py for the reference implementation this mirrors.
VRM_BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
VRM_INDEX_HTML = os.path.join(VRM_BASE_DIR, "vrm_avatar", "web", "index.html")

# Every clip name here must match one loaded in vrm_avatar/web/app.js's
# clip list, which in turn must match an actual file in vrm_avatar/animations
# (see the project's animations folder — Idle.fbx, wait.fbx, Walk.fbx,
# Walk2.fbx, lazy.fbx, clap.fbx, sit.fbx, Sitting Idle.fbx, Happy.fbx,
# happy_dance.fbx, dance.fbx, Hip Hop Dancing.fbx, Breakdance 1990.fbx,
# Waving.fbx, Standing Greeting.fbx, Standing Up.fbx, Catwalk Walk.fbx,
# cat_walk.fbx).
SLEEP_STATES  = {"SLEEP", "SLEEPING"}
HAPPY_STATES  = {"HAPPY", "EXCITED", "LOVING", "PROUD", "TEASING"}
SING_STATES   = {"SINGING"}
# Lip-sync only makes sense while she's actually speaking/singing, not
# while typing — TYPING gets its own two-step animation instead (below).
TALKING_STATES = {"SPEAKING", "SINGING"}

# Random per-play variety so she doesn't repeat the exact same clip every
# time, same pattern assistant.py already uses for walk clips.
GREETING_CLIPS = ["waving", "standing_greeting"]
DANCE_CLIPS    = ["dance", "happy_dance", "hiphop", "breakdance",
                   "dancing1", "dancing_maraschino", "snake_hiphop"]
POSE_CLIPS     = ["pose1", "pose2", "pose3", "pose4"]
# One-shot clip -> the clip to settle into once it finishes (see
# _on_animation_finished). Greetings settle into whatever the real target
# state was (handled separately, since that varies); this covers the
# fixed two-step sequences.
LEAD_IN_LOOP = {
    "sit_to_type": "typing",
}


class _AvatarBridge(QObject):
    """Exposed to the page as window.pyBridge — same contract as
    vrm_bridge.py's AssistantBridge. animationFinished is how app.js
    acknowledges a one-shot clip (e.g. a greeting) has actually completed,
    used here to settle into the *real* target state's pose afterward
    instead of app.js's default fallback-to-idle."""

    def __init__(self, on_finished):
        super().__init__()
        self._on_finished = on_finished

    @pyqtSlot(str)
    def animationFinished(self, name: str):
        self._on_finished(name)

    @pyqtSlot(str)
    def logFromPage(self, message: str):
        print(f"[AvatarWidget/web] {message}")


# ═══════════════════════════════════════════════════════════════════════════════
class AvatarWidget(QWidget):
    """Same public surface main.py relies on (set_state, .state,
    .dynamic_pulse, .update()) — internals now render the VRM avatar via
    QWebEngineView instead of a GIF via QMovie."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(GIF_W, GIF_H)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.state = "IDLE"

        self._dynamic_pulse    = 0
        self._controller_ready = False
        self._js_queue          = []  # [(fn_name, args)]
        self._pending_settle_state = None  # target state to settle into once
                                            # a greeting clip finishes (see
                                            # _on_animation_finished below)
        self._last_dance = None            # avoid repeating the same dance twice in a row
        self._dance_bag  = []              # shuffle-bag: all 7 clips get used
                                            # once each before any repeat (see
                                            # _next_dance_clip below), instead
                                            # of a plain random.choice which
                                            # could replay the same handful of
                                            # clips over and over by chance.

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.view = QWebEngineView(self)
        self.view.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.view.setStyleSheet("background: transparent;")
        # Without this the WebEngine surface paints an opaque rect behind
        # the canvas even though the page's own CSS is "transparent".
        self.view.page().setBackgroundColor(Qt.GlobalColor.transparent)
        layout.addWidget(self.view)

        settings = self.view.settings()
        # Required for the file:// page to import three.js/@pixiv/three-vrm
        # from a CDN via index.html's <script type="importmap">.
        settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True)
        settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, True)
        settings.setAttribute(QWebEngineSettings.WebAttribute.WebGLEnabled, True)
        settings.setAttribute(QWebEngineSettings.WebAttribute.Accelerated2dCanvasEnabled, True)

        # QWebChannel: lets app.js call back into Python (animation-finished
        # acknowledgement, debug logging) — mirrors vrm_bridge.py.
        self.channel = QWebChannel(self.view.page())
        self._bridge = _AvatarBridge(self._on_animation_finished)
        self.channel.registerObject("pyBridge", self._bridge)
        self.view.page().setWebChannel(self.channel)

        if os.path.isfile(VRM_INDEX_HTML):
            self.view.loadFinished.connect(self._on_load_finished)
            self.view.load(QUrl.fromLocalFile(VRM_INDEX_HTML))
        else:
            print(f"[AvatarWidget] VRM page not found at {VRM_INDEX_HTML} — "
                  "avatar will stay blank until vrm_avatar/web/index.html exists.")

    # ── public API main.py calls ────────────────────────────────────────
    def set_state(self, state: str):
        prev = self.state
        self.state = state

        # Lip-sync only while actually speaking/singing — no separate
        # "talking" body-loop clip is used, so this just arms/disarms
        # viseme blending in app.js; whatever pose she's already holding
        # (idle/wait/etc.) keeps playing underneath it.
        self._call_js("setTalking", state in TALKING_STATES)

        # Waking up from sleep (app start, wake word, or the sleep timer
        # timing out and then being woken again) — play a random greeting
        # once, then settle into whatever `state` actually calls for.
        if prev in SLEEP_STATES and state not in SLEEP_STATES:
            self._pending_settle_state = state
            self._call_js("playAnimation", random.choice(GREETING_CLIPS))
            return

        # Entering TYPING freshly (not already typing) — sit down once,
        # then settle into the looping typing clip once that finishes.
        if state == "TYPING" and prev != "TYPING":
            self._pending_settle_state = "TYPING"
            self._call_js("playAnimation", "sit_to_type")
            return

        # Already dancing — don't force a brand new clip on every redundant
        # set_state("SINGING") call. main.py calls set_state("SINGING")
        # every time she finishes a line of speech while music_playing is
        # True (see _stay_listening()/_back_to_idle()), just to keep the
        # dancing "sticky." Without this guard, each of those calls was
        # yanking her out of whatever dance clip was currently mid-play and
        # forcing a new one — and app.js's crossfade drops the interrupted
        # clip's 'finished' event, so the chain could visibly stall on
        # whatever pose she got hard-cut into. The actual clip-to-clip
        # chaining is entirely driven by _on_animation_finished below, once
        # per clip completing naturally — this just has to stay out of its
        # way.
        if state in SING_STATES and prev in SING_STATES:
            return

        self._play_for_state(state)

    def _next_dance_clip(self) -> str:
        """Shuffle-bag picker: refills with a fresh shuffled copy of all 7
        DANCE_CLIPS whenever the bag runs dry, so every clip gets played
        exactly once per lap before any of them repeats — instead of plain
        random.choice(), which can (and does, over enough dances) replay the
        same 2-3 clips back to back purely by chance. Also makes sure the
        first clip of a fresh bag isn't the same as the last clip played, so
        there's no repeat right at the seam between one lap and the next."""
        if not self._dance_bag:
            self._dance_bag = DANCE_CLIPS.copy()
            random.shuffle(self._dance_bag)
            if len(self._dance_bag) > 1 and self._dance_bag[0] == self._last_dance:
                # swap the seam clip with another so we don't repeat back-to-back
                swap_with = random.randrange(1, len(self._dance_bag))
                self._dance_bag[0], self._dance_bag[swap_with] = (
                    self._dance_bag[swap_with], self._dance_bag[0]
                )
        pick = self._dance_bag.pop(0)
        self._last_dance = pick
        return pick

    def _play_for_state(self, state: str):
        if state in SLEEP_STATES:
            self._call_js("playAnimationLoop", "sitting_idle")
        elif state == "LISTENING":
            self._call_js("playAnimationLoop", "wait")      # attentive pose
        elif state == "THINKING":
            self._call_js("playAnimationLoop", "thinking")
        elif state == "TYPING":
            # Only reached once already sitting (via the lead-in above, or
            # a repeat set_state("TYPING") call while still typing) — loop
            # the typing clip itself.
            self._call_js("playAnimationLoop", "typing")
        elif state == "POSE":
            self._call_js("playAnimation", random.choice(POSE_CLIPS))
        elif state in HAPPY_STATES:
            self._call_js("playAnimation", "happy")
        elif state in SING_STATES:
            self._call_js("playAnimation", self._next_dance_clip())
        else:
            # IDLE, SPEAKING, VOLUME, SCREENSHOT, SHY, CONCERNED, FOCUSED,
            # RELAXED, CURIOUS — no dedicated clip for these; she just
            # breathes/blinks on the idle loop, with lip-sync layered on
            # top whenever setTalking(True) is active.
            self._call_js("playAnimationLoop", "idle")

    def _on_animation_finished(self, name: str):
        # Greetings settle into whatever the real target state was;
        # sit_to_type always settles into the looping typing clip.
        if name in GREETING_CLIPS and self._pending_settle_state:
            settle = self._pending_settle_state
            self._pending_settle_state = None
            self._play_for_state(settle)
        elif name in LEAD_IN_LOOP:
            self._call_js("playAnimationLoop", LEAD_IN_LOOP[name])
            if self._pending_settle_state == "TYPING":
                self._pending_settle_state = None
        elif name in DANCE_CLIPS and self.state in SING_STATES:
            # "Never dance in silence": self.state only stays SINGING for
            # as long as main.py's music_playing watchdog keeps confirming
            # the song is still actually playing — the moment it isn't,
            # main.py calls set_state() to something else and this stops
            # chaining on its own, no separate music-aware check needed
            # here. While it IS still SINGING, keep going instead of
            # falling back to idle after just one clip. Uses the shuffle-bag
            # picker so all 7 clips cycle through before any repeat.
            self._call_js("playAnimation", self._next_dance_clip())
        # Every other one-shot clip (happy, a pose pick) already
        # auto-returns to idle inside app.js on its own, which is correct.

    @property
    def dynamic_pulse(self):
        return self._dynamic_pulse

    @dynamic_pulse.setter
    def dynamic_pulse(self, value):
        # main.py already sets this from the live TTS volume level
        # ("ui.avatar.dynamic_pulse = int(vol * 0.8)") — that exact signal
        # now drives the VRM mouth-open amount directly, so speaking-state
        # lip movement needs no new call site anywhere else in the app.
        self._dynamic_pulse = value
        level = max(0, min(100, int(value)))
        self._call_js("setMouthLevel", level, drop_if_not_ready=True)

    # .update() is inherited from QWidget as-is — no paintEvent override
    # is needed anymore since QWebEngineView draws itself.

    # ── page lifecycle / JS bridge ──────────────────────────────────────
    def _on_load_finished(self, ok: bool):
        if not ok:
            print("[AvatarWidget] Failed to load the VRM page.")
            return
        self._poll_ready(elapsed_ms=0)

    def _poll_ready(self, elapsed_ms: int):
        def handle(ready):
            if ready:
                self._controller_ready = True
                self._flush_queue()
                return
            if elapsed_ms >= 20_000:
                print("[AvatarWidget] Timed out waiting for the VRM avatar controller.")
                return
            QTimer.singleShot(100, lambda: self._poll_ready(elapsed_ms + 100))
        self.view.page().runJavaScript("window.__controllerReady === true", handle)

    def _call_js(self, fn_name: str, *args, drop_if_not_ready: bool = False):
        if self._controller_ready:
            self._run(fn_name, args)
        elif drop_if_not_ready:
            return
        else:
            self._js_queue.append((fn_name, args))

    def _flush_queue(self):
        queue, self._js_queue = self._js_queue, []
        for fn_name, args in queue:
            self._run(fn_name, args)

    def _run(self, fn_name: str, args: tuple):
        arg_str = ", ".join(json.dumps(a) for a in args)
        self.view.page().runJavaScript(f"window.{fn_name} && window.{fn_name}({arg_str});")


# ═══════════════════════════════════════════════════════════════════════════════
class WaveOverlay(QWidget):
    """
    Just the animated waveform strip, centered near the bottom of the card,
    sitting on top of the GIF sanju. No text is drawn anywhere — the GIF
    and this waveform are the entire UI.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(CARD_W, CARD_H)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        self._rgb = (64, 140, 220)

        # ── Waveform animation ──────────────────────────────────────────────
        self._wave_active  = False
        self._wave_phase   = 0.0
        self._wave_heights = [0.3] * WAVE_BAR_COUNT
        self._wave_timer   = QTimer(self)
        self._wave_timer.setInterval(60)
        self._wave_timer.timeout.connect(self._step_wave)
        self._wave_timer.start()

    # ── Public ────────────────────────────────────────────────────────────────
    def set_color(self, rgb: tuple):
        self._rgb = rgb
        self.update()

    def set_wave_active(self, active: bool):
        self._wave_active = active

    # ── Waveform logic ──────────────────────────────────────────────────────
    def _step_wave(self):
        self._wave_phase += 0.35
        for i in range(WAVE_BAR_COUNT):
            base = math.sin(self._wave_phase + i * 0.5) * 0.5 + 0.5
            if self._wave_active:
                jitter = random.uniform(0.0, 0.35)
                self._wave_heights[i] = max(0.15, min(1.0, base * 0.8 + jitter))
            else:
                # gentle idle breathing instead of a flat line
                self._wave_heights[i] = 0.12 + base * 0.10
        self.update()

    # ── Paint ─────────────────────────────────────────────────────────────────
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        r, g, b = self._rgb

        # Waveform bars — centered horizontally, sitting near the bottom
        # of the card, on top of the GIF.
        bar_w     = 3
        bar_gap   = 5
        max_bar_h = WAVE_AREA_H
        total_w   = WAVE_BAR_COUNT * (bar_w + bar_gap) - bar_gap
        wave_x    = (CARD_W - total_w) // 2
        wave_y    = CARD_H - max_bar_h - 18

        color = QColor(r, g, b, 220)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        for i, h_frac in enumerate(self._wave_heights):
            bar_h = max(2, int(max_bar_h * h_frac))
            x = wave_x + i * (bar_w + bar_gap)
            y = wave_y + (max_bar_h - bar_h) // 2
            path = QPainterPath()
            path.addRoundedRect(float(x), float(y), float(bar_w), float(bar_h), 1.5, 1.5)
            painter.drawPath(path)


# ═══════════════════════════════════════════════════════════════════════════════
class CardBackground(QWidget):
    """
    The dark rounded "card" that sits BEHIND the sanju and text panel,
    unifying them into one visual block — like the reference image's
    dark navy card with the palm-tree photo bleeding into it.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(CARD_W, CARD_H)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._rgb = (0, 170, 255)

    def set_color(self, rgb: tuple):
        self._rgb = rgb
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        rect = QRectF(0, 0, CARD_W, CARD_H)
        path = QPainterPath()
        path.addRoundedRect(rect, CARD_RADIUS, CARD_RADIUS)

        # Base dark navy fill — kept LOW alpha so the desktop shows through.
        # Raise CARD_FILL_ALPHA (0-255) if you want it more opaque/solid.
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(8, 10, 20, CARD_FILL_ALPHA))
        painter.drawPath(path)

        # Diagonal gradient wash, tinted with the current state colour —
        # kept subtle for a cleaner, more corporate feel (was more of an
        # ambient photo-lighting effect before).
        r, g, b = self._rgb
        grad = QLinearGradient(0, 0, CARD_W, CARD_H)
        grad.setColorAt(0.0, QColor(9, 12, 22, CARD_FILL_ALPHA))
        grad.setColorAt(0.6, QColor(11, 14, 26, int(CARD_FILL_ALPHA * 0.9)))
        grad.setColorAt(1.0, QColor(r, g, b, int(CARD_FILL_ALPHA * 0.18)))
        painter.setBrush(QBrush(grad))
        painter.drawPath(path)

        # Thin, low-contrast border — reads as a crisp edge, not a glow.
        painter.setPen(QPen(QColor(255, 255, 255, 24), 1.0))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(path)

        # Slim accent line along the bottom edge, toned down from a bright
        # neon strip to a quieter indicator of current state.
        accent = QColor(r, g, b, 120)
        painter.setPen(QPen(accent, 1.5))
        painter.drawLine(int(CARD_RADIUS * 1.2), CARD_H - 2,
                         CARD_W - int(CARD_RADIUS * 1.2), CARD_H - 2)


# ═══════════════════════════════════════════════════════════════════════════════
class CloseButton(QWidget):
    """
    Small circular X button — the only way to close this frameless overlay
    window, since it has no title bar. Sits in the top-right corner.
    """

    SIZE = 22

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(self.SIZE, self.SIZE)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._hover = False
        self._on_click = None  # set externally by SanjuUI

    def set_on_click(self, callback):
        self._on_click = callback

    def enterEvent(self, event):
        self._hover = True
        self.update()

    def leaveEvent(self, event):
        self._hover = False
        self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._on_click:
            self._on_click()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        bg_alpha = 90 if self._hover else 45
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(255, 70, 70, bg_alpha) if self._hover
                         else QColor(255, 255, 255, bg_alpha))
        painter.drawEllipse(0, 0, self.SIZE, self.SIZE)

        pen = QPen(QColor(255, 255, 255, 230 if self._hover else 170), 1.6)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        m = 6
        painter.drawLine(m, m, self.SIZE - m, self.SIZE - m)
        painter.drawLine(self.SIZE - m, m, m, self.SIZE - m)


# ═══════════════════════════════════════════════════════════════════════════════
class SanjuUI(QWidget):

    def __init__(self):
        super().__init__()
        self._init_ui()

    def _init_ui(self):
        self.setWindowTitle("Sanju")
        self.setFixedSize(WIN_W, WIN_H)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint  |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        screen = QApplication.primaryScreen().availableGeometry()
        self.move(screen.width()  - WIN_W - 20,
                  screen.height() - WIN_H - 20)

        # NOTE: the dark rounded-rect "card" that used to sit behind the
        # avatar (CardBackground) has been removed — the window itself is
        # already fully transparent, so with no card there's nothing but
        # her rendered on the desktop. CardBackground's class definition
        # is left in this file, unused, in case you want a background
        # again later.

        # Avatar / VRM — fills the entire card
        self.avatar = AvatarWidget(self)
        self.avatar.move(0, 0)
        self._avatar_base_pos = QPoint(0, 0)

        # NOTE: the green waveform/equalizer strip that used to sit here
        # (WaveOverlay) has been removed — speech is now indicated purely
        # by the avatar's own lip-sync + subtle body motion, not a bars/
        # pulse overlay. WaveOverlay's class definition is left in this
        # file, unused, in case you want a non-green visual indicator here
        # later; it's just no longer instantiated.

        # Close button (top-right corner) — the only way to close this
        # frameless window since it has no title bar.
        self.close_btn = CloseButton(self)
        self.close_btn.move(CARD_W - CloseButton.SIZE - 10, 10)
        self.close_btn.raise_()
        self.close_btn.set_on_click(self._handle_close_click)

        # Bounce animation (sanju now fills the whole card, starts at 0,0)
        base_y = 0
        self.bounce = QPropertyAnimation(self.avatar, b"pos")
        self.bounce.setDuration(440)
        self.bounce.setLoopCount(-1)
        self.bounce.setStartValue(QPoint(0, base_y))
        self.bounce.setKeyValueAt(0.5, QPoint(0, base_y - 12))
        self.bounce.setEndValue(QPoint(0, base_y))
        self.bounce.setEasingCurve(QEasingCurve.Type.InOutSine)

        # Window fade
        self.setWindowOpacity(0.0)
        self._win_fade = QPropertyAnimation(self, b"windowOpacity")
        self._win_fade.setDuration(500)

        # Sleep timer
        self._sleep_timer = QTimer(self)
        self._sleep_timer.setInterval(20_000)
        self._sleep_timer.timeout.connect(self.go_to_sleep)

        self.set_state("SLEEP")

    # ── Public ────────────────────────────────────────────────────────────────

    def set_state(self, state: str):
        self._sleep_timer.stop()
        self.avatar.set_state(state)

        if state == "SLEEP":
            self._win_fade_to(0.0)
        else:
            self._win_fade_to(1.0)

        if False:  # bounce disabled — GIF stays still, no up/down motion
            self.bounce.start()
        else:
            self.bounce.stop()
            self.avatar.move(self._avatar_base_pos)

        if state == "IDLE":
            self._sleep_timer.start()

    def update_text(self, text: str):
        """
        No-op by design — the UI is text-free now (GIF + waveform only).
        Kept as a method so main.py's existing ui.update_text(...) calls
        throughout the app don't need to be touched or removed.
        """
        pass

    def stream_response(self, prefix: str, words: list, word_interval_ms: int = 370):
        """
        No-op by design — no caption/transcript is ever shown. The
        waveform + sanju animation are the only "speaking" indicator.
        Kept as a method so main.py's call site doesn't need to change.
        """
        pass

    def append_word(self, word: str):
        pass

    def go_to_sleep(self):
        self.set_state("SLEEP")

    # ── Private ───────────────────────────────────────────────────────────────

    def _handle_close_click(self):
        """Called when the user clicks the X button — closes the whole app."""
        from PyQt6.QtWidgets import QApplication as _QApp
        app = _QApp.instance()
        if app:
            app.quit()
        else:
            self.close()

    def _win_fade_to(self, opacity: float):
        self._win_fade.stop()
        self._win_fade.setStartValue(self.windowOpacity())
        self._win_fade.setEndValue(opacity)
        self._win_fade.start()