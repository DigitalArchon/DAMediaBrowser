# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""The transport bar along the bottom.

Always present, so there is one fixed place to look for what is playing -
the old UI scattered six action buttons and four transport buttons across
two rows that changed meaning with the selection.

The seek slider is driven from outside: position_changed() moves it, and it
emits seek_requested only when a person drags it, never when it is being
updated to follow playback.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QProxyStyle,
    QPushButton,
    QSlider,
    QStyle,
    QVBoxLayout,
    QWidget,
)

from mediabrowser.core import utils
from mediabrowser.gui import covers

THUMB = 48


class NowPlayingBar(QWidget):
    play_pause_requested = Signal()
    next_requested = Signal()
    previous_requested = Signal()
    restart_requested = Signal()
    stop_requested = Signal()
    seek_requested = Signal(float)
    show_video_requested = Signal()
    fullscreen_requested = Signal()
    move_requested = Signal()  # video to the other place: out of the app, or into it

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("nowPlayingBar")
        self._duration = 0.0
        self._dragging = False
        self._active = False
        self._video_away = False  # video plays in the app, out of sight

        self.cover = QLabel()
        self.cover.setObjectName("artThumb")
        self.cover.setFixedSize(THUMB, THUMB)
        self.cover.setAlignment(Qt.AlignCenter)

        self.title = QLabel("Nothing playing")
        self.title.setObjectName("nowPlayingTitle")
        self.title.setTextFormat(Qt.PlainText)
        self.subtitle = QLabel("")
        self.subtitle.setObjectName("nowPlayingSubtitle")
        self.subtitle.setTextFormat(Qt.PlainText)

        labels = QVBoxLayout()
        labels.setContentsMargins(0, 0, 0, 0)
        labels.setSpacing(2)
        labels.addStretch(1)
        labels.addWidget(self.title)
        labels.addWidget(self.subtitle)
        labels.addStretch(1)

        self.previous_button = _transport("⏮", "Previous chapter", self.previous_requested)
        self.restart_button = _transport("↺", "Restart this chapter", self.restart_requested)
        self.play_button = _transport("⏸", "Play / pause", self.play_pause_requested)
        self.play_button.setObjectName("playButton")
        self.next_button = _transport("⏭", "Next chapter", self.next_requested)
        self.stop_button = _transport("⏹", "Stop", self.stop_requested)

        # Only while video plays in the app: back to its picture from
        # anywhere, fullscreen, and out into mpv's own window - or, while
        # it plays there, back in.
        self.show_video_button = QPushButton("Show Video")
        self.show_video_button.setToolTip("Back to the video playing in the app")
        self.show_video_button.clicked.connect(self.show_video_requested)
        self.fullscreen_button = QPushButton("Fullscreen")
        self.fullscreen_button.setToolTip("Fill the screen with the video (F11; Esc to leave)")
        self.fullscreen_button.clicked.connect(self.fullscreen_requested)
        self.move_button = QPushButton("Pop Out")
        self.move_button.clicked.connect(self.move_requested)
        self.show_video_button.hide()
        self.fullscreen_button.hide()
        self.move_button.hide()

        transport = QHBoxLayout()
        transport.setContentsMargins(0, 0, 0, 0)
        transport.setSpacing(6)
        transport.addWidget(self.show_video_button)
        transport.addWidget(self.fullscreen_button)
        transport.addWidget(self.move_button)
        for button in (
            self.previous_button, self.restart_button, self.play_button,
            self.next_button, self.stop_button,
        ):
            transport.addWidget(button)

        self.elapsed = QLabel("0:00")
        self.elapsed.setObjectName("nowPlayingTime")
        self.total = QLabel("0:00")
        self.total.setObjectName("nowPlayingTime")

        self.slider = QSlider(Qt.Horizontal)
        self.slider.setObjectName("seekSlider")
        # A click anywhere on the bar goes there, rather than a step towards
        # it; holding on and dragging still scrubs.
        # (No base style given: one given would be owned, and deleted, by it.)
        self._slider_style = _JumpToClick()
        self.slider.setStyle(self._slider_style)
        # Whole seconds: mpv is not precise enough for finer to mean anything,
        # and it keeps the slider's integer range honest.
        self.slider.setRange(0, 0)
        self.slider.sliderPressed.connect(self._on_press)
        self.slider.sliderReleased.connect(self._on_release)
        self.slider.sliderMoved.connect(self._on_moved)

        scrubber = QHBoxLayout()
        scrubber.setContentsMargins(0, 0, 0, 0)
        scrubber.setSpacing(8)
        scrubber.addWidget(self.elapsed)
        scrubber.addWidget(self.slider, 1)
        scrubber.addWidget(self.total)

        middle = QVBoxLayout()
        middle.setContentsMargins(0, 0, 0, 0)
        middle.setSpacing(6)
        middle.addLayout(labels)
        middle.addLayout(scrubber)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(14)
        layout.addWidget(self.cover)
        layout.addLayout(middle, 1)
        layout.addLayout(transport)

        # The cover and title take you back to the video too: where you'd
        # click on what's playing.
        for label in (self.cover, self.title, self.subtitle):
            label.installEventFilter(self)

        self.clear()

    def eventFilter(self, watched, event) -> bool:
        if (event.type() == QEvent.MouseButtonRelease and self._video_away
                and event.button() == Qt.LeftButton):
            self.show_video_requested.emit()
            return True
        return super().eventFilter(watched, event)

    # --- state -----------------------------------------------------------

    def show_playing(self, title: str, subtitle: str, video_id: str, cover_name: str) -> None:
        self._active = True
        self.title.setText(title)
        self.subtitle.setText(subtitle)
        self.cover.setPixmap(covers.for_video(video_id, cover_name, THUMB))
        self._set_enabled(True)

    def set_video_controls(self, in_app: bool, showing: bool, fullscreen: bool = False,
                           can_come_in: bool = False) -> None:
        """Show Video while video plays in the app out of sight; Fullscreen
        and Pop Out while it's in the app at all; Into App while it plays in
        mpv's own window and `can_come_in`."""
        self._video_away = in_app and not showing
        self.show_video_button.setVisible(self._video_away)
        self.fullscreen_button.setVisible(in_app)
        self.move_button.setVisible(in_app or can_come_in)
        self.move_button.setText("Pop Out" if in_app else "Into App")
        self.move_button.setToolTip(
            "Carry on in mpv's own window, from this moment - the controls here still drive it"
            if in_app else "Carry on here in the app, from this moment"
        )
        for label in (self.cover, self.title, self.subtitle):
            if self._video_away:
                label.setCursor(Qt.PointingHandCursor)
                label.setToolTip("Back to the video")
            else:
                label.unsetCursor()
                label.setToolTip("")
        self.fullscreen_button.setText("Leave Fullscreen" if fullscreen else "Fullscreen")

    def clear(self) -> None:
        self._active = False
        self.set_video_controls(False, False)
        self.title.setText("Nothing playing")
        self.subtitle.setText("")
        self.cover.clear()
        self.set_duration(0.0)
        self.position_changed(0.0)
        self._set_enabled(False)

    def set_paused(self, paused: bool) -> None:
        self.play_button.setText("▶" if paused else "⏸")

    def set_duration(self, seconds: float) -> None:
        self._duration = max(0.0, seconds or 0.0)
        self.slider.setRange(0, int(self._duration))
        self.total.setText(utils.format_seconds(self._duration))
        # Nothing to seek within until mpv reports a length.
        self.slider.setEnabled(self._active and self._duration > 0)

    def position_changed(self, seconds: float) -> None:
        """Follow playback. Ignored mid-drag so the handle doesn't fight the
        person holding it.
        """
        if self._dragging:
            return
        self.slider.setValue(int(max(0.0, seconds or 0.0)))
        self.elapsed.setText(utils.format_seconds(seconds or 0.0))

    def _set_enabled(self, enabled: bool) -> None:
        for button in (
            self.previous_button, self.restart_button, self.play_button,
            self.next_button, self.stop_button,
        ):
            button.setEnabled(enabled)
        self.slider.setEnabled(enabled and self._duration > 0)

    # --- seeking ---------------------------------------------------------

    def _on_press(self) -> None:
        self._dragging = True

    def _on_moved(self, value: int) -> None:
        self.elapsed.setText(utils.format_seconds(value))

    def _on_release(self) -> None:
        self._dragging = False
        self.seek_requested.emit(float(self.slider.value()))


class _JumpToClick(QProxyStyle):
    def styleHint(self, hint, option=None, widget=None, returnData=None):
        if hint == QStyle.SH_Slider_AbsoluteSetButtons:
            return Qt.LeftButton.value
        return super().styleHint(hint, option, widget, returnData)


def _transport(glyph: str, tooltip: str, signal) -> QPushButton:
    button = QPushButton(glyph)
    button.setObjectName("transportButton")
    button.setToolTip(tooltip)
    button.setFixedWidth(44)
    button.clicked.connect(signal)
    return button
