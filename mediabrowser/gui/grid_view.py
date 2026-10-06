# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""The library as a shelf of covers.

A QListWidget in icon mode rather than a hand-rolled flow layout: it wraps,
scrolls, selects and takes arrow keys for free, and one delegate's worth of
styling is cheaper than reimplementing all of that.

Titles are never shortened. Qt's icon mode would elide them to a fixed cell,
which for a disc called "BABYMETAL - LIVE AT INTUIT DOME [2026]" leaves an
unreadable stub, so the cells are measured against the longest title on the
shelf and every one is made that tall.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QAction, QColor, QFontMetrics, QPainter
from PySide6.QtWidgets import (
    QAbstractItemView,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QStyle,
    QStyledItemDelegate,
)

from mediabrowser.core import naming, utils
from mediabrowser.gui import covers, identified
from mediabrowser.gui.plain import tooltip

THUMB = 132
# Wider than the cover so titles have somewhere to go before wrapping.
CELL_WIDTH = THUMB + 56
# Room for the icon, the gap under it, and the cell's own padding.
CHROME_HEIGHT = THUMB + 26  # PAD * 2 + LABEL_GAP + a little slack

ROLE_VIDEO_ID = Qt.UserRole
ROLE_NAME = Qt.UserRole + 1
ROLE_STATE = Qt.UserRole + 2  # naming state: identified or not

# The dot on a cover saying whether its chapters are identified.
BADGE = 12

# Gap between the cover and the title beneath it.
LABEL_GAP = 8
PAD = 6


class CardDelegate(QStyledItemDelegate):
    """Draws a cell as one card: cover, title and selection ring together.

    Qt's icon mode, styled through QSS, puts the highlight around the text
    alone - so a selected tile looked like a label with a picture floating
    above it rather than like one object. Painting it here also means the
    title is drawn with word wrap and no eliding, whatever Qt would have
    done with a cell this shape.
    """

    def paint(self, painter: QPainter, option, index) -> None:
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)

        rect = option.rect.adjusted(2, 2, -2, -2)
        selected = bool(option.state & QStyle.State_Selected)
        hovered = bool(option.state & QStyle.State_MouseOver)

        if selected:
            fill, border = QColor("#191d27"), QColor("#7aa2f7")
        elif hovered:
            fill, border = QColor("#1b1e25"), QColor("#2f3441")
        else:
            fill = border = None

        if fill is not None:
            painter.setBrush(fill)
            painter.setPen(border)
            painter.drawRoundedRect(QRectF(rect).adjusted(0.5, 0.5, -0.5, -0.5), 8, 8)

        icon = index.data(Qt.DecorationRole)
        if icon is not None:
            pixmap = icon.pixmap(THUMB, THUMB)
            x = rect.x() + (rect.width() - pixmap.width()) // 2
            painter.drawPixmap(x, rect.y() + PAD, pixmap)
            state = index.data(ROLE_STATE)
            if state and state != naming.SINGLE:
                # In the cover's top right corner, ringed so it reads on
                # any cover.
                badge = QRectF(x + pixmap.width() - BADGE - 4, rect.y() + PAD + 4, BADGE, BADGE)
                painter.setPen(QColor("#14161a"))
                painter.setBrush(QColor(identified.colour(state)))
                painter.drawEllipse(badge)

        colour = index.data(Qt.ForegroundRole)
        if colour is not None:
            painter.setPen(colour.color())
        elif selected:
            painter.setPen(QColor("#e4e7ee"))
        elif hovered:
            painter.setPen(QColor("#d8dbe2"))
        else:
            painter.setPen(QColor("#b9bfcb"))

        text_rect = QRect(
            rect.x() + PAD,
            rect.y() + PAD + THUMB + LABEL_GAP,
            rect.width() - 2 * PAD,
            rect.height() - PAD - THUMB - LABEL_GAP,
        )
        painter.drawText(
            text_rect,
            Qt.TextWordWrap | Qt.AlignHCenter | Qt.AlignTop,
            index.data(Qt.DisplayRole) or "",
        )
        painter.restore()


