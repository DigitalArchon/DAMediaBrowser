# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Cover images for the grid.

Real artwork arrives later; until then every video gets a generated cover so
the grid reads as a grid rather than as rows of grey squares. The generated
one is derived from the video's own name, so a given disc always looks the
same and the shelf stays recognisable at a glance.
"""

from __future__ import annotations

import hashlib
import os

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPixmap,
)

from mediabrowser.core import artwork

# Picked to sit against #1b1e25 without any of them reading as the accent
# blue, which means something else in this app.
_PALETTE = (
    ("#2b3a5c", "#1d2740"),
    ("#3a3145", "#262030"),
    ("#2f4a4a", "#1f3232"),
    ("#4a3a20", "#312616"),
    ("#3d2a32", "#2a1d23"),
    ("#26405a", "#1a2c3e"),
    ("#334228", "#232d1b"),
    ("#402c2c", "#2b1e1e"),
)


def _colours(seed: str) -> tuple[QColor, QColor]:
    digest = hashlib.sha1(seed.encode("utf-8")).digest()
    top, bottom = _PALETTE[digest[0] % len(_PALETTE)]
    return QColor(top), QColor(bottom)


def _initials(name: str) -> str:
    words = [w for w in name.replace("_", " ").replace(".", " ").split() if w[:1].isalnum()]
    if not words:
        return "?"
    if len(words) == 1:
        return words[0][:2].upper()
    return (words[0][:1] + words[1][:1]).upper()


# Covers already drawn, so filling the shelf again - ticking a library on or
# off, sorting, searching - doesn't paint every one afresh. A placeholder
# depends only on the name; a cover file on its path and when it was
# written. Emptied when it grows past this, which is plenty for one shelf.
_drawn: dict[tuple, QPixmap] = {}
_MOST_DRAWN = 6000


def _remember(key: tuple, pixmap: QPixmap) -> QPixmap:
    if len(_drawn) >= _MOST_DRAWN:
        _drawn.clear()
    _drawn[key] = pixmap
    return pixmap


def placeholder(name: str, size: int) -> QPixmap:
    """A cover for a video with no artwork: its initials on a tint chosen
    from its name.
    """
    drawn = _drawn.get(("placeholder", name, size))
    if drawn is not None:
        return drawn
    return _remember(("placeholder", name, size), _paint_placeholder(name, size))


def _paint_placeholder(name: str, size: int) -> QPixmap:
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setRenderHint(QPainter.TextAntialiasing)

    top, bottom = _colours(name)
    gradient = QLinearGradient(0, 0, 0, size)
    gradient.setColorAt(0.0, top)
    gradient.setColorAt(1.0, bottom)

    rect = QRectF(0.5, 0.5, size - 1, size - 1)
    painter.setBrush(QBrush(gradient))
    painter.setPen(QColor("#23262f"))
    painter.drawRoundedRect(rect, 6, 6)

    font = QFont(painter.font())
    font.setPixelSize(max(14, size // 3))
    font.setWeight(QFont.DemiBold)
    painter.setFont(font)
    painter.setPen(QColor(255, 255, 255, 140))
    painter.drawText(rect, Qt.AlignCenter, _initials(name))

    painter.end()
    return pixmap


def from_file(path, size: int) -> QPixmap | None:
    """A cached cover, scaled to fill a square and rounded to match the
    placeholders it sits alongside.

    Covers are every shape - a square sleeve, a 16:9 frame - so they are
    cropped to the centre rather than letterboxed, which would leave the
    grid full of bars.
    """
    source = QPixmap(str(path))
    if source.isNull():
        return None

    scaled = source.scaled(
        size, size, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation
    )
    x = max(0, (scaled.width() - size) // 2)
    y = max(0, (scaled.height() - size) // 2)
    cropped = scaled.copy(x, y, size, size)

    rounded = QPixmap(size, size)
    rounded.fill(Qt.transparent)
    painter = QPainter(rounded)
    painter.setRenderHint(QPainter.Antialiasing)
    clip = QPainterPath()
    clip.addRoundedRect(QRectF(0, 0, size, size), 6, 6)
    painter.setClipPath(clip)
    painter.drawPixmap(0, 0, cropped)
    painter.setClipping(False)
    painter.setPen(QColor("#23262f"))
    painter.setBrush(Qt.NoBrush)
    painter.drawRoundedRect(QRectF(0.5, 0.5, size - 1, size - 1), 6, 6)
    painter.end()
    return rounded


def for_video(video_id: str, name: str, size: int) -> QPixmap:
    """The best cover available right now: the cached artwork if it has been
    found, otherwise the generated stand-in.
    """
    path = artwork.lookup(video_id)
    if path is not None:
        try:
            stat = os.stat(path)
        except OSError:
            return placeholder(name, size)
        key = ("file", str(path), size, stat.st_mtime_ns, stat.st_size)
        drawn = _drawn.get(key)
        if drawn is not None:
            return drawn
        pixmap = from_file(path, size)
        if pixmap is not None:
            return _remember(key, pixmap)
    return placeholder(name, size)
