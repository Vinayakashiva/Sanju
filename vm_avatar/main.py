"""
main.py — Entry point for the Sanju desktop AI assistant.

Run with:
    python main.py

See README.md for full setup instructions (dependencies, where to put the
VRM model and FBX animation files, Windows-specific notes).
"""
import torch
import os

# Only opens a Chrome DevTools debugging port when explicitly requested —
# left off by default so it can never collide with an existing Chrome
# instance's own remote-debugging port (a real failure mode this project
# is required to avoid). Enable with:
#   cmd:        set SANJU_DEBUG=1 && python main.py
#   PowerShell: $env:SANJU_DEBUG=1; python main.py
# Then inspect the avatar page at http://localhost:9222 in a normal browser.
if os.environ.get("SANJU_DEBUG") == "1":
    os.environ.setdefault("QTWEBENGINE_REMOTE_DEBUGGING", "9222")
    print("[main] Debug mode enabled: inspect the avatar page at http://localhost:9222")

import sys
import traceback

from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import QTimer

from vrm_bridge import VRMDesktopOverlay
from assistant import SanjuAssistant


def _install_global_exception_hook():
    """Print a full traceback instead of letting an unhandled exception in
    a Qt callback silently kill the app with no explanation."""
    def hook(exc_type, exc_value, exc_tb):
        print("[main] Unhandled exception:")
        traceback.print_exception(exc_type, exc_value, exc_tb)
    sys.excepthook = hook


def main():
    _install_global_exception_hook()

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(True)

    try:
        overlay = VRMDesktopOverlay()
    except Exception as e:
        print(f"[main] Failed to create the avatar overlay window: {e}")
        traceback.print_exc()
        sys.exit(1)

    overlay.show()

    assistant = SanjuAssistant(overlay)
    app.aboutToQuit.connect(assistant.shutdown)

    QTimer.singleShot(3000, assistant.greet)

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
