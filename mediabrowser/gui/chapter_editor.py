# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Manual Edit: set a video's chapters and names by hand, watching it.

For anyone without an AI account, and for any video nothing else can
name, this is the whole job, so it gets the main area: the video plays
right here (mpv, drawn into this window), with a timeline of its chapters
under it and the chapters themselves beside it.

Finding where a song starts is a matter of getting close and then exact:
jump ten seconds, five, one, then a frame at a time, and insert a chapter
at the playhead with one click or M. Its name can be typed straight away
- or the setlist pasted first, and each new chapter takes the next name.
A boundary that's a little out is dragged along the timeline, or moved to
the playhead.

Keys work wherever you aren't typing: Space plays and pauses, ←/→ step a
second (Shift five, Ctrl ten), , and . step a frame, Page Up/Down go to the
previous or next chapter, M inserts, Delete removes, Enter names.

Nothing is changed until Save. A chapter whose start and name are as they
were keeps where its name came from; if only names changed, a disc's own
chapters stay the disc's.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QEvent, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QAbstractSpinBox,
    QApplication,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from mediabrowser.core import library, marking, utils
from mediabrowser.core.player import Player
from mediabrowser.gui.mpv_poll import MpvPoll
from mediabrowser.gui.timeline import Timeline
from mediabrowser.gui.video_surface import ELSEWHERE, VideoSurface, can_embed, elsewhere_text

# How often the editor asks mpv where playback is: fine enough for the
# playhead to move smoothly on the timeline.
POLL_MS = 150
# The steps the buttons and keys take, in seconds.
SMALL, MEDIUM, LARGE = 1.0, 5.0, 10.0
NUDGE = 1.0
# A chapter within this of where one was is the same chapter.
SAME_START_SECONDS = 0.05

KEYS_HINT = (
    "Space play/pause · ←/→ 1s (Shift 5s, Ctrl 10s) · , and . one frame · "
    "Page Up/Down previous/next chapter · M insert a chapter · Enter name it · "
    "Delete remove it"
)


