# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""A video's length as a strip: its chapter boundaries, where playback is,
and the chapter it's in.

Clicking or dragging on the strip moves playback there. Pressing on a
boundary (other than the first, which is always the start) and dragging
moves the boundary instead: that's the quickest way to put right a chapter
that starts a few seconds out, played back to check.
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QSizePolicy, QToolTip, QWidget

from mediabrowser.core import utils

# How near (pixels) a press must be to a boundary to take hold of it.
GRAB_PIXELS = 6
PAD = 8

TRACK = QColor("#232733")
CURRENT = QColor("#2b3a5c")
BOUNDARY = QColor("#8a91a0")
SELECTED = QColor("#e0af68")
PLAYHEAD = QColor("#7aa2f7")
LABEL = QColor("#8a91a0")


class Timeline(QWidget):
    seek_requested = Signal(float)  # seconds
    boundary_moved = Signal(int, float)  # mark index, new start in seconds
    boundary_clicked = Signal(int)  # mark index

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("timeline")
        self.setMinimumHeight(46)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setMouseTracking(True)
        self.duration = 0.0
        self.starts: list[float] = [0.0]
        self.titles: list[str | None] = [None]
        self.playhead: float | None = None
        self.selected: int | None = None
        self._dragging_mark: int | None = None
        self._drag_to: float | None = None
        self._seeking = False

    # --- state -------------------------------------------------------------

    def set_marks(self, starts, titles, duration: float) -> None:
        self.starts, self.titles = list(starts), list(titles)
        self.duration = max(0.0, float(duration))
        self.update()

    def set_playhead(self, seconds: float | None) -> None:
        self.playhead = seconds
        self.update()

    def set_selected(self, index: int | None) -> None:
        self.selected = index
        self.update()

    # --- geometry ----------------------------------------------------------

    def _track(self) -> QRectF:
        return QRectF(PAD, 18, max(1, self.width() - 2 * PAD), self.height() - 24)

    def x_for(self, seconds: float) -> float:
        track = self._track()
        if self.duration <= 0:
            return track.left()
        return track.left() + track.width() * min(max(seconds, 0.0), self.duration) / self.duration

    def seconds_at(self, x: float) -> float:
        track = self._track()
        fraction = (x - track.left()) / track.width()
        return min(max(fraction, 0.0), 1.0) * self.duration

    def mark_at(self, x: float) -> int | None:
        """The boundary under x, if one is near enough to grab. Never the
        first: the video always starts at 0."""
        best, distance = None, GRAB_PIXELS + 1
        for i, start in enumerate(self.starts[1:], start=1):
            gap = abs(self.x_for(start) - x)
            if gap < distance:
                best, distance = i, gap
        return best

    def current_index(self) -> int | None:
        if self.playhead is None:
            return None
        index = 0
        for i, start in enumerate(self.starts):
            if start <= self.playhead:
                index = i
        return index

    # --- painting ------------------------------------------------------------

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        track = self._track()
        painter.setPen(Qt.NoPen)
        painter.setBrush(TRACK)
        painter.drawRoundedRect(track, 4, 4)

        current = self.current_index()
        if current is not None and self.duration > 0:
            left = self.x_for(self.starts[current])
            right = self.x_for(self.starts[current + 1] if current + 1 < len(self.starts)
                               else self.duration)
            painter.setBrush(CURRENT)
            painter.drawRect(QRectF(left, track.top(), max(1.0, right - left), track.height()))

        font = QFont(self.font())
        font.setPointSizeF(max(7.0, font.pointSizeF() * 0.8))
        painter.setFont(font)
        metrics = painter.fontMetrics()
        labelled_to = -1e9  # where the last number drawn ends
        for i, start in enumerate(self.starts):
            if i == self._dragging_mark and self._drag_to is not None:
                start = self._drag_to
            x = self.x_for(start)
            colour = SELECTED if i == self.selected else BOUNDARY
            painter.setPen(QPen(colour, 2 if i == self.selected else 1))
            painter.drawLine(QPointF(x, track.top() - 3), QPointF(x, track.bottom()))
            # A number only where there's room for it: close chapters would
            # run together. Hovering names every one.
            label = str(i + 1)
            if x + 3 >= labelled_to + 4 or i == self.selected:
                painter.setPen(colour if i == self.selected else LABEL)
                painter.drawText(QPointF(x + 3, track.top() - 5), label)
                labelled_to = x + 3 + metrics.horizontalAdvance(label)

        if self.playhead is not None and self.duration > 0:
            x = self.x_for(self.playhead)
            painter.setPen(QPen(PLAYHEAD, 2))
            painter.drawLine(QPointF(x, 2), QPointF(x, self.height() - 2))
        painter.end()

    # --- mouse ---------------------------------------------------------------

    def mousePressEvent(self, event) -> None:
        if event.button() != Qt.LeftButton or self.duration <= 0:
            return
        x = event.position().x()
        grabbed = self.mark_at(x)
        if grabbed is not None:
            self._dragging_mark = grabbed
            self._drag_to = self.starts[grabbed]
            self.boundary_clicked.emit(grabbed)
        else:
            self._seeking = True
            self.seek_requested.emit(self.seconds_at(x))

    def mouseMoveEvent(self, event) -> None:
        x = event.position().x()
        seconds = self.seconds_at(x)
        if self._dragging_mark is not None:
            self._drag_to = seconds
            self.update()
        elif self._seeking:
            self.seek_requested.emit(seconds)
        index = 0
        for i, start in enumerate(self.starts):
            if start <= seconds:
                index = i
        title = self.titles[index] if index < len(self.titles) else None
        tip = f"{utils.format_seconds(seconds)} · {utils.title_or_number(index, title)}"
        if self.mark_at(x) is not None:
            tip += "\nDrag to move where this chapter starts"
        QToolTip.showText(event.globalPosition().toPoint(), tip, self)

    def mouseReleaseEvent(self, event) -> None:
        if self._dragging_mark is not None:
            index, to = self._dragging_mark, self._drag_to
            self._dragging_mark = self._drag_to = None
            if to is not None and abs(to - self.starts[index]) > 0.05:
                self.boundary_moved.emit(index, to)
            self.update()
        self._seeking = False
