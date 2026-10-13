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
the playhead: the selected chapter's start, or - E - the end of the one
playing, which is the start of the next. J plays across the join, from a
few seconds before the selected chapter starts, to hear whether it's right.

The timeline zooms (the wheel, or + and -), since a whole concert in its
width is seconds to the pixel, and can show how loud the video is along
the way. That is measured only when asked for (Show Audio Levels): it
reads the whole file, and most libraries are on a network share that
nobody wants slowed down without having chosen to. Once measured it is
kept for the session.

The chapters share the video between them: each ends where the next
starts and the last where the video does, so moving a boundary takes from
one what it gives the other, never goes past the one either side, and
their lengths always add up to the video's. Clicking a chapter goes to
where it starts; double-clicking plays it from there. Names are typed in
the box under the list. The selection keeps up with the chapter playing,
except while a name is being typed.

Keys work wherever you aren't typing: Space plays and pauses, ←/→ step a
second (Shift five, Ctrl ten), , and . step a frame, Page Up/Down go to the
previous or next chapter, M inserts, E ends the chapter here, J plays
across the join, Delete removes, Enter names, Ctrl+Z undoes and
Ctrl+Shift+Z redoes.

Nothing is changed until Save. A chapter whose start and name are as they
were keeps where its name came from; if only names changed, a disc's own
chapters stay the disc's.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

from PySide6.QtCore import QEvent, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics
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
from mediabrowser.gui import audio_analysis
from mediabrowser.gui.mpv_poll import MpvPoll
from mediabrowser.gui.timeline import MIN_SPAN_SECONDS, Timeline
from mediabrowser.gui.video_surface import ELSEWHERE, VideoSurface, can_embed, elsewhere_text

# How often the editor asks mpv where playback is: fine enough for the
# playhead to move smoothly on the timeline.
POLL_MS = 150
# The steps the buttons and keys take, in seconds.
SMALL, MEDIUM, LARGE = 1.0, 5.0, 10.0
NUDGE = 1.0
# How long before a chapter's start hearing the join plays from.
JOIN_LEAD_SECONDS = 5.0
# A chapter within this of where one was is the same chapter.
SAME_START_SECONDS = 0.05