class ChapterEditor(QWidget):
    saved = Signal(str, list, object)  # video id, chapters, origin (None: the file's own)
    closed = Signal()

    def __init__(self, player_factory: Callable[[], Player] = Player) -> None:
        super().__init__()
        self.setObjectName("chapterEditor")
        self._player_factory = player_factory
        self.player: Player | None = None
        self.video_id: str | None = None
        self.video: dict | None = None
        self.sheet: marking.MarkSheet | None = None
        self._original: list[dict] = []
        self._position = 0.0
        self._paused: bool | None = None
        self._dirty = False
        self._embedded = False
        # Changed by whatever moves playback from here - a seek, a step,
        # play or pause, another mpv - so an answer asked of mpv before it,
        # arriving after, can't put the playhead back where it was.
        self._generation = 0

        self._build()
        # Off the GUI thread, as the main window's (see mpv_poll).
        self._poll = MpvPoll(self._ask_mpv, self._mpv_answered, POLL_MS, self)

    # --- construction ----------------------------------------------------

    def _build(self) -> None:
        self.back_button = QPushButton("‹  Chapters")
        self.back_button.setObjectName("backButton")
        self.back_button.clicked.connect(self.request_close)
        self.title = QLabel()
        self.title.setObjectName("detailTitle")
        self.title.setTextFormat(Qt.PlainText)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self.request_close)
        self.save_button = QPushButton("Save Chapters")
        self.save_button.setObjectName("primaryButton")
        self.save_button.clicked.connect(self.save)
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.addWidget(self.back_button)
        top.addWidget(self.title, 1)
        top.addWidget(self.cancel_button)
        top.addWidget(self.save_button)

        # --- the picture and how to move through it
        self.surface = VideoSurface()
        self.elsewhere = QLabel(ELSEWHERE)
        self.elsewhere.setObjectName("hintLabel")
        self.elsewhere.setAlignment(Qt.AlignCenter)
        self.elsewhere.setWordWrap(True)
        self.elsewhere.hide()
        self.timeline = Timeline()
        self.timeline.seek_requested.connect(self.seek)
        self.timeline.boundary_moved.connect(self._on_boundary_moved)
        self.timeline.boundary_clicked.connect(self._select)

        self.position_label = QLabel("0:00.0")
        self.position_label.setObjectName("editorTime")
        self.position_label.setTextFormat(Qt.PlainText)
        mono = QFont(self.position_label.font())
        mono.setStyleHint(QFont.Monospace)
        mono.setFamily("monospace")
        self.position_label.setFont(mono)

        transport = QHBoxLayout()
        transport.setContentsMargins(0, 0, 0, 0)
        transport.setSpacing(4)
        self.play_button = QPushButton("Play")
        self.play_button.setObjectName("primaryButton")
        self.play_button.setMinimumWidth(80)
        self.play_button.clicked.connect(self.play_pause)
        buttons = [
            ("⏮", "Previous chapter (Page Up)", lambda: self.go_to_chapter(-1)),
            (f"−{LARGE:g}s", "Ctrl+←", lambda: self.step(-LARGE)),
            (f"−{MEDIUM:g}s", "Shift+←", lambda: self.step(-MEDIUM)),
            (f"−{SMALL:g}s", "←", lambda: self.step(-SMALL)),
            ("◀|", "One frame back ( , )", lambda: self.frame(False)),
            (None, None, None),
            ("|▶", "One frame on ( . )", lambda: self.frame(True)),
            (f"+{SMALL:g}s", "→", lambda: self.step(SMALL)),
            (f"+{MEDIUM:g}s", "Shift+→", lambda: self.step(MEDIUM)),
            (f"+{LARGE:g}s", "Ctrl+→", lambda: self.step(LARGE)),
            ("⏭", "Next chapter (Page Down)", lambda: self.go_to_chapter(1)),
        ]
        self.transport_buttons = []
        for label, tip, slot in buttons:
            if label is None:
                transport.addWidget(self.play_button)
                continue
            button = QPushButton(label)
            button.setObjectName("transportButton")
            button.setToolTip(tip)
            button.setFocusPolicy(Qt.NoFocus)
            button.clicked.connect(slot)
            transport.addWidget(button)
            self.transport_buttons.append(button)
        self.play_button.setFocusPolicy(Qt.NoFocus)
        transport.addStretch(1)
        transport.addWidget(self.position_label)

        self.insert_button = QPushButton("＋  Insert chapter here   (M)")
        self.insert_button.setObjectName("primaryButton")
        self.insert_button.setMinimumHeight(38)
        self.insert_button.setFocusPolicy(Qt.NoFocus)
        self.insert_button.clicked.connect(self.insert_here)
        self.message = QLabel("")
        self.message.setObjectName("hintLabel")
        self.message.setTextFormat(Qt.PlainText)
        self.message.setWordWrap(True)
        insert_row = QHBoxLayout()
        insert_row.setContentsMargins(0, 0, 0, 0)
        insert_row.addWidget(self.insert_button, 1)
        insert_row.addWidget(self.message, 1)

        keys = QLabel(KEYS_HINT)
        keys.setObjectName("hintLabel")
        keys.setWordWrap(True)

        player_side = QWidget()
        left = QVBoxLayout(player_side)
        left.setContentsMargins(0, 0, 0, 0)
        left.setSpacing(6)
        left.addWidget(self.surface, 1)
        left.addWidget(self.elsewhere)
        left.addWidget(self.timeline)
        left.addLayout(transport)
        left.addLayout(insert_row)
        left.addWidget(keys)

        # --- the chapters
        caption = QLabel("Chapters")
        caption.setObjectName("sectionCaption")
        self.tree = QTreeWidget()
        self.tree.setObjectName("chapterTree")
        self.tree.setColumnCount(4)
        self.tree.setHeaderLabels(["#", "Starts", "Name", "Length"])
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(True)
        self.tree.setSelectionMode(QAbstractItemView.SingleSelection)
        self.tree.setEditTriggers(QAbstractItemView.DoubleClicked
                                  | QAbstractItemView.EditKeyPressed)
        self.tree.itemSelectionChanged.connect(self._on_selected)
        self.tree.itemChanged.connect(self._on_item_edited)
        self.tree.itemActivated.connect(lambda item, _col: self.play_from_selected())
        header = self.tree.header()
        header.setSectionResizeMode(0, QHeaderView.Fixed)
        header.setSectionResizeMode(1, QHeaderView.Fixed)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        header.setSectionResizeMode(3, QHeaderView.Fixed)
        self.tree.setColumnWidth(0, 36)
        self.tree.setColumnWidth(1, 72)
        self.tree.setColumnWidth(3, 64)

        self.name = QLineEdit()
        self.name.setPlaceholderText("Name of the selected chapter…")
        self.name.returnPressed.connect(self._name_entered)
        self.name.editingFinished.connect(self._commit_name)
        name_row = QHBoxLayout()
        name_row.setContentsMargins(0, 0, 0, 0)
        name_row.addWidget(QLabel("Name:"))
        name_row.addWidget(self.name, 1)

        self.go_button = QPushButton("Play from Here")
        self.go_button.clicked.connect(self.play_from_selected)
        self.move_button = QPushButton("Move Here")
        self.move_button.setToolTip("Start the selected chapter at the playhead")
        self.move_button.clicked.connect(self.move_selected_here)
        self.earlier_button = QPushButton(f"−{NUDGE:g}s")
        self.earlier_button.clicked.connect(lambda: self.nudge_selected(-NUDGE))
        self.later_button = QPushButton(f"+{NUDGE:g}s")
        self.later_button.clicked.connect(lambda: self.nudge_selected(NUDGE))
        self.remove_button = QPushButton("Remove")
        self.remove_button.setToolTip("Take the selected chapter away (Delete)")
        self.remove_button.clicked.connect(self.remove_selected)
        mark_row = QHBoxLayout()
        mark_row.setContentsMargins(0, 0, 0, 0)
        mark_row.setSpacing(4)
        for button in (self.go_button, self.move_button, self.earlier_button,
                       self.later_button, self.remove_button):
            button.setFocusPolicy(Qt.NoFocus)
            mark_row.addWidget(button)
        mark_row.addStretch(1)

        names_caption = QLabel("Names in order (optional)")
        names_caption.setObjectName("sectionCaption")
        names_hint = QLabel(
            "Paste the setlist, one song per line, and each chapter you insert takes "
            "the next name."
        )
        names_hint.setObjectName("hintLabel")
        names_hint.setWordWrap(True)
        self.names = QPlainTextEdit()
        self.names.setPlaceholderText("Opening\nMegitsune\nGimme Chocolate!!\n…")
        self.names.setMaximumHeight(130)
        self._names_timer = QTimer(self)
        self._names_timer.setSingleShot(True)
        self._names_timer.setInterval(300)
        self._names_timer.timeout.connect(self._on_names_changed)
        self.names.textChanged.connect(self._names_timer.start)
        self.apply_names_button = QPushButton("Name Chapters in Order")
        self.apply_names_button.setToolTip(
            "Put these names on the chapters in order, from the selected one"
        )
        self.apply_names_button.clicked.connect(self.apply_names)
        self.next_name = QLabel("")
        self.next_name.setObjectName("hintLabel")
        self.next_name.setTextFormat(Qt.PlainText)

        chapters_side = QWidget()
        right = QVBoxLayout(chapters_side)
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(6)
        right.addWidget(caption)
        right.addWidget(self.tree, 1)
        right.addLayout(name_row)
        right.addLayout(mark_row)
        right.addWidget(names_caption)
        right.addWidget(names_hint)
        right.addWidget(self.names)
        names_buttons = QHBoxLayout()
        names_buttons.setContentsMargins(0, 0, 0, 0)
        names_buttons.addWidget(self.apply_names_button)
        names_buttons.addWidget(self.next_name, 1)
        right.addLayout(names_buttons)

        self.splitter = QSplitter(Qt.Horizontal)
        self.splitter.addWidget(player_side)
        self.splitter.addWidget(chapters_side)
        self.splitter.setStretchFactor(0, 3)
        self.splitter.setStretchFactor(1, 2)
        self.splitter.setChildrenCollapsible(False)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        layout.addLayout(top)
        layout.addWidget(self.splitter, 1)

    # --- opening and closing ---------------------------------------------------

    def active(self) -> bool:
        return self.video is not None

    def open(self, video_id: str, video: dict, start: float = 0.0) -> None:
        """Edit `video`'s chapters, the picture paused at `start`."""
        self.video_id, self.video = video_id, video
        self._original = [dict(c) for c in video["chapters"]]
        self.sheet = marking.MarkSheet(video["duration"], video["chapters"])
        self._position = max(0.0, start)
        self._paused = True
        self._dirty = False
        self.title.setText(f"Manual Edit · {video['display_name']}")
        self.message.setText("")
        self.names.blockSignals(True)
        self.names.clear()
        self.names.blockSignals(False)
        self._refresh(select=self.sheet.at(self._position))
        self._embedded = can_embed()
        self.elsewhere.setText(elsewhere_text())
        self.elsewhere.setVisible(not self._embedded)
        self.surface.setVisible(self._embedded)
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)
        # The surface needs to be on screen, with a native window, before
        # mpv can draw into it.
        QTimer.singleShot(0, self._start_player)
        self._poll.start()

    def _start_player(self, paused: bool = True) -> None:
        if not self.active():
            return
        if self.player is None:
            self.player = self._player_factory()
        window_id = int(self.surface.winId()) if self._embedded else None
        self._generation += 1
        try:
            self.player.open_for_editing(self.video, self._position, window_id, paused=paused)
        except OSError as exc:
            self.message.setText(f"mpv couldn't be started: {exc}")
            return
        self._paused = paused

    def request_close(self) -> bool:
        """Close, asking first if there are changes. Returns whether it did."""
        if not self.active():
            return True
        if self._dirty:
            answer = QMessageBox.question(
                self, "Manual Edit", "Discard the changes to these chapters?",
                QMessageBox.Discard | QMessageBox.Cancel, QMessageBox.Cancel,
            )
            if answer != QMessageBox.Discard:
                return False
        self._close()
        return True

    def _close(self) -> None:
        self._poll.stop()
        self._generation += 1
        if self.player is not None:
            self.player.stop()
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)
        self.video_id = self.video = self.sheet = None
        self.closed.emit()

    def save(self) -> None:
        if not self.active():
            return
        self._commit_name()
        video_id = self.video_id
        chapters, origin = self.result()
        self._dirty = False
        self._close()
        self.saved.emit(video_id, chapters, origin)

    def result(self) -> tuple[list[dict], str | None]:
        """The chapters as edited, and what their origin is: the video's own
        if only names changed, else marked by hand. A chapter starting and
        named as before keeps where its name came from."""
        chapters = self.sheet.chapters()
        same_starts = len(chapters) == len(self._original) and all(
            abs(a["start"] - b["start"]) <= SAME_START_SECONDS
            for a, b in zip(chapters, self._original, strict=True)
        )
        for chapter in chapters:
            before = next((c for c in self._original
                           if abs(c["start"] - chapter["start"]) <= SAME_START_SECONDS), None)
            if before is None:
                continue
            if (before.get("title") or None) == chapter["title"] and chapter["title"]:
                for key in ("source", "original_title"):
                    if key in before:
                        chapter[key] = before[key]
            if before.get("estimated") and abs(before["start"] - chapter["start"]) < 1e-6:
                chapter["estimated"] = True
        origin = self.video.get("chapter_origin") if same_starts else library.ORIGIN_MARKED
        return chapters, origin

    # --- the list ------------------------------------------------------------

    def selected(self) -> int | None:
        item = self.tree.currentItem()
        return item.data(0, Qt.UserRole) if item is not None else None

    def _select(self, index: int) -> None:
        if 0 <= index < self.tree.topLevelItemCount():
            self.tree.setCurrentItem(self.tree.topLevelItem(index))

    def _refresh(self, select: int | None = None) -> None:
        if select is None:
            select = self.selected()
        marks = self.sheet.marks()
        self.tree.blockSignals(True)
        self.tree.clear()
        for i, mark in enumerate(marks):
            item = QTreeWidgetItem([
                str(i + 1), utils.format_seconds(mark.start), mark.title or "",
                utils.format_seconds(self.sheet.length_of(i)),
            ])
            item.setData(0, Qt.UserRole, i)
            item.setFlags(item.flags() | Qt.ItemIsEditable)
            for column in (0, 1, 3):
                item.setTextAlignment(column, Qt.AlignRight | Qt.AlignVCenter)
            if not mark.title:
                item.setText(2, utils.title_or_number(i, None))
                item.setForeground(2, QColor("#3a3f4b"))
            self.tree.addTopLevelItem(item)
        self.tree.blockSignals(False)
        if select is not None:
            self._select(min(select, len(marks) - 1))
        self.timeline.set_marks([m.start for m in marks], [m.title for m in marks],
                                self.sheet.duration)
        self._show_current()
        self._update_controls()

    def _show_current(self) -> None:
        current = self.sheet.at(self._position) if self.sheet else None
        self.tree.blockSignals(True)
        for i in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(i)
            font = QFont(item.font(2))
            font.setBold(i == current)
            for column in range(4):
                item.setFont(column, font)
        self.tree.blockSignals(False)

    def _on_selected(self) -> None:
        index = self.selected()
        self.name.blockSignals(True)
        self.name.setText((self.sheet.marks()[index].title or "") if index is not None else "")
        self.name.blockSignals(False)
        self.timeline.set_selected(index)
        self._update_controls()

    def _on_item_edited(self, item: QTreeWidgetItem, column: int) -> None:
        if column != 2:
            return
        index = item.data(0, Qt.UserRole)
        text = item.text(2).strip()
        if text == utils.title_or_number(index, None):
            text = ""
        self._rename(index, text)

    def _update_controls(self) -> None:
        index = self.selected()
        movable = index is not None and index > 0
        for button in (self.move_button, self.earlier_button, self.later_button,
                       self.remove_button):
            button.setEnabled(movable)
        self.go_button.setEnabled(index is not None)
        self.name.setEnabled(index is not None)
        self.apply_names_button.setEnabled(bool(self.sheet and self.sheet.names()))
        upcoming = self.sheet.next_name() if self.sheet else None
        self.next_name.setText(f"Next: {upcoming}" if upcoming else "")
        self.play_button.setText("Play" if self._paused is not False else "Pause")
        self.save_button.setEnabled(self.active())

    # --- playback --------------------------------------------------------------

    def _ask_mpv(self):
        """On the poll's thread: where mpv is and whether it's paused, with
        the generation they were asked in."""
        generation, player = self._generation, self.player
        if player is None or not player.is_active():
            return generation, None, None
        return generation, player.position(), player.is_paused()

    def _mpv_answered(self, answer) -> None:
        generation, position, paused = answer
        if generation == self._generation:
            self._tick((position, paused))

    def _tick(self, answer=None) -> None:
        """Follow mpv. `answer` is (position, paused) as it said them,
        asked here if not given."""
        if self.player is None:
            return
        if self.player.take_exit() is not None:
            self._paused = True
            self._update_controls()
            return
        if not self.player.is_active():
            return
        if answer is None:
            answer = self.player.position(), self.player.is_paused()
        position, paused = answer
        if position is not None:
            self._position = position
        if paused is not None and paused != self._paused:
            self._paused = paused
            self._update_controls()
        self._show_position()

    def _show_position(self) -> None:
        seconds = self._position
        whole = int(seconds)
        tenths = int((seconds - whole) * 10)
        self.position_label.setText(
            f"{utils.format_seconds(whole)}.{tenths} / "
            f"{utils.format_seconds(self.sheet.duration if self.sheet else 0)}"
        )
        self.timeline.set_playhead(seconds)
        self._show_current()

    def _running(self) -> bool:
        return self.player is not None and self.player.is_active()

    def play_pause(self) -> None:
        if not self._running():
            self._start_player(paused=False)
        else:
            self._generation += 1
            self.player.toggle_pause()
            self._paused = not self._paused if self._paused is not None else False
        self._update_controls()

    def seek(self, seconds: float) -> None:
        if not self.active():
            return
        seconds = min(max(0.0, seconds), max(0.0, self.sheet.duration - 0.05))
        self._position = seconds
        self._generation += 1
        if self._running():
            self.player.seek_exact(seconds)
        else:
            self._start_player(paused=True)
        self._show_position()

    def step(self, delta: float) -> None:
        self.seek(self._position + delta)

    def frame(self, forward: bool) -> None:
        if self._running():
            self._generation += 1
            self.player.frame_step(forward)
            self._paused = True
            self._update_controls()

    def go_to_chapter(self, direction: int) -> None:
        if not self.active():
            return
        starts = [m.start for m in self.sheet.marks()]
        here = self._position
        if direction > 0:
            later = [s for s in starts if s > here + 0.5]
            if later:
                self.seek(later[0])
        else:
            # Back to where this chapter starts, or - just after it - the one before.
            earlier = [s for s in starts if s < here - 1.0]
            self.seek(earlier[-1] if earlier else 0.0)

    def play_from_selected(self) -> None:
        index = self.selected()
        if index is not None:
            self.seek(self.sheet.marks()[index].start)
            if self._paused and self._running():
                self._generation += 1
                self.player.set_paused(False)
                self._paused = False
                self._update_controls()

    # --- changing the chapters -----------------------------------------------------

    def _changed(self, select: int | None = None) -> None:
        self._dirty = True
        self._refresh(select=select)

    def insert_here(self) -> None:
        if not self.active():
            return
        try:
            index = self.sheet.mark(self._position)
        except marking.MarkError as exc:
            self.message.setText(f"Can't insert one there: {exc}.")
            return
        self.message.setText(
            f"Chapter {index + 1} starts at {utils.format_seconds(self._position)}."
        )
        self._changed(select=index)
        if not self.sheet.marks()[index].title:
            self.name.setFocus()
            self.name.selectAll()

    def _rename(self, index: int, text: str) -> None:
        if (self.sheet.marks()[index].title or "") == text:
            return
        self.sheet.rename(index, text)
        self._changed(select=index)

    def _commit_name(self) -> None:
        index = self.selected()
        if index is not None and self.sheet is not None:
            self._rename(index, self.name.text().strip())

    def _name_entered(self) -> None:
        self._commit_name()
        # Back to the keys, for the next song.
        self.timeline.setFocus()

    def remove_selected(self) -> None:
        index = self.selected()
        if index is None:
            return
        try:
            self.sheet.remove(index)
        except marking.MarkError as exc:
            self.message.setText(f"Can't remove that one: {exc}.")
            return
        self._changed(select=max(0, index - 1))

    def _move(self, index: int, seconds: float) -> None:
        try:
            index = self.sheet.move(index, seconds)
        except marking.MarkError as exc:
            self.message.setText(f"Can't move it there: {exc}.")
            self._refresh()
            return
        self._changed(select=index)

    def move_selected_here(self) -> None:
        index = self.selected()
        if index:
            self._move(index, self._position)

    def nudge_selected(self, delta: float) -> None:
        index = self.selected()
        if index:
            self._move(index, self.sheet.marks()[index].start + delta)

    def _on_boundary_moved(self, index: int, seconds: float) -> None:
        self._move(index, seconds)

    def _on_names_changed(self) -> None:
        if self.sheet is not None:
            self.sheet.set_names(self.names.toPlainText())
            self._update_controls()

    def apply_names(self) -> None:
        self._names_timer.stop()
        self.sheet.set_names(self.names.toPlainText())
        start = self.selected() or 0
        if self.sheet.apply_names_in_order(from_index=start):
            self._changed(select=start)

    # --- keys ----------------------------------------------------------------------

    def _typing(self) -> bool:
        focus = QApplication.focusWidget()
        if isinstance(focus, (QLineEdit, QPlainTextEdit, QAbstractSpinBox)):
            return True
        return self.tree.state() == QAbstractItemView.EditingState

    def eventFilter(self, watched, event) -> bool:
        if (event.type() != QEvent.KeyPress or not self.active() or not self.isVisible()
                or self._typing()):
            return False
        focus = QApplication.focusWidget()
        if focus is not None and focus is not self and not self.isAncestorOf(focus):
            return False  # a dialog of its own
        return self.handle_key(event.key(), event.modifiers())

    def handle_key(self, key, modifiers) -> bool:
        """What a key does here; returns whether it did anything."""
        shift = bool(modifiers & Qt.ShiftModifier)
        ctrl = bool(modifiers & Qt.ControlModifier)
        if key == Qt.Key_Space and not ctrl:
            self.play_pause()
        elif key in (Qt.Key_Left, Qt.Key_Right):
            size = LARGE if ctrl else MEDIUM if shift else SMALL
            self.step(size if key == Qt.Key_Right else -size)
        elif key == Qt.Key_Comma:
            self.frame(False)
        elif key == Qt.Key_Period:
            self.frame(True)
        elif key == Qt.Key_PageUp:
            self.go_to_chapter(-1)
        elif key == Qt.Key_PageDown:
            self.go_to_chapter(1)
        elif key in (Qt.Key_M, Qt.Key_Insert) and not ctrl:
            self.insert_here()
        elif key == Qt.Key_Delete:
            self.remove_selected()
        elif key in (Qt.Key_Return, Qt.Key_Enter):
            if self.selected() is not None:
                self.name.setFocus()
                self.name.selectAll()
        else:
            return False
        return True

