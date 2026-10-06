# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""The library as a list.

The same shelf as the grid, in the form that suits a long library and a
search: one row per video, titles never shortened, and - when a search is
running - the chapters that actually matched sitting under their video,
ready to play without opening anything first.

That last part is the point of it. Searching for a song across a dozen
concert discs is no use if the answer is "it is somewhere in this one".
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QAction, QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QMenu,
    QTreeWidget,
    QTreeWidgetItem,
)

from mediabrowser.core import library, naming, utils
from mediabrowser.gui import covers, identified
from mediabrowser.gui.plain import tooltip

THUMB = 28

ROLE_VIDEO_ID = Qt.UserRole
ROLE_CHAPTER = Qt.UserRole + 1
ROLE_NAME = Qt.UserRole + 2

COLUMNS = ["Video / Chapter", "Type", "Chapters", "Length", "Identified", "Protection"]
COL_NAME, COL_TYPE, COL_CHAPTERS, COL_LENGTH, COL_IDENTIFIED, COL_PROTECTION = range(6)

SOURCE_COLOURS = {
    "manual": "#9ece6a",
    "menu": "#73daca",
    "musicbrainz": "#7aa2f7",
    "setlistfm": "#ff9e64",
    "ai": "#bb9af7",
    "filename": "#a08a60",
    "embedded": "#8a91a0",
    "auto-numbered": "#3a3f4b",
}


