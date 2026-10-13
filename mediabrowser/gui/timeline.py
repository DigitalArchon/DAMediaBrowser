# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""A video's length as a strip: its chapter boundaries, where playback is,
the chapter it's in, and - once measured - how loud it is along the way.

Clicking or dragging on the strip moves playback there. Pressing on a
boundary (other than the first, which is always the start) and dragging
moves the boundary instead: that's the quickest way to put right a chapter
that starts a few seconds out, played back to check. A boundary stops
short of the ones either side of it.

A whole concert in a strip this wide is several seconds to the pixel, too
coarse to place anything by, so the strip zooms: the wheel, about the
pointer, down to half a minute across. Zoomed in, it keeps the playhead in
sight, Shift and the wheel move along it, and a line underneath shows
which part of the video is on show.

The loudness is drawn behind the boundaries, the bass brighter over the
full band: the bass is what stops between songs, even under applause.
"""

from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import QSizePolicy, QToolTip, QWidget

from mediabrowser.core import utils

# How near (pixels) a press must be to a boundary to take hold of it.
GRAB_PIXELS = 6
PAD = 8
# The least of the video the strip zooms in to, and how far a step goes.
MIN_SPAN_SECONDS = 30.0
ZOOM_STEP = 1.5
# Where the playhead is put, across the strip, when the view moves to it.
REVEAL_AT = 0.1
# How far below a video's loudest moment still shows as anything.
LEVEL_RANGE_DB = 36.0

TRACK = QColor("#232733")
CURRENT = QColor("#2b3a5c")
BOUNDARY = QColor("#8a91a0")
SELECTED = QColor("#e0af68")
PLAYHEAD = QColor("#7aa2f7")
LABEL = QColor("#8a91a0")
LOUDNESS = QColor(138, 145, 160, 50)
BASS = QColor(138, 145, 160, 110)
OVERVIEW = QColor("#3a3f4b")


def _heights(levels: list[float]) -> list[float]:
    """Levels in dB as heights from 0 to 1, the loudest the full height."""
    if not levels:
        return []
    top = max(levels)
    return [min(1.0, max(0.0, 1.0 - (top - level) / LEVEL_RANGE_DB)) for level in levels]


class Timeline(QWidget):
    seek_requested = Signal(float)  # seconds
    boundary_moved = Signal(int, float)  # mark index, new start in seconds
    boundary_clicked = Signal(int)  # mark index
    view_changed = Signal()

    def __init__(self, min_gap: float = 0.0) -> None:
        super().__init__()
        self.setObjectName("timeline")
        self.setMinimumHeight(50)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setMouseTracking(True)
        # A click takes the keyboard, though the strip does nothing with
        # it: out of a name being typed beside it, so that the keys move
        # through the video again.
        self.setFocusPolicy(Qt.ClickFocus)
        # How close a dragged boundary may come to the ones either side.
        self.min_gap = min_gap
        self.duration = 0.0
        self.starts: list[float] = [0.0]
        self.titles: list[str | None] = [None]
        self.playhead: float | None = None
        self.selected: int | None = None
        self._dragging_mark: int | None = None
        self._drag_to: float | None = None
        self._seeking = False
        # The part on show: from _view_start, _view_span long (0: all of it).
        self._view_start = 0.0
        self._view_span = 0.0
        self._level_step = 0.0
        self._loudness: list[float] = []
        self._bass: list[float] = []
        self._level_shapes: tuple | None = None  # what they were drawn for, and them

    # --- state -------------------------------------------------------------

    def set_marks(self, starts, titles, duration: float) -> None:
        self.starts, self.titles = list(starts), list(titles)
        duration = max(0.0, float(duration))
        if duration != self.duration:
            self.duration = duration
            self.show_all()
        self.update()

    def set_playhead(self, seconds: float | None) -> None:
        """Where playback is. Zoomed in, the view goes with a playhead that
        runs off it - but not after one it was moved away from."""
        was_in_view = self.playhead is not None and self.in_view(self.playhead)
        self.playhead = seconds
        if seconds is not None and was_in_view and not self.in_view(seconds):
            self.reveal(seconds)
        self.update()

    def set_selected(self, index: int | None) -> None:
        self.selected = index
        self.update()

    def set_levels(self, levels) -> None:
        """The loudness to draw behind the boundaries (core.audio_levels'
        Levels), or None for none."""
        self._level_step = levels.step if levels is not None else 0.0
        self._loudness = _heights(levels.full) if levels is not None else []
        self._bass = _heights(levels.bass) if levels is not None else []
        self._level_shapes = None
        self.update()

    def has_levels(self) -> bool:
        return bool(self._loudness)

    # --- the part on show ----------------------------------------------------

    def view(self) -> tuple[float, float]:
        """Where the strip starts, and how much of the video it spans."""
        return self._view_start, self._view_span or self.duration

    def zoomed(self) -> bool:
        return 0.0 < self._view_span < self.duration

    def in_view(self, seconds: float) -> bool:
        start, span = self.view()
        return start <= seconds <= start + span

    def _set_view(self, start: float, span: float) -> None:
        span = min(max(span, min(MIN_SPAN_SECONDS, self.duration)), self.duration)
        start = min(max(0.0, start), self.duration - span)
        if span >= self.duration:
            start, span = 0.0, 0.0
        if (start, span) != (self._view_start, self._view_span):
            self._view_start, self._view_span = start, span
            self.view_changed.emit()
            self.update()

    def show_all(self) -> None:
        self._set_view(0.0, self.duration)

    def zoom(self, factor: float, about: float | None = None) -> None:
        """Show `factor` times less of the video (more, below 1), the moment
        `about` staying where it is on the strip: the playhead if it's on
        show, else the middle."""
        start, span = self.view()
        if span <= 0:
            return
        if about is None:
            in_sight = self.playhead is not None and self.in_view(self.playhead)
            about = self.playhead if in_sight else start + span / 2
        across = (about - start) / span
        new_span = min(max(span / factor, min(MIN_SPAN_SECONDS, self.duration)),
                       self.duration)
        self._set_view(about - across * new_span, new_span)

    def zoom_in(self) -> None:
        self.zoom(ZOOM_STEP)

    def zoom_out(self) -> None:
        self.zoom(1 / ZOOM_STEP)

    def pan(self, seconds: float) -> None:
        start, span = self.view()
        self._set_view(start + seconds, span)

    def reveal(self, seconds: float) -> None:
        """Bring `seconds` onto the strip, if it's off it."""
        if self.zoomed() and not self.in_view(seconds):
            span = self._view_span
            self._set_view(seconds - REVEAL_AT * span, span)

    # --- geometry ----------------------------------------------------------

    def _track(self) -> QRectF:
        return QRectF(PAD, 18, max(1, self.width() - 2 * PAD), self.height() - 28)

    def x_for(self, seconds: float) -> float:
        track = self._track()
        start, span = self.view()
        if span <= 0:
            return track.left()
        return track.left() + track.width() * (seconds - start) / span

    def seconds_at(self, x: float) -> float:
        track = self._track()
        start, span = self.view()
        fraction = (x - track.left()) / track.width()
        return start + min(max(fraction, 0.0), 1.0) * span

    def mark_at(self, x: float) -> int | None:
        """The boundary under x, if one is near enough to grab. Never the
        first: the video always starts at 0."""
        best, distance = None, GRAB_PIXELS + 1
        for i, start in enumerate(self.starts[1:], start=1):
            if not self.in_view(start):
                continue
            gap = abs(self.x_for(start) - x)
            if gap < distance:
                best, distance = i, gap
        return best

    def room(self, index: int) -> tuple[float, float]:
        """The earliest and latest boundary `index` can be dragged to."""
        after = self.starts[index + 1] if index + 1 < len(self.starts) else self.duration
        return self.starts[index - 1] + self.min_gap, after - self.min_gap

    def current_index(self) -> int | None:
        if self.playhead is None:
            return None
        return self._index_at(self.playhead)

    def _index_at(self, seconds: float) -> int:
        index = 0
        for i, start in enumerate(self.starts):
            if start <= seconds:
                index = i
        return index

    # --- painting ------------------------------------------------------------

    def _levels_drawn(self, track: QRectF) -> list[QPolygonF]:
        """The loudness and the bass as shapes standing on the bottom of
        the track, a point to a pixel: the average of what's under each,
        not the peak - with the whole video on show a gap between songs
        is a pixel or two wide, and the songs either side would hide it."""
        key = (track.left(), track.top(), track.width(), track.height(), self.view())
        if self._level_shapes is not None and self._level_shapes[0] == key:
            return self._level_shapes[1]
        start, span = self.view()
        columns = max(1, int(track.width()))
        per_column = span / columns
        shapes = []
        for heights in (self._loudness, self._bass):
            points = [QPointF(track.left(), track.bottom())]
            for column in range(columns):
                first = int((start + column * per_column) / self._level_step)
                last = max(first + 1,
                           math.ceil((start + (column + 1) * per_column) / self._level_step))
                under = heights[first:last]
                height = sum(under) / len(under) if under else 0.0
                points.append(QPointF(track.left() + column,
                                      track.bottom() - height * track.height()))
            points.append(QPointF(track.left() + columns, track.bottom()))
            shapes.append(QPolygonF(points))
        self._level_shapes = (key, shapes)
        return shapes

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        track = self._track()
        painter.setPen(Qt.NoPen)
        painter.setBrush(TRACK)
        painter.drawRoundedRect(track, 4, 4)
        start, span = self.view()

        current = self.current_index()
        if current is not None and span > 0:
            left = self.x_for(self.starts[current])
            right = self.x_for(self.starts[current + 1] if current + 1 < len(self.starts)
                               else self.duration)
            painter.setBrush(CURRENT)
            painter.drawRect(QRectF(left, track.top(), max(1.0, right - left),
                                    track.height()).intersected(track))

        if self.has_levels() and span > 0:
            painter.setRenderHint(QPainter.Antialiasing, False)
            for colour, shape in zip((LOUDNESS, BASS), self._levels_drawn(track),
                                     strict=True):
                painter.setBrush(colour)
                painter.drawPolygon(shape)
            painter.setRenderHint(QPainter.Antialiasing)

        font = QFont(self.font())
        font.setPointSizeF(max(7.0, font.pointSizeF() * 0.8))
        painter.setFont(font)
        metrics = painter.fontMetrics()
        labelled_to = -1e9  # where the last number drawn ends
        for i, mark in enumerate(self.starts):
            if i == self._dragging_mark and self._drag_to is not None:
                mark = self._drag_to
            if not self.in_view(mark):
                continue
            x = self.x_for(mark)
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

        if self.zoomed():
            # Which part of the video this is, and where playback is in all of it.
            y = self.height() - 4
            painter.setPen(QPen(OVERVIEW, 2))
            painter.drawLine(QPointF(track.left(), y), QPointF(track.right(), y))
            painter.setPen(QPen(BOUNDARY, 2))
            painter.drawLine(
                QPointF(track.left() + track.width() * start / self.duration, y),
                QPointF(track.left() + track.width() * (start + span) / self.duration, y),
            )
            if self.playhead is not None:
                x = track.left() + track.width() * self.playhead / self.duration
                painter.setPen(QPen(PLAYHEAD, 2))
                painter.drawLine(QPointF(x, y - 2), QPointF(x, y + 2))

        if self.playhead is not None and span > 0 and self.in_view(self.playhead):
            x = self.x_for(self.playhead)
            painter.setPen(QPen(PLAYHEAD, 2))
            painter.drawLine(QPointF(x, 2), QPointF(x, track.bottom() + 3))
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
            # No further than the boundaries either side: past one, it would
            # be that chapter's place it took.
            low, high = self.room(self._dragging_mark)
            if low <= high:
                self._drag_to = min(max(seconds, low), high)
            self.update()
            tip = (f"Chapter {self._dragging_mark + 1} starts at "
                   f"{utils.format_tenths(self._drag_to)}")
        else:
            if self._seeking:
                self.seek_requested.emit(seconds)
            index = self._index_at(seconds)
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

    def wheelEvent(self, event) -> None:
        """The wheel zooms about the pointer; with Shift, or sideways, it
        moves along the video."""
        delta = event.angleDelta()
        _start, span = self.view()
        if span <= 0:
            return
        if event.modifiers() & Qt.ShiftModifier or abs(delta.x()) > abs(delta.y()):
            steps = (delta.x() or delta.y()) / 120
            self.pan(-steps * span / 10)
        elif delta.y():
            self.zoom(ZOOM_STEP ** (delta.y() / 120),
                      about=self.seconds_at(event.position().x()))
        event.accept()
