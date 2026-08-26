"""
vrm_bridge.py — Desktop overlay window + Python<->JS bridge for the Sanju
VRM avatar.

Responsibilities:
  * Host the VRM avatar in a frameless, transparent, always-on-top window
    positioned in the bottom-right corner of the screen — a "desktop
    companion" window. QWebEngineView renders the page, but no browser UI
    (address bar, tabs, etc.) is ever shown, and no browser tab exists.
  * Make the transparent parts of that window click-through on Windows, so
    clicks pass to whatever's behind the avatar except where she's
    actually drawn.
  * Bridge Python -> JS calls (play_animation, set_talking, etc.) via
    runJavaScript, queued until the page reports itself ready.
  * Bridge JS -> Python calls (animation-finished acknowledgement) via
    QWebChannel — an in-process Qt transport, NOT a websocket or a
    localhost server, so "websocket errors / localhost connection
    failures / remote-debugging port conflicts" don't apply to it.
"""

import ctypes
import json
import os
import sys

from PyQt6.QtCore import (
    Qt, QUrl, QTimer, QObject, pyqtSlot, QRect,
    QPropertyAnimation, QEasingCurve, QPoint,
)
from PyQt6.QtGui import QCursor
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QApplication
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWebEngineCore import QWebEngineSettings
from PyQt6.QtWebChannel import QWebChannel

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INDEX_HTML_PATH = os.path.join(BASE_DIR, "web", "index.html")

_READY_POLL_INTERVAL_MS = 100
_READY_POLL_TIMEOUT_MS = 20_000

# Small vertical strip, pinned to the bottom-right of the screen.
WINDOW_WIDTH = 340
WINDOW_HEIGHT = 560
MARGIN_X = 24
MARGIN_Y = 56

IS_WINDOWS = sys.platform == "win32"


# ══════════════════════════════════════════════════════════════════════════
# Windows click-through support
# ══════════════════════════════════════════════════════════════════════════
# Only meaningful on Windows (this project's target platform per the
# requirements). Toggles the WS_EX_TRANSPARENT extended window style on or
# off depending on whether the pixel under the cursor is opaque (part of
# the avatar) or transparent (background) — the standard technique for
# desktop-mascot-style overlays. No-ops on other platforms.
if IS_WINDOWS:
    _GWL_EXSTYLE = -20
    _WS_EX_LAYERED = 0x00080000
    _WS_EX_TRANSPARENT = 0x00000020
    _user32 = ctypes.windll.user32
    _GetWindowLong = getattr(_user32, "GetWindowLongPtrW", _user32.GetWindowLongW)
    _SetWindowLong = getattr(_user32, "SetWindowLongPtrW", _user32.SetWindowLongW)


class AssistantBridge(QObject):
    """Exposed to the page as `window.pyBridge` via QWebChannel.

    This is how the frontend acknowledges that a one-shot animation
    finished (per the animation state machine in web/app.js): the page
    calls `pyBridge.animationFinished("clap")` once the mixer's own
    'finished' event fires, instead of Python guessing a clip's duration.
    """

    def __init__(self, overlay: "VRMDesktopOverlay"):
        super().__init__()
        self._overlay = overlay

    @pyqtSlot(str)
    def animationFinished(self, name: str):
        print(f"[vrm_bridge] Animation finished, back to idle: {name}")

    @pyqtSlot(str)
    def logFromPage(self, message: str):
        # Optional: JS can call `pyBridge.logFromPage("...")` for anything
        # worth surfacing in the Python console during debugging.
        print(f"[web] {message}")


class AvatarProxy:
    """Python-side stand-in for the JS `avatar` object. Every method here
    mirrors one of the `window.*` functions exposed in web/app.js. Calls
    made before the page (or, for play_animation, before that specific
    clip) is ready are queued and replayed automatically once it is —
    callers in assistant.py don't need to know or care about page-load
    timing.
    """

    def __init__(self, bridge: "VRMDesktopOverlay"):
        self._bridge = bridge

    def play_animation(self, name: str):
        self._bridge._call_js("playAnimation", name, requires="anims")

    def play_animation_timed(self, name: str, duration_seconds: float):
        # Plays a clip on loop for a fixed duration (rather than the usual
        # one-shot-then-idle) — used for "walking" while the desktop window
        # itself is sliding to a new position, so the clip's own natural
        # length doesn't have to match the slide's.
        self._bridge._call_js("playAnimationTimed", name, duration_seconds, requires="anims")

    def walk_and_turn(self, direction: str, duration_seconds: float, clip_name: str = "walk"):
        # Turns her to face the direction of travel (never a full 180 —
        # she's never shown from behind), loops the given walk clip
        # (walk / walk2 / lazy — assistant.py picks one at random) for the
        # duration of the slide, then turns back to face the camera and
        # settles into idle. Widens the camera to a fuller-body framing for
        # the duration so the walk cycle is actually visible, then restores
        # the default portrait view. See walkAndTurn in web/app.js and
        # slide_to_side below (called separately, in parallel, by
        # assistant.py's _handle_move so the window slide and this
        # animation stay in sync).
        self._bridge._call_js("walkAndTurn", direction, duration_seconds, clip_name, requires="anims")

    def set_talking(self, is_talking: bool):
        self._bridge._call_js("setTalking", is_talking, requires="controller")

    def set_mouth_level(self, volume0to100: int):
        # High-frequency (driven by the TTS volume signal) — drop rather
        # than queue if the page isn't ready, since there'd be no
        # corresponding audio yet either.
        self._bridge._call_js("setMouthLevel", volume0to100, requires="controller", drop_if_not_ready=True)

    def set_viseme(self, name: str, weight: float):
        self._bridge._call_js("setViseme", name, weight, requires="controller", drop_if_not_ready=True)