class ListView(QTreeWidget):
    video_activated = Signal(str)
    video_selected = Signal(str)
    enqueue_requested = Signal(str)
    chapter_enqueue_requested = Signal(str, int)  # video_id, chapter_index
    chapter_activated = Signal(str, int)
    play_requested = Signal(str, int, bool)  # video_id, chapter_index, audio_only
    play_video_at = Signal(str, int, bool)  # video_id, chapter_index, in_app

    def __init__(self) -> None:
        super().__init__()
        # See GridView.menu_extender.
        self.menu_extender: Callable[[QMenu, str], None] | None = None
        # The window says where else video can play than where it plays by
        # default: [(label, in_app)], or none.
        self.video_choices: Callable[[], list[tuple[str, bool]]] | None = None
        # The window says what protects a video: (label, tooltip), both
        # empty for none.
        self.protection_of: Callable[[str, dict], tuple[str, str]] | None = None
        self.setObjectName("shelfList")
        self.setColumnCount(len(COLUMNS))
        self.setHeaderLabels(COLUMNS)
        self.setRootIsDecorated(True)
        self.setAlternatingRowColors(True)
        self.setSelectionMode(QAbstractItemView.SingleSelection)
        self.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.setIconSize(QSize(THUMB, THUMB))
        # Titles are shown whole; the column stretches, and anything longer
        # than the pane scrolls rather than being cut off.
        self.setTextElideMode(Qt.ElideNone)

        header = self.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(COL_NAME, QHeaderView.Stretch)
        for column, width in ((COL_TYPE, 130), (COL_CHAPTERS, 75), (COL_LENGTH, 70),
                              (COL_IDENTIFIED, 130), (COL_PROTECTION, 110)):
            header.setSectionResizeMode(column, QHeaderView.Interactive)
            self.setColumnWidth(column, width)

        self.itemActivated.connect(self._on_activated)
        self.itemSelectionChanged.connect(self._on_selection)

        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._on_context_menu)

    # --- population ------------------------------------------------------

    def populate(
        self, videos, missing=(), search: str = "", hidden=(), keep_position=False
    ) -> None:
        """Fill the list with (video_id, video) pairs, in the order given.

        With a search running, each video carries the chapters that matched
        it as children, already expanded. Without one, videos stand alone -
        listing every chapter of every disc unasked would bury the shelf.
        Videos in `hidden` (only passed when hidden folders are being shown)
        are dimmed and marked. `keep_position` keeps the list scrolled where
        it was, as the grid does.
        """
        missing = set(missing)
        hidden = set(hidden)
        scrolled = self.verticalScrollBar().value() if keep_position else 0
        self.clear()

        for video_id, video in videos:
            name = video["display_name"]
            count = len(video["chapters"])
            state = naming.status(video)
            protection, protection_tip = (
                self.protection_of(video_id, video) if self.protection_of is not None
                else ("", "")
            )
            row = QTreeWidgetItem([
                name,
                library.media_label(video),
                str(count),
                utils.format_seconds(video["duration"]),
                ("● " if state.state != naming.SINGLE else "") + state.describe(),
                protection,
            ])
            row.setForeground(COL_IDENTIFIED, QColor(identified.colour(state.state)))
            row.setToolTip(COL_IDENTIFIED, identified.TIPS[state.state])
            kind = library.media_type(video)
            row.setToolTip(COL_TYPE, library.MEDIA_TIPS.get(
                kind, "A video file - whether it came with chapters is found on the next rescan."
            ))
            row.setForeground(COL_TYPE, QColor("#8a91a0"))
            if protection:
                row.setToolTip(COL_PROTECTION, protection_tip)
                row.setForeground(COL_PROTECTION, QColor(
                    "#f7768e" if protection.startswith("Locked") else "#e0af68"
                ))
            row.setIcon(0, covers.for_video(video_id, name, THUMB))
            row.setData(0, ROLE_VIDEO_ID, video_id)
            row.setData(0, ROLE_CHAPTER, None)
            row.setData(0, ROLE_NAME, name)
            row.setTextAlignment(COL_CHAPTERS, Qt.AlignRight | Qt.AlignVCenter)
            row.setTextAlignment(COL_LENGTH, Qt.AlignRight | Qt.AlignVCenter)
            row.setSizeHint(0, QSize(0, THUMB + 8))

            tip = f"{name}\n{video['path']}"
            if video_id in missing:
                row.setText(0, f"{name}  (file missing)")
                row.setForeground(0, QColor("#e0af68"))
                tip = f"{tip}\n\nThis file is no longer at that path."
            elif video_id in hidden:
                row.setText(0, f"{name}  (hidden)")
                for column in (COL_NAME, COL_CHAPTERS, COL_LENGTH):
                    row.setForeground(column, QColor("#565c69"))
                tip = f"{tip}\n\nIn a hidden folder."
            row.setToolTip(0, tooltip(tip))

            self.addTopLevelItem(row)

            if search:
                self._add_matches(row, video_id, video, search)

        if keep_position:
            self.doItemsLayout()
            self.verticalScrollBar().setValue(scrolled)
        else:
            self.scrollToTop()

    def _add_matches(self, row, video_id, video, search: str) -> None:
        for index, chapter in utils.matching_chapters(video["chapters"], search):
            source = chapter.get("source") or "auto-numbered"
            child = QTreeWidgetItem([""] * len(COLUMNS))
            child.setText(COL_NAME, utils.chapter_label(index, chapter))
            child.setText(COL_CHAPTERS, str(index + 1))
            child.setText(COL_LENGTH, utils.format_seconds(chapter["end"] - chapter["start"]))
            child.setData(0, ROLE_VIDEO_ID, video_id)
            child.setData(0, ROLE_CHAPTER, index)
            child.setTextAlignment(COL_CHAPTERS, Qt.AlignRight | Qt.AlignVCenter)
            child.setTextAlignment(COL_LENGTH, Qt.AlignRight | Qt.AlignVCenter)
            child.setToolTip(0, "Double-click to play this chapter")
            # Same provenance colouring the detail view's dots use, so a
            # name found by search carries how much to trust it.
            child.setForeground(COL_CHAPTERS, QColor(SOURCE_COLOURS.get(source, "#565c69")))
            if not chapter["title"]:
                child.setForeground(0, QColor(SOURCE_COLOURS["auto-numbered"]))
            row.addChild(child)
        row.setExpanded(True)

    def set_cover(self, video_id: str) -> None:
        """Swap in a cover that has just been found, without rebuilding."""
        for row in range(self.topLevelItemCount()):
            item = self.topLevelItem(row)
            if item.data(0, ROLE_VIDEO_ID) == video_id:
                name = item.data(0, ROLE_NAME)
                item.setIcon(0, covers.for_video(video_id, name, THUMB))
                return

    def selected_video_id(self) -> str | None:
        item = self.currentItem()
        return item.data(0, ROLE_VIDEO_ID) if item is not None else None

    # --- actions ---------------------------------------------------------

    def _on_activated(self, item: QTreeWidgetItem, _column: int = 0) -> None:
        video_id = item.data(0, ROLE_VIDEO_ID)
        chapter = item.data(0, ROLE_CHAPTER)
        if chapter is None:
            self.video_activated.emit(video_id)
        else:
            self.chapter_activated.emit(video_id, chapter)

    def _on_selection(self) -> None:
        video_id = self.selected_video_id()
        if video_id is not None:
            self.video_selected.emit(video_id)

    def _on_context_menu(self, point) -> None:
        item = self.itemAt(point)
        if item is None:
            return
        self.context_menu_for(item).exec(self.viewport().mapToGlobal(point))

    def context_menu_for(self, item: QTreeWidgetItem) -> QMenu:
        video_id = item.data(0, ROLE_VIDEO_ID)
        chapter = item.data(0, ROLE_CHAPTER)

        menu = QMenu(self)
        # A found chapter plays from itself; a video row from its start.
        start = 0 if chapter is None else chapter
        for label, audio_only in (("Play Audio", True), ("Play Video", False)):
            action = QAction(label, menu)
            action.triggered.connect(
                lambda _checked=False, a=audio_only: self.play_requested.emit(
                    video_id, start, a
                )
            )
            menu.addAction(action)
        for label, in_app in (self.video_choices() if self.video_choices else []):
            action = QAction(label, menu)
            action.triggered.connect(
                lambda _checked=False, i=in_app: self.play_video_at.emit(video_id, start, i)
            )
            menu.addAction(action)
        menu.addSeparator()

        if chapter is not None:
            # Double-clicking a found chapter plays it, so opening its video
            # is something the menu adds rather than repeats.
            open_action = QAction("Open Video", menu)
            open_action.triggered.connect(lambda: self.video_activated.emit(video_id))
            menu.addAction(open_action)
            chapter_queue = QAction("Add to Queue", menu)
            chapter_queue.triggered.connect(
                lambda: self.chapter_enqueue_requested.emit(video_id, chapter)
            )
            menu.addAction(chapter_queue)

        queue_action = QAction("Add Video to Queue", menu)
        queue_action.triggered.connect(lambda: self.enqueue_requested.emit(video_id))
        menu.addAction(queue_action)
        if self.menu_extender is not None:
            menu.addSeparator()
            self.menu_extender(menu, video_id)
        return menu
