# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""One video and its chapters.

The chapter table is the app's real working surface - naming chapters is
what it is for - so it gets the room, and the actions that operate on a
whole tracklist sit directly under it.

Below those are the tools for where chapters are rather than what they are
called: estimating them for a video with none, and splitting, merging and
nudging them. Splitting works at wherever playback has got to, so the way to
fix a boundary is to play the video, pause where the song really starts,
and split there.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMenu,
    QPushButton,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from mediabrowser.core import chapter_edit, chaptergen, library, utils
from mediabrowser.gui import covers
from mediabrowser.gui.plain import tooltip

COVER = 96

# What each chapter's stored `source` means, in the order of how much the
# name can be trusted. The dot is the only place this is surfaced.
SOURCE_LABELS = {
    "manual": "Named by hand",
    "musicbrainz": "From MusicBrainz",
    "setlistfm": "From setlist.fm",
    "menu": "From the disc's own menu",
    "ai": "Named by the AI",
    "filename": "Named after the file - not checked yet",
    "embedded": "Embedded in the file",
    "auto-numbered": "Not named",
}

# How the meta line describes chapters the app made or changed.
ORIGIN_LABELS = {
    library.ORIGIN_TRACKLIST: "placed by track lengths",
    library.ORIGIN_ESTIMATED: "estimated from the audio",
    library.ORIGIN_EDITED: "edited",
    library.ORIGIN_MARKED: "marked by hand",
}

# How far the nudge buttons move a chapter's start.
NUDGE_SECONDS = 1.0