KEYS_HINT = (
    "Space play/pause · ←/→ 1s (Shift 5s, Ctrl 10s) · , and . one frame · "
    "Page Up/Down previous/next chapter · M insert a chapter · E end the chapter "
    "here · J hear the join · Enter name it · Delete remove it · Ctrl+Z undo · "
    "+ and − zoom the timeline (0 for all of it)"
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
        # The marks as they were opened, as last shown, and before each
        # change made since (and each one undone): see undo.
        self._opened: list[marking.Mark] = []
        self._shown: list[marking.Mark] = []
        self._undo: list[list[marking.Mark]] = []
        self._redo: list[list[marking.Mark]] = []
        # The chapter playback was last seen in: the selection goes with it.
        self._followed: int | None = None
        # Set to stop the audio being measured: the editor has closed.
        self._levels_cancel: threading.Event | None = None
        self._levels_job = None
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
        self.undo_button = QPushButton("Undo")
        self.undo_button.setToolTip("Take back the last change to the chapters (Ctrl+Z)")
        self.undo_button.clicked.connect(self.undo)
        self.redo_button = QPushButton("Redo")
        self.redo_button.setToolTip("Make the change just undone again (Ctrl+Shift+Z)")
        self.redo_button.clicked.connect(self.redo)
        for button in (self.undo_button, self.redo_button):
            button.setFocusPolicy(Qt.NoFocus)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self.request_close)
        self.save_button = QPushButton("Save Chapters")
        self.save_button.setObjectName("primaryButton")
        self.save_button.clicked.connect(self.save)
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.addWidget(self.back_button)
        top.addWidget(self.title, 1)
        top.addWidget(self.undo_button)
        top.addWidget(self.redo_button)
        top.addSpacing(12)
        top.addWidget(self.cancel_button)
        top.addWidget(self.save_button)

        # --- the picture and how to move through it
        self.surface = VideoSurface()
        self.elsewhere = QLabel(ELSEWHERE)
        self.elsewhere.setObjectName("hintLabel")
        self.elsewhere.setAlignment(Qt.AlignCenter)
        self.elsewhere.setWordWrap(True)
        self.elsewhere.hide()
        self.timeline = Timeline(min_gap=marking.MIN_GAP_SECONDS)
        self.timeline.seek_requested.connect(self.seek)
        self.timeline.boundary_moved.connect(self._on_boundary_moved)
        self.timeline.boundary_clicked.connect(self._select)
        self.timeline.view_changed.connect(self._update_zoom)
        self.levels_button = QPushButton("Show Audio Levels")
        self.levels_button.setFocusPolicy(Qt.NoFocus)
        self.levels_button.clicked.connect(self._levels_clicked)
        self.levels_note = QLabel("")
        self.levels_note.setObjectName("hintLabel")
        self.levels_note.setTextFormat(Qt.PlainText)
        self.zoom_out_button = QPushButton("−")
        self.zoom_out_button.setToolTip("Show more of the video on the timeline ( - )")
        self.zoom_out_button.clicked.connect(self.timeline.zoom_out)
        self.zoom_all_button = QPushButton("All")
        self.zoom_all_button.setToolTip("Show the whole video on the timeline ( 0 )")
        self.zoom_all_button.clicked.connect(self.timeline.show_all)
        self.zoom_in_button = QPushButton("＋")
        self.zoom_in_button.setToolTip(
            "Zoom the timeline in, to place a chapter exactly ( + , or the mouse "
            "wheel over it; Shift and the wheel moves along)"
        )
        self.zoom_in_button.clicked.connect(self.timeline.zoom_in)
        zoom_row = QHBoxLayout()
        zoom_row.setContentsMargins(0, 0, 0, 0)
        zoom_row.setSpacing(4)
        zoom_row.addWidget(self.levels_button)
        zoom_row.addWidget(self.levels_note, 1)
        zoom_row.addWidget(QLabel("Timeline:"))
        for button in (self.zoom_out_button, self.zoom_all_button, self.zoom_in_button):
            button.setObjectName("transportButton")
            button.setFocusPolicy(Qt.NoFocus)
            zoom_row.addWidget(button)

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
        self.end_button = QPushButton("⇥  End chapter here   (E)")
        self.end_button.setToolTip(
            "Move the start of the next chapter to the playhead: the one playing "
            "ends here"
        )
        self.end_button.setMinimumHeight(38)
        self.end_button.setFocusPolicy(Qt.NoFocus)
        self.end_button.clicked.connect(self.end_here)
        self.message = QLabel("")
        self.message.setObjectName("hintLabel")
        self.message.setTextFormat(Qt.PlainText)
        self.message.setWordWrap(True)
        insert_row = QHBoxLayout()
        insert_row.setContentsMargins(0, 0, 0, 0)
        insert_row.addWidget(self.insert_button, 1)
        insert_row.addWidget(self.end_button, 1)

        keys = QLabel(KEYS_HINT)
        keys.setObjectName("hintLabel")
        keys.setWordWrap(True)

        player_side = QWidget()
        left = QVBoxLayout(player_side)
        left.setContentsMargins(0, 0, 0, 0)
        left.setSpacing(6)
        left.addWidget(self.surface, 1)
        left.addWidget(self.elsewhere, 1)
        left.addWidget(self.timeline)
        left.addLayout(zoom_row)
        left.addLayout(transport)
        left.addLayout(insert_row)
        left.addWidget(self.message)
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
        # Names are typed in the box below; a click here is for getting about.
        self.tree.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tree.itemSelectionChanged.connect(self._on_selected)
        self.tree.itemClicked.connect(self._on_item_clicked)
        # Not itemActivated: on a desktop set to open things with one click,
        # that's a single click.
        self.tree.itemDoubleClicked.connect(self._on_item_double_clicked)
        header = self.tree.header()
        # The name takes what room there is, not the length after it.
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.Fixed)
        header.setSectionResizeMode(1, QHeaderView.Fixed)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        header.setSectionResizeMode(3, QHeaderView.Fixed)
        # Wide enough for a time in an hours-long video, in the bold the
        # chapter playing is shown in.
        bold = QFont(self.tree.font())
        bold.setBold(True)
        time_width = QFontMetrics(bold).horizontalAdvance("0:00:00.0") + 20
        self.tree.setColumnWidth(0, 36)
        self.tree.setColumnWidth(1, time_width)
        self.tree.setColumnWidth(3, time_width)

        self.total = QLabel("")
        self.total.setObjectName("hintLabel")
        self.total.setTextFormat(Qt.PlainText)
        self.total.setAlignment(Qt.AlignRight)

        self.name = QLineEdit()
        self.name.setPlaceholderText("Name of the selected chapter…")
        self.name.returnPressed.connect(self._name_entered)
        self.name.editingFinished.connect(self._commit_name)
        name_row = QHBoxLayout()
        name_row.setContentsMargins(0, 0, 0, 0)
        name_row.addWidget(QLabel("Name:"))
        name_row.addWidget(self.name, 1)

        self.move_button = QPushButton("Start at Playhead")
        self.move_button.setToolTip("Start the selected chapter where the playhead is")
        self.move_button.clicked.connect(self.move_selected_here)
        self.join_button = QPushButton("Hear the Join")
        self.join_button.setToolTip(
            f"Play from {JOIN_LEAD_SECONDS:g}s before the selected chapter starts, to "
            "hear whether it starts in the right place ( J )"
        )
        self.join_button.clicked.connect(self.hear_join)
        self.earlier_button = QPushButton(f"−{NUDGE:g}s")
        self.earlier_button.setToolTip("Start the selected chapter a second sooner")
        self.earlier_button.clicked.connect(lambda: self.nudge_selected(-NUDGE))
        self.later_button = QPushButton(f"+{NUDGE:g}s")
        self.later_button.setToolTip("Start the selected chapter a second later")
        self.later_button.clicked.connect(lambda: self.nudge_selected(NUDGE))
        self.remove_button = QPushButton("Remove")
        self.remove_button.setToolTip("Take the selected chapter away (Delete)")
        self.remove_button.clicked.connect(self.remove_selected)
        # Two rows, not one: in one, these five set the narrowest the editor
        # can be - and the editor shares the main area with every other page,
        # so it set the narrowest the whole window could be, editing or not.
        mark_rows = []
        for row_buttons in ((self.move_button, self.join_button),
                            (self.earlier_button, self.later_button, self.remove_button)):
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(4)
            for button in row_buttons:
                button.setFocusPolicy(Qt.NoFocus)
                row.addWidget(button)
            row.addStretch(1)
            mark_rows.append(row)

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
        right.addWidget(self.total)
        right.addLayout(name_row)
        for row in mark_rows:
            right.addLayout(row)
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
        # Two fifths to the chapters to begin with: left to what each side
        # asks for, the list is too narrow to read a name in.
        self.splitter.setSizes([600, 400])

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
        self._opened = self.sheet.marks()
        self._undo, self._redo = [], []
        self.timeline.set_playhead(None)
        self.timeline.show_all()
        self.title.setText(f"Manual Edit · {video['display_name']}")
        self.message.setText("")
        self.names.blockSignals(True)
        self.names.clear()
        self.names.blockSignals(False)
        self._refresh(select=self.sheet.at(self._position))
        self._update_zoom()
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
        self._offer_levels()

    # --- how loud it is, along the timeline --------------------------------------

    def _offer_levels(self) -> None:
        """Show how loud the video is along the timeline if that has been
        measured this session; if not, offer to. Never measured unasked:
        it reads the whole file, wherever the file is."""
        self._stop_measuring()
        self.timeline.set_levels(None)
        self.levels_note.setText("")
        levels = (audio_analysis.cached(self.video)
                  if audio_analysis.can_analyse(self.video) else None)
        if levels is not None:
            self.timeline.set_levels(levels)
        self._show_levels_button()

    def _show_levels_button(self) -> None:
        measuring = self._levels_cancel is not None
        self.levels_button.setText("Stop Measuring" if measuring else "Show Audio Levels")
        self.levels_button.setToolTip(
            "Stop reading the file; nothing is shown" if measuring else
            "Draw how loud the video is along the timeline - where the bass drops "
            "away is usually between songs. Reads the whole file's audio once: a few "
            "seconds to a minute, longer from a network share, and kept until the "
            "app closes."
        )
        # Nothing to offer for a disc (see audio_analysis), or once they're there.
        self.levels_button.setVisible(
            self.active() and audio_analysis.can_analyse(self.video)
            and not self.timeline.has_levels()
        )

    def _levels_clicked(self) -> None:
        if self._levels_cancel is not None:
            self._stop_measuring()
            self.levels_note.setText("")
            self._show_levels_button()
        else:
            self.measure_audio()

    def _stop_measuring(self) -> None:
        if self._levels_cancel is not None:
            self._levels_cancel.set()
        self._levels_cancel = self._levels_job = None

    def measure_audio(self) -> None:
        """Measure the video's audio for the timeline, off the GUI thread:
        the video can be worked on meanwhile."""
        if (not self.active() or self._levels_cancel is not None
                or not audio_analysis.can_analyse(self.video)):
            return
        cancel = self._levels_cancel = threading.Event()

        def wanted() -> bool:
            # Not once it has been stopped, or the editor has closed or
            # opened on another video.
            return self._levels_cancel is cancel

        def progress(percent: int) -> None:
            if wanted():
                self.levels_note.setText(f"Measuring the audio… {percent}%")

        def done(levels) -> None:
            if wanted():
                self._levels_cancel = self._levels_job = None
                self.levels_note.setText("")
                self.timeline.set_levels(levels)
                self._show_levels_button()

        def failed(_message: str) -> None:
            if wanted():
                self._levels_cancel = self._levels_job = None
                self.levels_note.setText("The audio couldn't be measured.")
                self._show_levels_button()

        self.levels_note.setText("Measuring the audio…")
        self._show_levels_button()
        self._levels_job = audio_analysis.start(
            self, self.video, cancel, on_progress=progress, on_done=done, on_failed=failed,
        )

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
        self._stop_measuring()
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
        times = self.sheet.tenths()
        self.tree.blockSignals(True)
        self.tree.clear()
        for i, mark in enumerate(marks):
            start, length = times[i]
            item = QTreeWidgetItem([
                str(i + 1), utils.format_tenths(start / 10), mark.title or "",
                utils.format_tenths(length / 10),
            ])
            item.setData(0, Qt.UserRole, i)
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
        self.total.setText(
            f"{len(marks)} chapter{'' if len(marks) == 1 else 's'} · "
            f"{utils.format_tenths(sum(length for _start, length in times) / 10)} in all"
        )
        self._shown = marks
        # A chapter put in or taken out moves the rest along: that isn't
        # playback going from one to another.
        self._followed = self.sheet.at(self._position)
        self._show_current()
        self._update_controls()

    def _show_current(self) -> None:
        current = self.sheet.at(self._position) if self.sheet else None
        if current is not None and current != self._followed:
            # Playback has gone into another chapter: the selection goes
            # with it, so what the buttons act on is what's playing - but
            # not from under a name being typed.
            self._followed = current
            if not self.name.hasFocus():
                self._select(current)
        self.tree.blockSignals(True)
        for i in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(i)
            font = QFont(item.font(2))
            font.setBold(i == current)
            for column in range(4):
                item.setFont(column, font)
        self.tree.blockSignals(False)
        # The last chapter ends where the video does, and nowhere else.
        self.end_button.setEnabled(current is not None and current + 1 < len(self.sheet))

    def _on_selected(self) -> None:
        index = self.selected()
        self.name.blockSignals(True)
        self.name.setText((self.sheet.marks()[index].title or "") if index is not None else "")
        self.name.blockSignals(False)
        self.timeline.set_selected(index)
        self._update_controls()

    def _on_item_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        """Go to where the chapter starts, playing or paused as it was."""
        self.seek(self.sheet.marks()[item.data(0, Qt.UserRole)].start)

    def _on_item_double_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        self._select(item.data(0, Qt.UserRole))
        self.play_from_selected()

    def _update_controls(self) -> None:
        index = self.selected()
        movable = index is not None and index > 0
        for button in (self.move_button, self.earlier_button, self.later_button,
                       self.remove_button):
            button.setEnabled(movable)
        self.join_button.setEnabled(index is not None)
        self.name.setEnabled(index is not None)
        self.undo_button.setEnabled(bool(self._undo))
        self.redo_button.setEnabled(bool(self._redo))
        self.apply_names_button.setEnabled(bool(self.sheet and self.sheet.names()))
        upcoming = self.sheet.next_name() if self.sheet else None
        self.next_name.setText(f"Next: {upcoming}" if upcoming else "")
        self.play_button.setText("Play" if self._paused is not False else "Pause")
        self.save_button.setEnabled(self.active())

    def _update_zoom(self) -> None:
        zoomed = self.timeline.zoomed()
        self.zoom_out_button.setEnabled(zoomed)
        self.zoom_all_button.setEnabled(zoomed)
        _start, span = self.timeline.view()
        self.zoom_in_button.setEnabled(
            span > min(MIN_SPAN_SECONDS, self.timeline.duration)
        )

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
        # To the tenth, as the list has the chapters: put one here and it
        # says the same time.
        self.position_label.setText(
            f"{utils.format_tenths(seconds)} / "
            f"{utils.format_tenths(self.sheet.duration if self.sheet else 0)}"
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
        self.timeline.reveal(seconds)
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

    def _play_from(self, seconds: float) -> None:
        self.seek(seconds)
        if self._paused and self._running():
            self._generation += 1
            self.player.set_paused(False)
            self._paused = False
            self._update_controls()

    def play_from_selected(self) -> None:
        index = self.selected()
        if index is not None:
            self._play_from(self.sheet.marks()[index].start)

    def hear_join(self) -> None:
        """Play across where the selected chapter starts, from a few
        seconds before it: the end of one song and the start of the next."""
        index = self.selected()
        if index is not None:
            self._play_from(self.sheet.marks()[index].start - JOIN_LEAD_SECONDS)

    # --- changing the chapters -----------------------------------------------------

    def _changed(self, select: int | None = None) -> None:
        """The sheet has just been changed: what it was before is what was
        last shown, and is what undo goes back to."""
        self._undo.append(self._shown)
        self._redo.clear()
        self._dirty = self.sheet.marks() != self._opened
        self._refresh(select=select)

    def _step_back(self, taken: list, given: list, said: str) -> None:
        if not self.active() or not taken:
            return
        before = self.sheet.marks()
        given.append(before)
        self.sheet.restore(taken.pop())
        after = self.sheet.marks()
        self._dirty = after != self._opened
        # On the chapter that changed: the first that isn't as it was.
        changed = next((i for i, (a, b) in enumerate(zip(before, after, strict=False))
                        if a != b), min(len(before), len(after)))
        self.message.setText(said)
        self._refresh(select=min(changed, len(after) - 1))

    def undo(self) -> None:
        """Take back the last change to the chapters - one put in, taken
        out, moved or named."""
        self._step_back(self._undo, self._redo, "Undone.")

    def redo(self) -> None:
        self._step_back(self._redo, self._undo, "Redone.")

    def insert_here(self) -> None:
        if not self.active():
            return
        try:
            index = self.sheet.mark(self._position)
        except marking.MarkError as exc:
            self.message.setText(f"Can't insert one there: {exc}.")
            return
        self.message.setText(
            f"Chapter {index + 1} starts at {utils.format_tenths(self._position)}."
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
        # Back to the keys, for the next song - once this Enter is over. A
        # line edit doesn't keep the key, so it goes on up to its parents,
        # past the key filter each time: with the name box already left,
        # the filter took it for the Enter that names a chapter, and put
        # the keyboard straight back in the box.
        QTimer.singleShot(0, self.timeline.setFocus)

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
        self.message.setText(
            f"Chapter {index + 1} starts at "
            f"{utils.format_tenths(self.sheet.marks()[index].start)}."
        )
        self._changed(select=index)

    def move_selected_here(self) -> None:
        index = self.selected()
        if index:
            self._move(index, self._position)

    def end_here(self) -> None:
        """End the chapter playing at the playhead: the next one starts
        here instead of where it did."""
        if not self.active():
            return
        following = self.sheet.at(self._position) + 1
        if following >= len(self.sheet):
            self.message.setText(
                "The last chapter ends where the video does. Insert a chapter "
                "here to start another."
            )
            return
        self._move(following, self._position)

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
        return isinstance(focus, (QLineEdit, QPlainTextEdit, QAbstractSpinBox))

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
        elif key == Qt.Key_E and not ctrl:
            self.end_here()
        elif key == Qt.Key_J and not ctrl:
            self.hear_join()
        elif key == Qt.Key_Z and ctrl:
            self.redo() if shift else self.undo()
        elif key == Qt.Key_Y and ctrl:
            self.redo()
        elif key in (Qt.Key_Plus, Qt.Key_Equal) and not ctrl:
            self.timeline.zoom_in()
        elif key == Qt.Key_Minus and not ctrl:
            self.timeline.zoom_out()
        elif key == Qt.Key_0 and not ctrl:
            self.timeline.show_all()
        elif key == Qt.Key_Delete:
            self.remove_selected()
        elif key in (Qt.Key_Return, Qt.Key_Enter):
            if self.selected() is not None:
                self.name.setFocus()
                self.name.selectAll()
        else:
            return False
        return True

