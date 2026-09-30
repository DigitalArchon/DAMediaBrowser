# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""The native window mpv draws into, for Manual Edit and for playing video
in the app.

mpv is handed the window's X11 id (--wid) and draws the picture into it
itself, so the app's own controls sit around a real mpv rather than a
re-implementation of one. That only works where the app has X11 windows:
on X11, or XWayland (app.prefer_x11 arranges that on a Wayland desktop).
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QGuiApplication
from PySide6.QtWidgets import QSizePolicy, QWidget

ELSEWHERE = "The video plays in its own window on this system; everything here still drives it."
ELSEWHERE_WAYLAND = (
    "The video plays in its own window: the app is running on Wayland itself, "
    "where it can't hold another program's picture. Everything here still drives it. "
    "Start the app with QT_QPA_PLATFORM=xcb to have the video here."
)


def elsewhere_text() -> str:
    """Why the video isn't here, and what would bring it."""
    return ELSEWHERE_WAYLAND if QGuiApplication.platformName() == "wayland" else ELSEWHERE


def can_embed() -> bool:
    """Whether mpv can draw into a window of ours: on X11 (or XWayland)."""
    return QGuiApplication.platformName() == "xcb"


class VideoSurface(QWidget):
    """A native window mpv draws the video into."""

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("videoSurface")
        # Only this widget is native. Left to itself Qt makes its ancestors
        # and their siblings native too - the page stack, the detail view -
        # and a menu or dialog opened from those then gets a child window as
        # its parent ("... must be a top level window").
        self.setAttribute(Qt.WA_DontCreateNativeAncestors)
        self.setAttribute(Qt.WA_NativeWindow)
        self.setAttribute(Qt.WA_OpaquePaintEvent)
        self.setMinimumSize(320, 180)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setAutoFillBackground(True)
        palette = self.palette()
        palette.setColor(self.backgroundRole(), QColor("#000000"))
        self.setPalette(palette)