class DetailView(QWidget):
    back_requested = Signal()
    play_requested = Signal(int, bool)  # chapter_index, audio_only
    play_video_at = Signal(int, bool)  # chapter_index, in_app
    enqueue_requested = Signal(list)  # chapter indices, in order
    export_audio_requested = Signal(list)  # chapter indices, as audio files
    rename_requested = Signal(int)
    checked_requested = Signal(int)  # chapter_index: its name is right as it is
    chapters_requested = Signal()  # Detect Chapters
    edit_requested = Signal()
    reset_requested = Signal()
    split_requested = Signal()  # at the playhead
    mark_requested = Signal()  # Manual Edit
    merge_requested = Signal(int)  # chapter_index, merged with the one after
    nudge_requested = Signal(int, float)  # chapter_index, seconds to move its start

    def __init__(self) -> None:
        super().__init__()
        self._video: dict | None = None
        self._video_id: str | None = None
        # Where playback of this video has got to, absolute within the file;
        # None when this video isn't the one playing.
        self._playhead: float | None = None
        # See GridView.menu_extender: the window adds the containing folder.
        self.menu_extender: Callable[[QMenu, str], None] | None = None
        # The window says where else video can play than where it plays by
        # default: [(label, in_app)], or none.
        self.video_choices: Callable[[], list[tuple[str, bool]]] | None = None
        # The window says whether a video is locked, and what its
        # protection is called ("Locked", "Private (library)"...).
        self.protection_of: Callable[[str, dict], tuple[bool, str]] | None = None
        self._locked = False
        # What a double-click (or Enter) plays a chapter as: set_default_audio_only.
        self.default_audio_only = True

        self.back_button = QPushButton("‹  All videos")
        self.back_button.setObjectName("backButton")
        self.back_button.setCursor(Qt.PointingHandCursor)
        self.back_button.clicked.connect(self.back_requested)

        self.cover = QLabel()
        self.cover.setObjectName("artThumb")
        self.cover.setFixedSize(COVER, COVER)
        self.cover.setAlignment(Qt.AlignCenter)

        self.title = QLabel()
        self.title.setObjectName("detailTitle")
        self.title.setTextFormat(Qt.PlainText)
        self.title.setWordWrap(True)
        self.meta = QLabel()
        self.meta.setObjectName("detailMeta")
        self.meta.setTextFormat(Qt.PlainText)
        self.path_label = QLabel()
        self.path_label.setObjectName("hintLabel")
        self.path_label.setTextFormat(Qt.PlainText)
        # A long path must not set the window's minimum width.
        self.path_label.setMinimumWidth(1)
        self.path_label.setTextInteractionFlags(Qt.TextSelectableByMouse)

        heading = QVBoxLayout()
        heading.setContentsMargins(0, 0, 0, 0)
        heading.setSpacing(4)
        heading.addStretch(1)
        heading.addWidget(self.title)
        heading.addWidget(self.meta)
        heading.addWidget(self.path_label)
        heading.addStretch(1)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(14)
        header.addWidget(self.cover)
        header.addLayout(heading, 1)

        self.tree = QTreeWidget()
        self.tree.setObjectName("chapterTree")
        self.tree.setColumnCount(4)
        self.tree.setHeaderLabels(["", "#", "Chapter", "Length"])
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(True)
        # Several chapters can be picked at once (Ctrl/Shift-click) to queue
        # them together; everything else acts on the current one.
        self.tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.tree.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.tree.itemActivated.connect(self._on_activated)
        self.tree.itemSelectionChanged.connect(self._update_buttons)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._on_context_menu)

        header_view = self.tree.header()
        header_view.setSectionResizeMode(0, QHeaderView.Fixed)
        header_view.setSectionResizeMode(1, QHeaderView.Fixed)
        header_view.setSectionResizeMode(2, QHeaderView.Stretch)
        header_view.setSectionResizeMode(3, QHeaderView.Fixed)
        self.tree.setColumnWidth(0, 22)
        self.tree.setColumnWidth(1, 44)
        self.tree.setColumnWidth(3, 80)

        self.play_audio_button = QPushButton("Play Audio")
        self.play_audio_button.setObjectName("primaryButton")
        self.play_audio_button.clicked.connect(lambda: self._play(audio_only=True))
        # Plays where video plays by default; its arrow offers the other place.
        self.play_video_button = QToolButton()
        self.play_video_button.setObjectName("playVideoButton")
        self.play_video_button.setText("Play Video")
        self.play_video_button.setPopupMode(QToolButton.MenuButtonPopup)
        self.play_video_button.clicked.connect(lambda: self._play(audio_only=False))
        self._video_menu = QMenu(self.play_video_button)
        self._video_menu.aboutToShow.connect(self._fill_video_menu)
        self.play_video_button.setMenu(self._video_menu)
        self.queue_button = QPushButton("Add to Queue")
        self.queue_button.setToolTip(
            "Queue the selected chapters after whatever is playing (Ctrl-click to pick several)"
        )
        self.queue_button.clicked.connect(self._enqueue_selected)
        self.rename_button = QPushButton("Rename…")
        self.rename_button.clicked.connect(self._rename)

        self.chapters_button = QPushButton("Detect Chapters…")
        self.chapters_button.setToolTip(
            "Find where the songs start and what they're called: from the disc's menu, "
            "a tracklist (MusicBrainz, or pasted), the audio, or the AI - or let it "
            "work that out itself"
        )
        self.chapters_button.clicked.connect(self.chapters_requested)
        self.edit_button = QPushButton("Edit Tracklist…")
        self.edit_button.clicked.connect(self.edit_requested)

        chapter_actions = QHBoxLayout()
        chapter_actions.setContentsMargins(0, 0, 0, 0)
        chapter_actions.setSpacing(6)
        chapter_actions.addWidget(self.play_audio_button)
        chapter_actions.addWidget(self.play_video_button)
        self._chapter_actions = chapter_actions
        chapter_actions.addWidget(self.rename_button)
        chapter_actions.addStretch(1)
        chapter_actions.addWidget(self.chapters_button)
        chapter_actions.addWidget(self.edit_button)

        # Rarely wanted, so a menu entry (here and in the Chapters menu)
        # rather than a button: the button rows are already as wide as the
        # window's minimum allows.
        self.reset_action = QAction("Reset to the File's Chapters…", self)
        self.reset_action.setToolTip("Go back to the chapters in the file itself")
        self.reset_action.triggered.connect(self.reset_requested)
        self.mark_button = QPushButton("Manual Edit…")
        self.mark_button.setToolTip(
            "Play the video here and mark where each song starts, naming them as you "
            "go - for a video nothing else can help with"
        )
        self.mark_button.clicked.connect(self.mark_requested)
        self.split_button = QPushButton("Split at Playhead")
        self.split_button.clicked.connect(self.split_requested)
        self.merge_button = QPushButton("Merge with Next")
        self.merge_button.setToolTip("Join the selected chapter and the one after it")
        self.merge_button.clicked.connect(self._merge)
        self.earlier_button = QPushButton(f"−{NUDGE_SECONDS:g}s")
        self.earlier_button.setToolTip("Move where the selected chapter starts earlier")
        self.earlier_button.clicked.connect(lambda: self._nudge(-NUDGE_SECONDS))
        self.later_button = QPushButton(f"+{NUDGE_SECONDS:g}s")
        self.later_button.setToolTip("Move where the selected chapter starts later")
        self.later_button.clicked.connect(lambda: self._nudge(NUDGE_SECONDS))

        boundary_actions = QHBoxLayout()
        boundary_actions.setContentsMargins(0, 0, 0, 0)
        boundary_actions.setSpacing(6)
        boundary_actions.addWidget(self.queue_button)
        boundary_actions.addStretch(1)
        boundary_actions.addWidget(self.mark_button)
        boundary_actions.addWidget(self.split_button)
        boundary_actions.addWidget(self.merge_button)
        boundary_actions.addWidget(self.earlier_button)
        boundary_actions.addWidget(self.later_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        layout.addWidget(self.back_button, 0, Qt.AlignLeft)
        layout.addLayout(header)
        layout.addWidget(self.tree, 1)
        layout.addLayout(chapter_actions)
        layout.addLayout(boundary_actions)

        self._update_buttons()

    # --- population ------------------------------------------------------

    def show_video(self, video_id: str, video: dict, keep_selection: bool = False) -> None:
        previous = self.selected_chapter() if keep_selection else None
        # The rest of the selection, and where the list was scrolled: a
        # rename or a cover arriving rebuilds it, and it mustn't jump.
        also_selected = self.selected_chapters() if keep_selection else []
        scrolled = self.tree.verticalScrollBar().value()
        self._video = video
        self._video_id = video_id

        name = video["display_name"]
        self.title.setText(name)
        self.cover.setPixmap(covers.for_video(self._video_id, name, COVER))
        count = len(video["chapters"])
        kind = {"bluray": "Blu-ray title", "dvd": "DVD title"}.get(video["type"], "File")
        chapters_text = f"{count} chapter{'' if count == 1 else 's'}"
        origin = ORIGIN_LABELS.get(video.get("chapter_origin"))
        if origin:
            chapters_text += f" ({origin})"
        self._locked, protection = (
            self.protection_of(video_id, video) if self.protection_of is not None
            else (False, "")
        )
        meta = f"{kind}  ·  {chapters_text}  ·  {utils.format_seconds(video['duration'])}"
        if protection:
            meta += f"  ·  {protection}"
        self.meta.setText(meta)
        self.path_label.setText(video["path"])

        self.tree.clear()
        for i, chapter in enumerate(video["chapters"]):
            source = chapter.get("source") or "auto-numbered"
            estimated = chapter.get("estimated", False)
            item = QTreeWidgetItem([
                "●",
                str(i + 1),
                utils.chapter_label(i, chapter),
                ("~" if estimated else "")
                + utils.format_seconds(chapter["end"] - chapter["start"]),
            ])
            item.setData(0, Qt.UserRole, i)
            item.setTextAlignment(1, Qt.AlignRight | Qt.AlignVCenter)
            item.setTextAlignment(3, Qt.AlignRight | Qt.AlignVCenter)
            starts = f"Starts at {utils.format_seconds(chapter['start'])}"
            if estimated:
                starts += " - an estimate from the audio, not yet checked"
            item.setToolTip(3, starts)
            item.setToolTip(0, SOURCE_LABELS.get(source, source))
            item.setForeground(0, _source_brush(source))
            if chapter.get("original_title"):
                item.setToolTip(2, tooltip(f"Original title: {chapter['original_title']}"))
            if not chapter["title"]:
                item.setForeground(2, _source_brush("auto-numbered"))
            item.setSizeHint(0, QSize(0, 26))
            self.tree.addTopLevelItem(item)

        if previous is not None and previous < self.tree.topLevelItemCount():
            self.tree.setCurrentItem(self.tree.topLevelItem(previous))
            for index in also_selected:
                if index < self.tree.topLevelItemCount():
                    self.tree.topLevelItem(index).setSelected(True)
        if keep_selection:
            # Laid out now, or the scroll range is still empty and clamps it to 0.
            self.tree.doItemsLayout()
            self.tree.verticalScrollBar().setValue(scrolled)
        self._update_buttons()

    def selected_chapter(self) -> int | None:
        item = self.tree.currentItem()
        return item.data(0, Qt.UserRole) if item is not None else None

    def selected_chapters(self) -> list[int]:
        """Every selected chapter, in disc order."""
        return sorted(item.data(0, Qt.UserRole) for item in self.tree.selectedItems())

    def set_playhead(self, position: float | None) -> None:
        """Where playback of the shown video has got to, or None when it
        isn't the one playing. Drives Split at Playhead.
        """
        self._playhead = position
        self._update_buttons()

    def split_target(self) -> int | None:
        """The chapter a split at the playhead would cut, if one would."""
        if self._video is None or self._playhead is None:
            return None
        chapters = self._video["chapters"]
        index = chapter_edit.chapter_at(chapters, self._playhead)
        if index is None:
            return None
        chapter = chapters[index]
        shortest = chaptergen.MIN_CHAPTER_SECONDS
        if (self._playhead - chapter["start"] < shortest
                or chapter["end"] - self._playhead < shortest):
            return None
        return index

    # --- actions ---------------------------------------------------------

    def set_default_audio_only(self, audio_only: bool) -> None:
        """Which of Play Audio and Play Video is the main one: first, picked
        out, and what a double-click does."""
        self.default_audio_only = audio_only
        first, second = (
            (self.play_audio_button, self.play_video_button) if audio_only
            else (self.play_video_button, self.play_audio_button)
        )
        for index, button in enumerate((first, second)):
            self._chapter_actions.removeWidget(button)
            self._chapter_actions.insertWidget(index, button)
        self.play_audio_button.setObjectName("primaryButton" if audio_only else "")
        self.play_video_button.setProperty("primary", not audio_only)
        for button in (first, second):
            button.style().unpolish(button)
            button.style().polish(button)

    def _on_activated(self, item: QTreeWidgetItem) -> None:
        self.play_requested.emit(item.data(0, Qt.UserRole), self.default_audio_only)

    def _play(self, audio_only: bool) -> None:
        index = self.selected_chapter()
        if index is not None:
            self.play_requested.emit(index, audio_only)

    def _on_context_menu(self, point) -> None:
        item = self.tree.itemAt(point)
        if item is None:
            return
        # Selecting it too keeps the buttons below in step with the menu.
        self.tree.setCurrentItem(item)
        self.context_menu_for(item).exec(self.tree.viewport().mapToGlobal(point))

    def context_menu_for(self, item: QTreeWidgetItem) -> QMenu:
        index = item.data(0, Qt.UserRole)
        # Right-clicking inside a multiple selection queues all of it;
        # anywhere else, just the chapter under the mouse.
        selected = self.selected_chapters()
        to_queue = selected if index in selected and len(selected) > 1 else [index]

        menu = QMenu(self)
        for label, audio_only in (("Play Audio", True), ("Play Video", False)):
            action = QAction(label, menu)
            action.triggered.connect(
                lambda _checked=False, a=audio_only: self.play_requested.emit(index, a)
            )
            menu.addAction(action)
        for label, in_app in (self.video_choices() if self.video_choices else []):
            action = QAction(label, menu)
            action.triggered.connect(
                lambda _checked=False, i=in_app: self.play_video_at.emit(index, i)
            )
            menu.addAction(action)
        queue_action = QAction(
            "Add to Queue" if len(to_queue) == 1 else f"Add {len(to_queue)} Chapters to Queue",
            menu,
        )
        queue_action.triggered.connect(lambda: self.enqueue_requested.emit(to_queue))
        menu.addAction(queue_action)
        export_action = QAction(
            "Export as Audio…" if len(to_queue) == 1
            else f"Export {len(to_queue)} Songs as Audio…",
            menu,
        )
        export_action.setToolTip("Save as FLAC or Opus files in a folder outside the library")
        export_action.triggered.connect(lambda: self.export_audio_requested.emit(to_queue))
        menu.addAction(export_action)
        menu.addSeparator()
        rename_action = QAction("Rename…", menu)
        rename_action.setEnabled(not self._locked)
        rename_action.triggered.connect(lambda: self.rename_requested.emit(index))
        menu.addAction(rename_action)
        chapter = self._video["chapters"][index] if self._video else {}
        if (chapter.get("title") and chapter.get("source") not in ("manual", None)
                and not self._locked):
            # A name taken from the file, the AI or anywhere else, confirmed
            # as right by the person: theirs now.
            checked = QAction("Mark Name as Checked", menu)
            checked.setToolTip("It's right as it is - count it as named by you")
            checked.triggered.connect(lambda: self.checked_requested.emit(index))
            menu.addAction(checked)

        menu.addSeparator()
        count = len(self._video["chapters"]) if self._video else 0
        merge_action = QAction("Merge with Next", menu)
        merge_action.setEnabled(index < count - 1 and not self._locked)
        merge_action.triggered.connect(lambda: self.merge_requested.emit(index))
        menu.addAction(merge_action)
        for label, delta in (
            (f"Start {NUDGE_SECONDS:g}s Earlier", -NUDGE_SECONDS),
            (f"Start {NUDGE_SECONDS:g}s Later", NUDGE_SECONDS),
        ):
            action = QAction(label, menu)
            action.setEnabled(index > 0 and not self._locked)
            action.triggered.connect(
                lambda _checked=False, d=delta: self.nudge_requested.emit(index, d)
            )
            menu.addAction(action)
        if self.reset_action.isEnabled():
            menu.addAction(self.reset_action)
        if self.menu_extender is not None and self._video_id is not None:
            menu.addSeparator()
            self.menu_extender(menu, self._video_id)
        return menu

    def _fill_video_menu(self) -> None:
        self._video_menu.clear()
        index = self.selected_chapter()
        for label, in_app in (self.video_choices() if self.video_choices else []):
            action = self._video_menu.addAction(label)
            action.setEnabled(index is not None)
            action.triggered.connect(
                lambda _checked=False, i=in_app: self.play_video_at.emit(index, i)
            )

    def _enqueue_selected(self) -> None:
        chapters = self.selected_chapters()
        if chapters:
            self.enqueue_requested.emit(chapters)

    def _merge(self) -> None:
        index = self.selected_chapter()
        if index is not None:
            self.merge_requested.emit(index)

    def _nudge(self, delta: float) -> None:
        index = self.selected_chapter()
        if index is not None:
            self.nudge_requested.emit(index, delta)

    def _rename(self) -> None:
        index = self.selected_chapter()
        if index is not None:
            self.rename_requested.emit(index)

    def _update_buttons(self) -> None:
        has_video = self._video is not None
        has_chapter = self.selected_chapter() is not None
        for button in (self.play_audio_button, self.play_video_button):
            button.setEnabled(has_chapter)
        # Locked: it plays and queues, and nothing else.
        changeable = not self._locked
        self.rename_button.setEnabled(has_chapter and changeable)
        count_selected = len(self.tree.selectedItems())
        self.queue_button.setEnabled(count_selected > 0)
        # The label stays put - a changing width would shuffle the row - and
        # the tooltip says how many.
        self.queue_button.setToolTip(
            f"Queue the {count_selected} selected chapters after whatever is playing"
            if count_selected > 1
            else "Queue the selected chapter after whatever is playing "
            "(Ctrl-click to pick several)"
        )
        for button in (self.chapters_button, self.edit_button, self.mark_button):
            button.setEnabled(has_video and changeable)

        index = self.selected_chapter()
        count = len(self._video["chapters"]) if has_video else 0
        self.merge_button.setEnabled(changeable and index is not None and index < count - 1)
        self.earlier_button.setEnabled(changeable and index is not None and index > 0)
        self.later_button.setEnabled(changeable and index is not None and index > 0)

        self.reset_action.setEnabled(
            has_video and changeable and bool(self._video.get("chapter_origin"))
        )

        target = self.split_target() if changeable else None
        if target is not None:
            self.split_button.setEnabled(True)
            self.split_button.setText(f"Split at {utils.format_seconds(self._playhead)}")
            self.split_button.setToolTip(
                f"Start a new chapter at {utils.format_seconds(self._playhead)}, "
                f"splitting chapter {target + 1}"
            )
        else:
            self.split_button.setEnabled(False)
            self.split_button.setText("Split at Playhead")
            self.split_button.setToolTip(
                "Play this video and pause where a song starts, then split there"
            )


def _source_brush(source: str):
    from PySide6.QtGui import QColor

    return QColor({
        "manual": "#9ece6a",
        "musicbrainz": "#7aa2f7",
        "setlistfm": "#ff9e64",
        "menu": "#73daca",
        "ai": "#bb9af7",
        "filename": "#a08a60",
        "embedded": "#8a91a0",
        "auto-numbered": "#3a3f4b",
    }.get(source, "#565c69"))