class GridView(QListWidget):
    video_activated = Signal(str)
    video_selected = Signal(str)
    enqueue_requested = Signal(str)
    play_requested = Signal(str, int, bool)  # video_id, chapter_index, audio_only
    play_video_at = Signal(str, int, bool)  # video_id, chapter_index, in_app

    def __init__(self) -> None:
        super().__init__()
        # Adds the window's own entries (the containing folder, hiding it) to
        # a video's menu: this view doesn't know where libraries or their
        # hidden folders are, and shouldn't need to.
        self.menu_extender: Callable[[QMenu, str], None] | None = None
        # The window says where else video can play than where it plays by
        # default: [(label, in_app)], or none.
        self.video_choices: Callable[[], list[tuple[str, bool]]] | None = None
        self.setObjectName("grid")
        self.setViewMode(QListWidget.IconMode)
        self.setFlow(QListWidget.LeftToRight)
        self.setWrapping(True)
        # Adjust re-flows the columns when the dock or window is resized;
        # without it the grid keeps whatever column count it started with.
        self.setResizeMode(QListWidget.Adjust)
        self.setMovement(QListWidget.Static)
        self.setIconSize(QSize(THUMB, THUMB))
        self.setSpacing(8)
        self.setUniformItemSizes(True)
        self.setWordWrap(True)
        self.setSelectionMode(QAbstractItemView.SingleSelection)
        self.setTextElideMode(Qt.ElideNone)
        self.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)

        self.setItemDelegate(CardDelegate(self))
        # Hover states reach the delegate only if the view tracks the mouse.
        self.setMouseTracking(True)

        self.itemActivated.connect(self._on_activated)
        self.itemSelectionChanged.connect(self._on_selection)

        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._on_context_menu)

    # --- population ------------------------------------------------------

    def populate(
        self, videos: list[tuple[str, dict]], missing=(), hidden=(), keep_position=False
    ) -> None:
        """Fill the shelf with (video_id, video) pairs, in the order given.

        `missing` names videos whose files are no longer on disk; they stay
        listed, because their chapter names are still worth keeping, but they
        say so rather than failing only when someone presses play. `hidden`
        names videos in hidden folders, which are only passed in at all when
        hidden folders are being shown - and then they are marked as such.

        `keep_position` keeps the shelf scrolled where it was: hiding a tile
        halfway down should close the gap, not throw the view back to the top.
        """
        missing = set(missing)
        hidden = set(hidden)
        scrolled = self.verticalScrollBar().value() if keep_position else 0
        self.clear()

        labels = [
            (video_id, video, _label_for(video, video_id in missing, video_id in hidden))
            for video_id, video in videos
        ]
        cell = QSize(CELL_WIDTH, CHROME_HEIGHT + self._tallest_label(
            [label for _, _, label in labels]
        ))
        self.setGridSize(cell)

        for video_id, video, label in labels:
            name = video["display_name"]
            item = QListWidgetItem()
            # Names come from filenames, so they are never treated as markup.
            item.setText(label)
            item.setIcon(covers.for_video(video_id, name, THUMB))
            item.setData(ROLE_VIDEO_ID, video_id)
            item.setData(ROLE_NAME, name)
            state = naming.status(video)
            item.setData(ROLE_STATE, state.state)
            item.setSizeHint(cell)
            item.setTextAlignment(Qt.AlignHCenter | Qt.AlignTop)
            count = len(video["chapters"])
            tip = (
                f"{name}\n{count} chapter{'' if count == 1 else 's'} · "
                f"{utils.format_seconds(video['duration'])} · {state.describe()}\n"
                f"{video['path']}"
            )
            if video_id in missing:
                item.setForeground(QColor("#e0af68"))
                tip = f"{tip}\n\nThis file is no longer at that path."
            elif video_id in hidden:
                item.setForeground(QColor("#565c69"))
                tip = f"{tip}\n\nIn a hidden folder."
            item.setToolTip(tooltip(tip))
            self.addItem(item)

        if keep_position:
            # Icon mode lays its items out lazily, so until it does the
            # scroll range is empty and any position is clamped to the top.
            self.doItemsLayout()
            self.verticalScrollBar().setValue(scrolled)
        else:
            self.scrollToTop()

    def _tallest_label(self, labels) -> int:
        """The height the longest title needs once wrapped.

        Every cell gets it, because an icon-mode grid lays out on a single
        cell size - varying them would stagger the rows.
        """
        metrics = QFontMetrics(self.font())
        # The text sits inside the cell's padding, and Qt keeps a little of
        # its own either side.
        width = CELL_WIDTH - 2 * PAD - 4
        tallest = metrics.height()
        for label in labels:
            rect = metrics.boundingRect(
                QRect(0, 0, width, 10_000),
                Qt.TextWordWrap | Qt.AlignHCenter,
                label,
            )
            tallest = max(tallest, rect.height())
        return tallest

    def set_cover(self, video_id: str) -> None:
        """Swap in a cover that has just been found, without rebuilding."""
        for row in range(self.count()):
            item = self.item(row)
            if item.data(ROLE_VIDEO_ID) == video_id:
                item.setIcon(covers.for_video(video_id, item.data(ROLE_NAME), THUMB))
                return

    def selected_video_id(self) -> str | None:
        item = self.currentItem()
        return item.data(ROLE_VIDEO_ID) if item is not None else None

    # --- actions ---------------------------------------------------------

    def _on_activated(self, item: QListWidgetItem) -> None:
        self.video_activated.emit(item.data(ROLE_VIDEO_ID))

    def _on_selection(self) -> None:
        video_id = self.selected_video_id()
        if video_id is not None:
            self.video_selected.emit(video_id)

    def _on_context_menu(self, point) -> None:
        item = self.itemAt(point)
        if item is None:
            return
        self.context_menu_for(item).exec(self.viewport().mapToGlobal(point))

    def context_menu_for(self, item: QListWidgetItem) -> QMenu:
        video_id = item.data(ROLE_VIDEO_ID)

        menu = QMenu(self)
        # Play starts at the first chapter, the way pressing play on a disc
        # would, without a detour through the chapter list.
        for label, audio_only in (("Play Audio", True), ("Play Video", False)):
            action = QAction(label, menu)
            action.triggered.connect(
                lambda _checked=False, a=audio_only: self.play_requested.emit(video_id, 0, a)
            )
            menu.addAction(action)
        for label, in_app in (self.video_choices() if self.video_choices else []):
            action = QAction(label, menu)
            action.triggered.connect(
                lambda _checked=False, i=in_app: self.play_video_at.emit(video_id, 0, i)
            )
            menu.addAction(action)
        menu.addSeparator()
        queue_action = QAction("Add to Queue", menu)
        queue_action.triggered.connect(lambda: self.enqueue_requested.emit(video_id))
        menu.addAction(queue_action)
        if self.menu_extender is not None:
            menu.addSeparator()
            self.menu_extender(menu, video_id)
        return menu


# A zero-width space: an invisible place a label may wrap.
_WRAP_HERE = "\u200b"


def _label_for(video: dict, is_missing: bool, is_hidden: bool = False) -> str:
    # A disc's folder name is often one long word joined by underscores
    # ("BABYMETAL_LEGEND_MM_20NIGHT"), which word wrap can't break and the
    # tile then cuts off at both sides: let it wrap after each underscore.
    name = video["display_name"].replace("_", "_" + _WRAP_HERE)
    if is_missing:
        return f"{name}\n(file missing)"
    if is_hidden:
        return f"{name}\n(hidden)"
    return name
