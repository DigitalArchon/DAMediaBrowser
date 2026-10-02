# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Video playing in the app: the picture in the main area, mpv drawing it.

The transport bar along the bottom drives it as it drives mpv's own window
- fullscreen and popping out included - so this page is only the picture
and a way back to what was showing before. Leaving it doesn't stop
anything: the video plays on out of sight, and Show Video on the transport
bar brings it back.

Keys, while the page has focus: Space plays and pauses, ←/→ jump ten
seconds, F (or F11) is fullscreen, Esc leaves fullscreen. And some of mpv's
own, which it can't take here itself: S a screenshot, V subtitles on and
off, J the next subtitles, # the next audio track.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from mediabrowser.gui.video_surface import VideoSurface

JUMP_SECONDS = 10.0

# mpv's keys for these, and the property each steps on.
CYCLE_KEYS = {
    Qt.Key_V: "sub-visibility",
    Qt.Key_J: "sub",
    Qt.Key_NumberSign: "audio",
}


class VideoPage(QWidget):
    back_requested = Signal()
    fullscreen_requested = Signal()  # toggle
    play_pause_requested = Signal()
    jump_requested = Signal(float)  # seconds, relative
    leave_fullscreen_requested = Signal()
    screenshot_requested = Signal()
    cycle_requested = Signal(str)  # mpv's property: "sub-visibility", "sub", "audio"

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("videoPage")
        self.setFocusPolicy(Qt.StrongFocus)

        self.back_button = QPushButton("‹  Back")
        self.back_button.setObjectName("backButton")
        self.back_button.setToolTip("Back to what was showing - the video plays on")
        self.back_button.clicked.connect(self.back_requested)
        self.title = QLabel()
        self.title.setObjectName("detailTitle")
        self.title.setTextFormat(Qt.PlainText)
        self.title.setMinimumWidth(1)

        self.header = QWidget()
        top = QHBoxLayout(self.header)
        top.setContentsMargins(0, 0, 0, 0)
        top.addWidget(self.back_button)
        top.addWidget(self.title, 1)

        self.surface = VideoSurface()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addWidget(self.header)
        layout.addWidget(self.surface, 1)

    def window_id(self) -> int:
        return int(self.surface.winId())

    def set_title(self, text: str) -> None:
        self.title.setText(text)

    def set_fullscreen(self, on: bool) -> None:
        """Just the picture, edge to edge."""
        self.header.setVisible(not on)
        self.layout().setSpacing(0 if on else 8)

    def keyPressEvent(self, event) -> None:
        key = event.key()
        if key == Qt.Key_Space:
            self.play_pause_requested.emit()
        elif key in (Qt.Key_Left, Qt.Key_Right):
            self.jump_requested.emit(JUMP_SECONDS if key == Qt.Key_Right else -JUMP_SECONDS)
        elif key in (Qt.Key_F, Qt.Key_F11):
            self.fullscreen_requested.emit()
        elif key == Qt.Key_Escape:
            self.leave_fullscreen_requested.emit()
        elif key == Qt.Key_S:
            self.screenshot_requested.emit()
        elif key in CYCLE_KEYS:
            self.cycle_requested.emit(CYCLE_KEYS[key])
        else:
            super().keyPressEvent(event)