class VRMDesktopOverlay(QWidget):
    """Frameless, transparent, always-on-top window hosting the VRM
    avatar, pinned to the bottom-right of the screen."""

    def __init__(self):
        super().__init__()

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool  # keeps it out of the taskbar
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.resize(WINDOW_WIDTH, WINDOW_HEIGHT)
        self._position_bottom_right()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.view = QWebEngineView(self)
        self.view.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.view.setStyleSheet("background: transparent;")
        # Transparent page background — without this the WebEngine surface
        # renders an opaque (usually white) rect behind the canvas even
        # though the HTML/CSS itself is "transparent".
        self.view.page().setBackgroundColor(Qt.GlobalColor.transparent)
        layout.addWidget(self.view)

        self._configure_settings(self.view.settings())

        # QWebChannel: JS -> Python (animation-finished acknowledgement).
        self.channel = QWebChannel(self.view.page())
        self.bridge = AssistantBridge(self)
        self.channel.registerObject("pyBridge", self.bridge)
        self.view.page().setWebChannel(self.channel)

        self._js_queue = []          # [(fn_name, args, requires)]
        self._controller_ready = False
        self._anims_ready = False
        self._closed = False

        self.view.loadFinished.connect(self._on_load_finished)

        if not os.path.isfile(INDEX_HTML_PATH):
            raise FileNotFoundError(
                f"[vrm_bridge] web/index.html not found at {INDEX_HTML_PATH}. "
                "Check the project folder structure in README.md — "
                "index.html/app.js/style.css must be inside a 'web' folder "
                "next to vrm_bridge.py, with 'models' and 'animations' as "
                "siblings of 'web' (not inside it)."
            )
        self.view.load(QUrl.fromLocalFile(INDEX_HTML_PATH))

        self.avatar = AvatarProxy(self)

        self._click_through_enabled = False
        self._hwnd = None
        self._click_through_timer = None

        # Tracks which side of the screen she's currently pinned to, so
        # "move left"/"move right"/"change place" know where she's coming
        # from and can no-op if she's already there. _position_bottom_right
        # (called above, in __init__) starts her on the right.
        self.current_side = "right"
        self._slide_animation = None

    def showEvent(self, event):
        super().showEvent(event)
        if IS_WINDOWS and self._click_through_timer is None:
            self._hwnd = int(self.winId())
            self._click_through_timer = QTimer(self)
            self._click_through_timer.timeout.connect(self._update_click_through)
            self._click_through_timer.start(33)  # ~30Hz

    def _position_bottom_right(self):
        screen = QApplication.primaryScreen()
        if not screen:
            return
        geo = screen.availableGeometry()
        x = geo.right() - self.width() - MARGIN_X
        y = geo.bottom() - self.height() - MARGIN_Y
        self.move(max(geo.left(), x), max(geo.top(), y))

    def slide_to_side(self, side: str, duration_ms: int = 2200, on_finished=None):
        """Animate the overlay window sliding from wherever it is now to
        the left or right edge of the screen, at the same vertical
        position it's already at. Used for the "move left"/"move
        right"/"change place" voice commands (see assistant.py)."""
        screen = QApplication.primaryScreen()
        if not screen:
            if on_finished:
                on_finished()
            return

        geo = screen.availableGeometry()
        if side == "left":
            target_x = geo.left() + MARGIN_X
        else:
            target_x = geo.right() - self.width() - MARGIN_X
        target_y = self.y()

        if self._slide_animation is not None:
            self._slide_animation.stop()

        anim = QPropertyAnimation(self, b"pos")
        anim.setDuration(duration_ms)
        anim.setStartValue(self.pos())
        anim.setEndValue(QPoint(target_x, target_y))
        anim.setEasingCurve(QEasingCurve.Type.InOutQuad)

        def _done():
            self.current_side = side
            self._slide_animation = None
            print(f"[vrm_bridge] Finished sliding to the {side}.")
            if on_finished:
                on_finished()

        anim.finished.connect(_done)
        self._slide_animation = anim
        anim.start()

    def _configure_settings(self, settings):
        # Required for a file:// page to import three.js/@pixiv/three-vrm
        # from a CDN via the <script type="importmap"> in index.html —
        # without this, module resolution fails silently and the avatar
        # never renders at all.
        settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True)
        settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, True)
        settings.setAttribute(QWebEngineSettings.WebAttribute.JavascriptEnabled, True)
        settings.setAttribute(QWebEngineSettings.WebAttribute.WebGLEnabled, True)
        settings.setAttribute(QWebEngineSettings.WebAttribute.Accelerated2dCanvasEnabled, True)
        settings.setAttribute(QWebEngineSettings.WebAttribute.ErrorPageEnabled, False)

    # ── click-through (Windows only) ────────────────────────────────────
    def _update_click_through(self):
        if self._closed or self._hwnd is None:
            return
        try:
            cursor = self.mapFromGlobal(QCursor.pos())
            if not self.rect().contains(cursor):
                self._set_click_through(True)
                return
            pixmap = self.grab(QRect(cursor.x(), cursor.y(), 1, 1))
            if pixmap.isNull():
                return
            alpha = pixmap.toImage().pixelColor(0, 0).alpha()
            self._set_click_through(alpha < 16)
        except Exception as e:
            # A click-through hiccup should never crash the whole app.
            print(f"[vrm_bridge] click-through check failed: {e}")

    def _set_click_through(self, enabled: bool):
        if not IS_WINDOWS or enabled == self._click_through_enabled:
            return
        self._click_through_enabled = enabled
        style = _GetWindowLong(self._hwnd, _GWL_EXSTYLE)
        if enabled:
            style |= _WS_EX_TRANSPARENT | _WS_EX_LAYERED
        else:
            style = (style | _WS_EX_LAYERED) & ~_WS_EX_TRANSPARENT
        _SetWindowLong(self._hwnd, _GWL_EXSTYLE, style)

    # ── page lifecycle ──────────────────────────────────────────────────
    def _on_load_finished(self, ok: bool):
        if not ok:
            print("[vrm_bridge] web/index.html failed to load.")
            return
        print("[vrm_bridge] Page loaded, waiting for avatar controller readiness…")
        self._poll_ready(elapsed_ms=0)

    def _poll_ready(self, elapsed_ms: int):
        if self._closed:
            return

        def handle(flags):
            if not flags:
                self._retry_poll(elapsed_ms)
                return
            controller_ready, anims_ready, last_error = flags
            if last_error:
                print(f"[vrm_bridge] JS error reported: {last_error}")

            newly_ready = controller_ready and not self._controller_ready
            self._controller_ready = bool(controller_ready)
            self._anims_ready = bool(anims_ready)

            if newly_ready:
                print("[vrm_bridge] Avatar controller ready.")
            self._flush_queue()

            if self._controller_ready and self._anims_ready:
                return  # fully ready, stop polling

            self._retry_poll(elapsed_ms)

        self.view.page().runJavaScript(
            "[window.__controllerReady === true, window.__animsReady === true, window.__lastError]",
            handle,
        )

    def _retry_poll(self, elapsed_ms: int):
        if self._anims_ready or self._closed:
            return
        elapsed_ms += _READY_POLL_INTERVAL_MS
        if elapsed_ms >= _READY_POLL_TIMEOUT_MS:
            print(
                "[vrm_bridge] Timed out waiting for animations to finish loading; "
                "proceeding with whatever loaded so far."
            )
            self._controller_ready = True
            self._anims_ready = True
            self._flush_queue()
            return
        QTimer.singleShot(_READY_POLL_INTERVAL_MS, lambda: self._poll_ready(elapsed_ms))

    # ── JS call queue ───────────────────────────────────────────────────
    def _call_js(self, fn_name: str, *args, requires: str = "controller", drop_if_not_ready: bool = False):
        ready = self._controller_ready if requires == "controller" else (self._controller_ready and self._anims_ready)
        if ready:
            self._run(fn_name, args)
        elif drop_if_not_ready:
            return
        else:
            self._js_queue.append((fn_name, args, requires))

    def _flush_queue(self):
        remaining = []
        for fn_name, args, requires in self._js_queue:
            ready = self._controller_ready if requires == "controller" else (self._controller_ready and self._anims_ready)
            if ready:
                self._run(fn_name, args)
            else:
                remaining.append((fn_name, args, requires))
        self._js_queue = remaining

    def _run(self, fn_name: str, args: tuple):
        arg_str = ", ".join(json.dumps(a) for a in args)
        js = f"window.{fn_name} && window.{fn_name}({arg_str});"
        self.view.page().runJavaScript(js)

    def closeEvent(self, event):
        self._closed = True
        if self._click_through_timer:
            self._click_through_timer.stop()
        super().closeEvent(event)