# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Detect Chapters: one place to find a video's chapters and their names.

Just Figure It Out, at the top, does what Identify Library does for one
video with someone watching: the disc's menu, MusicBrainz, the audio and
the AI, as many of them as this video turns out to need (core.autoname).

Otherwise it's done a step at a time. A Blu-ray title's own
scene-selection menu names its songs, and each button says which chapter
it plays (core.menu_chapters), so Extract Blu-ray Menu reads that and a
model transcribes each button.

The Tracklist tab is three steps. 1: get a tracklist, whichever way there
is - look the show up on MusicBrainz, paste one, or go without, and a
video file's chapters are found from the audio. 2: make the chapters from
it. 3, if it's wanted: have the AI check. What the video already has -
chapters wanting only names, or nothing at all - is said at the top, as it
decides what a tracklist does. Whatever tracklist there is, core.methods
picks the best way to use
it - naming the existing chapters, placing them by the lengths (then
splitting each song's intro off using the audio), or detecting them from
the audio and lighting. The choice can be overridden. The audio and
lighting are measured only when a method needs them, once, and cached for
the session.

Nothing is applied without Apply, and MusicBrainz is only searched when
asked: it rate-limits hard. A private video is never sent to MusicBrainz
or the AI.
"""

from __future__ import annotations

import threading

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QButtonGroup,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from mediabrowser.core import (
    ai,
    ai_chapters,
    autoname,
    chaptergen,
    library,
    menu_chapters,
    methods,
    musicbrainz,
    naming,
    privacy,
    proposal,
    store,
    utils,
)
from mediabrowser.gui import audio_analysis
from mediabrowser.gui.worker import run_job

TAB_MENU, TAB_TRACKLIST = range(2)
MENU_TAB = "Extract Blu-ray Menu"
MENU_TAB_OFF = "Extract Blu-ray Menu (not a Blu-ray)"

METHOD_LABELS = {
    methods.NAME: "Name the existing chapters",
    methods.LENGTHS: "Place chapters by the track lengths",
    methods.DETECT: "Detect chapters from the audio",
}
METHOD_HINTS = {
    methods.NAME: "Keeps where the chapters are and puts the tracklist's names on them.",
    methods.LENGTHS: (
        "Each song starts where the lengths before it add up to - within a second or "
        "two when the tracks are the whole video."
    ),
    methods.DETECT: (
        "Finds where the music stops between songs. With a tracklist it knows how many "
        "songs to find and names them in order; the result will want checking."
    ),
}

NAME_HEADERS = ["Chapter", "Duration", "Proposed Title", "Notes"]
PLACE_HEADERS = ["Chapter", "Starts", "Title", "Length"]

FLAG_COLOURS = {
    proposal.FLAG_POSITION: "#e0af68",
    proposal.FLAG_NO_MATCH: "#565c69",
    proposal.FLAG_UNUSED: "#565c69",
}

PASTE_INSTRUCTIONS = (
    "One track per line, from a sleeve, setlist.fm or anywhere. A leading track "
    "number and a duration anywhere on the line are both understood - \"3. "
    "Megitsune 5:16\". Durations let the lengths place the chapters; without them "
    "the titles still name them, and tell detection how many songs to find."
)
LOOK_UP_HINT = (
    "Most concerts are on MusicBrainz. Check the search - add the artist or the "
    "album if it's missing - then press Search. It limits how often it can be asked."
)
NO_TRACKLIST_CHAPTERED = (
    "No tracklist anywhere? The chapters stay where they are, unnamed: step 3's AI "
    "can name them, or name them by hand in Manual Edit."
)
NO_TRACKLIST_ONE_PIECE = (
    "No tracklist anywhere? Leave both empty: the chapters are then found from where "
    "the music stops between songs, and step 3's AI - or Manual Edit - can name them."
)
NO_TRACKLIST_DISC = (
    "No tracklist anywhere? Step 3's AI can look at the video, or mark the songs by "
    "hand in Manual Edit."
)
AI_OPTIONAL = (
    "Not needed when the tracklist fits. Worth it with no tracklist, when names are "
    "missing or in another script, or to check where the chapters start."
)
PRIVATE_NOTE = (
    "This video is private: nothing about it goes to MusicBrainz or the AI. Paste a "
    "tracklist, or detect the chapters from the audio."
)
MUSICBRAINZ_OFF_NOTE = (
    privacy.OFF[privacy.MUSICBRAINZ] + " Paste a tracklist, or detect the chapters "
    "from the audio."
)

FIGURE_NOTE = (
    "Uses the AI: reads the disc's menu if it has one, looks the show up on "
    "MusicBrainz, measures the audio and has {model} look at the video - whatever "
    "this video turns out to need. A few cents; nothing changes until Apply."
)
HINTS_TIP = "Tell it what you know about this video: what it's called, how many songs, the setlist"
HINTS_NOTE = (
    "All optional. What it's known as is searched on MusicBrainz first; a release "
    "must have that many songs, and most of the setlist's; the audio is split into "
    "that many; and the AI is told all of it. A setlist with a song per chapter "
    "names them if nothing else does."
)
FIGURE_SOURCES = {
    "menu": "disc menu",
    "musicbrainz": "MusicBrainz",
    "ai": "AI",
    "embedded": "the file",
    "manual": "by hand",
    "filename": "file name",
}

MENU_HINT = (
    "A Blu-ray's scene-selection menu is its makers' own list of the songs, and "
    "each button plays exactly one chapter. This works out which chapter every "
    "button plays from the disc's navigation, draws the menu as a player would, "
    "and has the AI read each button's text - no matching, no guessing. Uses the "
    "AI: a cent or two each time the menu is read; nothing changes until Apply."
)
MENU_NOT_A_DISC = (
    "Only a Blu-ray folder keeps its menus; a rip to a single file leaves them behind."
)
MENU_PICTURE_HEIGHT = 220

# The dialog's first size, at most this much of the screen.
DEFAULT_SIZE = (1000, 1120)
MIN_WIDTH = 640
SCREEN_FRACTION = 0.9
# What the result table keeps however far the steps above are opened.
RESULT_MIN_HEIGHT = 240

AI_HEADERS = ["Chapter", "Starts", "Title", "Notes"]
CONFIDENCE_COLOURS = {"medium": "#e0af68", "low": "#f7768e"}

# Typing a pasted tracklist or a song count updates the preview on a pause.
TYPING_DEBOUNCE_MS = 400

# How far a tracklist's total may be from the video's length before placing
# by lengths warns that the wrong discs are probably chosen.
TOTAL_WARNING_FRACTION = 0.03
TOTAL_WARNING_MIN_SECONDS = 30.0


def situation_text(video, can_analyse: bool) -> str:
    """What this video already has, and so what a tracklist does for it:
    chapters wanting only names, estimates that may be placed afresh, or
    one piece where the chapters must be found too."""
    chapters = video["chapters"]
    total = len(chapters)
    named = naming.status(video, marked=False).named
    if total <= 1:
        found = ("Without lengths, or without a tracklist, they're found from where the "
                 "music stops between songs." if can_analyse else
                 "Without lengths, the AI or Manual Edit can mark them.")
        return ("This video is in one piece: it has no chapters yet, so they have to be "
                "found as well as named. A tracklist with lengths places them. " + found)
    if video.get("chapter_origin") == library.ORIGIN_ESTIMATED and not named:
        return (f"This video's {total} chapters were only estimated from the audio, and "
                "nothing is named yet. A tracklist with lengths can place them afresh; "
                "otherwise it names them where they are.")
    if named == total:
        return (f"This video's {total} chapters are all named already. A tracklist would "
                "rename them where they are.")
    have = f"{named} of them named" if named else "none of them named"
    return (f"This video already has {total} chapters, {have}. They're where the disc or "
            "file put them, so they only want names: a tracklist names them where they "
            "are, and nothing is moved.")


class ChaptersDialog(QDialog):
    def __init__(self, parent, video, library_root=None, private: bool = False) -> None:
        super().__init__(parent)
        self.setWindowTitle("Detect Chapters")
        self.setModal(True)
        self.setMinimumWidth(MIN_WIDTH)
        screen = (parent.screen() if parent is not None else QApplication.primaryScreen())
        room = screen.availableGeometry() if screen is not None else None
        width, height = DEFAULT_SIZE
        if room is not None:
            width = min(width, int(room.width() * SCREEN_FRACTION))
            height = min(height, int(room.height() * SCREEN_FRACTION))
        self.resize(width, height)

        self.video = video
        self._library_root = library_root
        # Nothing about a private video goes to MusicBrainz or the AI.
        self._private = private
        self._can_analyse = audio_analysis.can_analyse(video)
        self._jobs: list = []
        self._cancel = threading.Event()

        # The tracklist, wherever it came from.
        self._mb_media: list[dict] = []
        self._mb_tracks: list[dict] | None = None
        self._release_id: str | None = None
        self._paste_tracks: list[dict] = []

        # Measurements, once taken.
        self._levels = audio_analysis.cached(video) if self._can_analyse else None
        self._light = None
        self._levels_job = None
        self._light_job = None
        self._analysis_error: str | None = None

        self._method: str | None = None
        self._method_picked = False
        self._result: tuple[str, object] | None = None

        # The model's answer, while it is what the table shows.
        self._ai_settings = ai.load_settings()
        self._ai_result: ai_chapters.Result | None = None
        self._ai_job = None
        # Ask AI was pressed before the audio was measured: ask once it is.
        self._ai_waiting = False

        # The disc's menu, once read, and the model's reading of it.
        self._disc_menu: menu_chapters.DiscMenu | None = None
        self._menu_result: ai_chapters.Result | None = None
        self._menu_job = None

        # Just Figure It Out's answer, while it is what the table shows; and
        # the last one's report, which stays readable after.
        self._figured: autoname.Outcome | None = None
        self._report: str = ""
        self._figure_job = None

        self._build()
        self.refresh()

    # --- construction ----------------------------------------------------

    def _build(self) -> None:
        # --- the whole job, at once
        self.figure_button = QPushButton("Just Figure It Out")
        self.figure_button.setObjectName("primaryButton")
        self.figure_button.clicked.connect(self.figure_it_out)
        self.figure_note = QLabel()
        self.figure_note.setObjectName("hintLabel")
        self.figure_note.setWordWrap(True)
        self.figure_note.setMinimumWidth(1)
        self.hints_toggle = QPushButton("Hints…")
        self.hints_toggle.setCheckable(True)
        self.hints_toggle.setToolTip(HINTS_TIP)
        self.hints_toggle.toggled.connect(self._show_hints)
        figure_buttons = QVBoxLayout()
        figure_buttons.setContentsMargins(0, 0, 0, 0)
        figure_buttons.setSpacing(6)
        self.report_button = QPushButton("Report…")
        self.report_button.setToolTip(
            "What Just Figure It Out did, step by step, and why it decided as it did"
        )
        self.report_button.setEnabled(False)
        self.report_button.clicked.connect(self.show_report)
        figure_buttons.addWidget(self.figure_button)
        figure_buttons.addWidget(self.hints_toggle)
        figure_buttons.addWidget(self.report_button)
        figure_row = QHBoxLayout()
        figure_row.setContentsMargins(0, 0, 0, 0)
        figure_row.setSpacing(10)
        figure_row.addLayout(figure_buttons)
        figure_row.addWidget(self.figure_note, 1, Qt.AlignTop)
        self.hints_area = self._build_hints()
        step_caption = QLabel("Or do it yourself")
        step_caption.setObjectName("sectionCaption")
        self.situation = QLabel(situation_text(self.video, self._can_analyse))
        self.situation.setObjectName("situationLabel")
        self.situation.setWordWrap(True)
        self.situation.setMinimumWidth(1)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_menu_tab(), MENU_TAB)
        self.tabs.addTab(self._build_tracklist_tab(), "Tracklist")
        is_disc = self.video.get("type") == "bluray"
        if not is_disc:
            # Switched off, and saying so: a greyed tab alone reads as a bug.
            self.tabs.setTabEnabled(TAB_MENU, False)
            self.tabs.setTabText(TAB_MENU, MENU_TAB_OFF)
            self.tabs.setTabToolTip(TAB_MENU, MENU_NOT_A_DISC)
        elif self._private:
            self.menu_button.setEnabled(False)
            self.menu_status.setText(
                "This title is private: the menu is read by the AI, which isn't used for it."
            )
        self.tabs.setCurrentIndex(TAB_MENU if is_disc and not self._private else TAB_TRACKLIST)
        self.tabs.currentChanged.connect(lambda _index: self.refresh())

        # --- how
        self.method_group = QButtonGroup(self)
        method_row = QHBoxLayout()
        method_row.setContentsMargins(0, 0, 0, 0)
        method_row.setSpacing(12)
        self.method_radios: dict[str, QRadioButton] = {}
        for method in (methods.NAME, methods.LENGTHS, methods.DETECT):
            radio = QRadioButton(METHOD_LABELS[method])
            radio.setToolTip(METHOD_HINTS[method])
            radio.clicked.connect(lambda _checked=False, m=method: self._pick_method(m))
            self.method_group.addButton(radio)
            self.method_radios[method] = radio
            method_row.addWidget(radio)
        method_row.addStretch(1)

        self.snap_check = QCheckBox(
            f"Line boundaries up with quiet moments (±{chaptergen.SNAP_RADIUS_SECONDS:g}s)"
        )
        self.intro_check = QCheckBox("Split off each song's intro")
        self.intro_check.setToolTip(
            "Where a track starts well before the band does - a story video, an "
            "entrance, an intermission - the audio finds where the song proper starts, "
            "and the part before becomes \"Intro to …\"."
        )
        self.intro_check.setChecked(True)
        self.light_check = QCheckBox("Also check the stage lighting")
        self.light_check.setToolTip(
            "Looks at a few hundred frames around the quiet stretches, which adds under "
            "a minute. A quiet moment where the lights come up is a breakdown within a "
            "song, not a gap between two."
        )
        self.light_check.setChecked(True)
        self.count = QSpinBox()
        self.count.setRange(0, 200)
        self.count.setSpecialValueText("Work it out")
        self.count.setToolTip("How many songs this video has")
        self.count_label = QLabel("Songs:")
        for check in (self.snap_check, self.intro_check, self.light_check):
            if self._can_analyse:
                check.toggled.connect(lambda _on: self.refresh())
            else:
                check.setEnabled(False)
                check.setToolTip("Only for video files, not Blu-ray folders.")
        self.snap_check.setChecked(False)
        self._count_timer = QTimer(self)
        self._count_timer.setSingleShot(True)
        self._count_timer.setInterval(TYPING_DEBOUNCE_MS)
        self._count_timer.timeout.connect(self.refresh)
        self.count.valueChanged.connect(self._count_timer.start)

        options_row = QHBoxLayout()
        options_row.setContentsMargins(0, 0, 0, 0)
        options_row.setSpacing(12)
        for widget in (self.snap_check, self.intro_check, self.light_check,
                       self.count_label, self.count):
            options_row.addWidget(widget)
        options_row.addStretch(1)

        self.method_hint = QLabel()
        self.method_hint.setObjectName("hintLabel")
        self.method_hint.setWordWrap(True)

        # --- the AI
        self.ai_frames_check = QCheckBox("Show it frames")
        self.ai_frames_check.setToolTip(
            "Sends a frame or two from just after each chapter's start, where a "
            "concert Blu-ray shows the song's title. Without them the model goes on "
            "the names, times and its knowledge of the setlist alone."
        )
        self.ai_frames_check.setChecked(True)
        self.ai_translate_check = QCheckBox("Romanised titles")
        self.ai_translate_check.setToolTip(
            "Song titles as they're officially known in English-language releases - "
            "usually romanised (Iine!, Megitsune), or the official English title where "
            "there is one; never a translation of the meaning. The original is kept on the "
            "chapter so a search in either finds it."
        )
        self.ai_translate_check.setChecked(True)
        self.ai_online_check = QCheckBox("Look the show up online")
        self.ai_online_check.setToolTip(
            "Nano-GPT searches the web for the model first, so it reads the show's "
            "setlist from setlist.fm rather than guessing it. Costs a little more; "
            "worth it whenever the frames don't show the titles."
        )
        self.ai_online_check.setChecked(True)
        self._online_tip = self.ai_online_check.toolTip()
        self._update_online_check()
        self.ask_ai_button = QPushButton("Ask AI")
        self.ask_ai_button.setObjectName("primaryButton")
        self.ask_ai_button.clicked.connect(self.ask_ai)
        self.ai_settings_button = QPushButton("Settings…")
        self.ai_settings_button.setToolTip("The AI's key and model, and what may be sent")
        self.ai_settings_button.clicked.connect(self._open_settings)
        for check in (self.ai_frames_check, self.ai_translate_check, self.ai_online_check):
            check.toggled.connect(lambda _on: self._update_ai_controls())
        ai_row = QHBoxLayout()
        ai_row.setContentsMargins(0, 0, 0, 0)
        ai_row.setSpacing(12)
        ai_row.addWidget(self.ai_frames_check)
        ai_row.addWidget(self.ai_online_check)
        ai_row.addWidget(self.ai_translate_check)
        ai_row.addStretch(1)
        self.ai_hint = QLabel()
        self.ai_hint.setObjectName("hintLabel")
        self.ai_hint.setTextFormat(Qt.PlainText)
        self.ai_hint.setWordWrap(True)
        # A long hint must not set the dialog's minimum width.
        self.ai_hint.setMinimumWidth(1)
        ai_ask_row = QHBoxLayout()
        ai_ask_row.setContentsMargins(0, 0, 0, 0)
        ai_ask_row.setSpacing(6)
        ai_ask_row.addWidget(self.ai_hint, 1)
        ai_ask_row.addWidget(self.ai_settings_button, 0, Qt.AlignTop)
        ai_ask_row.addWidget(self.ask_ai_button, 0, Qt.AlignTop)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setTextVisible(True)
        self.progress.hide()

        # --- result
        self.table_caption = QLabel("Proposed chapters")
        self.table_caption.setObjectName("sectionCaption")
        self.tree = QTreeWidget()
        self.tree.setColumnCount(4)
        self.tree.setHeaderLabels(NAME_HEADERS)
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(True)
        self.tree.setSelectionMode(QAbstractItemView.NoSelection)
        self.tree.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.tree.setMinimumHeight(200)
        header = self.tree.header()
        header.setSectionResizeMode(0, QHeaderView.Fixed)
        header.setSectionResizeMode(1, QHeaderView.Fixed)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        header.setSectionResizeMode(3, QHeaderView.Fixed)
        self.tree.setColumnWidth(0, 80)
        self.tree.setColumnWidth(1, 80)
        self.tree.setColumnWidth(3, 170)

        self.status = QLabel("")
        self.status.setObjectName("hintLabel")
        self.status.setTextFormat(Qt.PlainText)
        self.status.setWordWrap(True)

        buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
        self.apply_button = buttons.addButton("Apply", QDialogButtonBox.AcceptRole)
        self.apply_button.setObjectName("primaryButton")
        self.apply_button.setEnabled(False)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        how = QLabel("2 · Make the chapters from it")
        how.setObjectName("stepCaption")
        self.ai_caption = QLabel("3 · Optional: have the AI check")
        self.ai_caption.setObjectName("stepCaption")
        self.ai_optional = QLabel(AI_OPTIONAL)
        self.ai_optional.setObjectName("hintLabel")
        self.ai_optional.setWordWrap(True)
        self.ai_optional.setMinimumWidth(1)

        # Naming and placing is the Tracklist tab's; the menu's reading is
        # the disc's own, with nothing to choose.
        self.how_section = QWidget()
        how_layout = QVBoxLayout(self.how_section)
        how_layout.setContentsMargins(0, 0, 0, 0)
        how_layout.setSpacing(8)
        how_layout.addWidget(how)
        how_layout.addLayout(method_row)
        how_layout.addLayout(options_row)
        how_layout.addWidget(self.method_hint)

        # The steps scroll rather than squeeze: squeezed, wrapped hints
        # overprint one another. The result below keeps its own share, and
        # the line between them can be dragged.
        steps = QWidget()
        steps_layout = QVBoxLayout(steps)
        steps_layout.setContentsMargins(0, 0, 4, 0)
        steps_layout.setSpacing(8)
        steps_layout.addLayout(figure_row)
        steps_layout.addWidget(self.hints_area)
        steps_layout.addWidget(step_caption)
        steps_layout.addWidget(self.situation)
        steps_layout.addWidget(self.tabs)
        steps_layout.addWidget(self.how_section)
        steps_layout.addWidget(self.ai_caption)
        steps_layout.addWidget(self.ai_optional)
        steps_layout.addLayout(ai_row)
        steps_layout.addLayout(ai_ask_row)
        steps_layout.addStretch(1)
        self.steps_area = QScrollArea()
        self.steps_area.setWidgetResizable(True)
        self.steps_area.setFrameShape(QFrame.NoFrame)
        self.steps_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.steps_area.setWidget(steps)

        result = QWidget()
        result_layout = QVBoxLayout(result)
        result_layout.setContentsMargins(0, 0, 0, 0)
        result_layout.setSpacing(8)
        result_layout.addWidget(self.progress)
        self.clear_button = QPushButton("Clear")
        self.clear_button.setToolTip(
            "Start again: drop what's proposed here - the tracklist, the method, and any "
            "answer from Just Figure It Out or the AI"
        )
        self.clear_button.clicked.connect(self.clear_proposal)
        caption_row = QHBoxLayout()
        caption_row.setContentsMargins(0, 0, 0, 0)
        caption_row.addWidget(self.table_caption, 1)
        caption_row.addWidget(self.clear_button)
        result_layout.addLayout(caption_row)
        result_layout.addWidget(self.tree, 1)
        result_layout.addWidget(self.status)
        result.setMinimumHeight(RESULT_MIN_HEIGHT)

        self.splitter = QSplitter(Qt.Vertical)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.addWidget(self.steps_area)
        self.splitter.addWidget(result)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)
        layout.addWidget(self.splitter, 1)
        layout.addWidget(buttons)
        QTimer.singleShot(0, self._fit_steps)

    def _fit_width(self) -> None:
        """Scrolling hides how wide the steps need to be; the window keeps
        to it, as it is now (which options show depends on the method)."""
        scrollbar = self.steps_area.verticalScrollBar().sizeHint().width()
        needed = self.steps_area.widget().minimumSizeHint().width() + scrollbar + 24
        self.setMinimumWidth(max(MIN_WIDTH, needed))

    def _fit_steps(self) -> None:
        """Give the steps all the room they ask for that the result can
        spare - again whenever they grow (results found, a tracklist
        pasted)."""
        total = sum(self.splitter.sizes())
        if total <= 0:
            return
        wanted = self.steps_area.widget().sizeHint().height() + 4
        top = max(0, min(wanted, total - RESULT_MIN_HEIGHT))
        self.splitter.setSizes([top, total - top])

    def _build_menu_tab(self) -> QWidget:
        tab = QWidget()
        hint = QLabel(MENU_HINT)
        hint.setObjectName("hintLabel")
        hint.setWordWrap(True)
        self.menu_button = QPushButton("Read the Menu")
        self.menu_button.setObjectName("primaryButton")
        self.menu_button.setToolTip("Sends pictures of the menu to the AI - a cent or two")
        self.menu_button.clicked.connect(self.read_menu)
        self.menu_status = QLabel("")
        self.menu_status.setObjectName("hintLabel")
        self.menu_status.setTextFormat(Qt.PlainText)
        self.menu_status.setWordWrap(True)
        self.menu_status.setMinimumWidth(1)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(10)
        row.addWidget(self.menu_button, 0, Qt.AlignTop)
        row.addWidget(self.menu_status, 1)
        self.menu_picture = QLabel()
        self.menu_picture.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        self.menu_picture.setFixedHeight(MENU_PICTURE_HEIGHT)
        self.menu_picture.hide()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)
        layout.addWidget(hint)
        layout.addLayout(row)
        layout.addWidget(self.menu_picture)
        layout.addStretch(1)
        return tab

    def _build_tracklist_tab(self) -> QWidget:
        """Step 1, three ways round, easiest first: look it up; else paste
        one; else go without."""
        tab = QWidget()
        get_caption = QLabel("1 · Get a tracklist - any one of these")
        get_caption.setObjectName("stepCaption")
        look_caption = QLabel("Look it up on MusicBrainz")
        look_caption.setObjectName("sectionCaption")
        self.query = QLineEdit(utils.suggest_search_query(self.video, self._library_root))
        self.query.setPlaceholderText("Artist and album…")
        self.query.returnPressed.connect(self.search)
        self.search_button = QPushButton("Search MusicBrainz")
        self.search_button.clicked.connect(self.search)
        query_row = QHBoxLayout()
        query_row.setContentsMargins(0, 0, 0, 0)
        query_row.setSpacing(6)
        query_row.addWidget(self.query, 1)
        query_row.addWidget(self.search_button)

        saved = store.load_app_settings().get("musicbrainz_format", "xml")
        self.xml_radio = QRadioButton("XML")
        self.json_radio = QRadioButton("JSON")
        formats = QButtonGroup(tab)
        formats.addButton(self.xml_radio)
        formats.addButton(self.json_radio)
        (self.json_radio if saved == "json" else self.xml_radio).setChecked(True)
        self.xml_radio.toggled.connect(self._save_format)
        format_row = QHBoxLayout()
        format_row.setContentsMargins(0, 0, 0, 0)
        format_row.setSpacing(6)
        format_row.addWidget(QLabel("Request format:"))
        format_row.addWidget(self.xml_radio)
        format_row.addWidget(self.json_radio)
        format_hint = QLabel("(some networks and VPNs are far more reliable with one)")
        format_hint.setObjectName("hintLabel")
        format_row.addWidget(format_hint)
        format_row.addStretch(1)

        self.results = QListWidget()
        self.results.setSelectionMode(QAbstractItemView.SingleSelection)
        # A search gives up to ten; enough of them in sight to choose from.
        self.results.setMinimumHeight(150)
        self.results.setMaximumHeight(220)
        self.results.itemSelectionChanged.connect(self._on_release_selected)
        self.results.hide()

        self.media_caption = QLabel("Discs making up this video")
        self.media_caption.setObjectName("sectionCaption")
        self.media_list = QListWidget()
        self.media_list.setMinimumHeight(70)
        self.media_list.setMaximumHeight(150)
        self.media_list.itemChanged.connect(lambda _item: self._on_discs_changed())
        self.media_caption.hide()
        self.media_list.hide()

        self.mb_status = QLabel(LOOK_UP_HINT)
        self.mb_status.setObjectName("hintLabel")
        self.mb_status.setWordWrap(True)
        self._update_mb_controls()

        paste_caption = QLabel("Or paste one")
        paste_caption.setObjectName("sectionCaption")
        self.paste_toggle = QPushButton("Paste a Tracklist…")
        self.paste_toggle.setCheckable(True)
        self.paste_toggle.toggled.connect(self._show_paste)
        instructions = QLabel(PASTE_INSTRUCTIONS)
        instructions.setObjectName("hintLabel")
        instructions.setWordWrap(True)
        self.paste = QPlainTextEdit()
        self.paste.setPlaceholderText("Paste the tracklist here…")
        self.paste.setMinimumHeight(110)
        self._paste_timer = QTimer(self)
        self._paste_timer.setSingleShot(True)
        self._paste_timer.setInterval(TYPING_DEBOUNCE_MS)
        self._paste_timer.timeout.connect(self._on_paste_changed)
        self.paste.textChanged.connect(self._paste_timer.start)
        load = QPushButton("Load Current Titles")
        load.setToolTip(
            "Fill this box with the names already on these chapters, so they can be "
            "copied out, translated, and pasted back."
        )
        load.clicked.connect(self._load_current_titles)
        copy = QPushButton("Copy")
        copy.clicked.connect(
            lambda: QApplication.clipboard().setText(self.paste.toPlainText())
        )
        paste_buttons = QHBoxLayout()
        paste_buttons.setContentsMargins(0, 0, 0, 0)
        paste_buttons.setSpacing(6)
        paste_buttons.addWidget(load)
        paste_buttons.addWidget(copy)
        paste_buttons.addStretch(1)
        self.paste_area = QWidget()
        paste_layout = QVBoxLayout(self.paste_area)
        paste_layout.setContentsMargins(0, 0, 0, 0)
        paste_layout.setSpacing(6)
        paste_layout.addWidget(instructions)
        paste_layout.addWidget(self.paste, 1)
        paste_layout.addLayout(paste_buttons)
        self.paste_area.hide()
        paste_row = QHBoxLayout()
        paste_row.setContentsMargins(0, 0, 0, 0)
        paste_row.addWidget(paste_caption)
        paste_row.addStretch(1)
        paste_row.addWidget(self.paste_toggle)

        none_caption = QLabel("Or go without")
        none_caption.setObjectName("sectionCaption")
        if len(self.video["chapters"]) > 1:
            without = NO_TRACKLIST_CHAPTERED
        else:
            without = NO_TRACKLIST_ONE_PIECE if self._can_analyse else NO_TRACKLIST_DISC
        none_label = QLabel(without)
        none_label.setObjectName("hintLabel")
        none_label.setWordWrap(True)

        layout = QVBoxLayout(tab)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)
        layout.addWidget(get_caption)
        layout.addWidget(look_caption)
        layout.addLayout(query_row)
        layout.addLayout(format_row)
        layout.addWidget(self.mb_status)
        layout.addWidget(self.results)
        layout.addWidget(self.media_caption)
        layout.addWidget(self.media_list)
        layout.addLayout(paste_row)
        layout.addWidget(self.paste_area, 1)
        layout.addWidget(none_caption)
        layout.addWidget(none_label)
        layout.addStretch(0)
        self.query.setFocus()
        self.query.selectAll()
        return tab

    def _show_paste(self, shown: bool) -> None:
        self.paste_area.setVisible(shown)
        self.paste_toggle.setText("Hide the Tracklist" if shown else "Paste a Tracklist…")
        if shown:
            self.paste.setFocus()
        QTimer.singleShot(0, self._fit_steps)

    # --- results ---------------------------------------------------------

    def result(self) -> tuple[str, object] | None:
        """("name", [(chapter_index, title), ...]) to name the existing
        chapters, ("replace", (chapters, origin)) to replace them, or
        ("change", autoname.Change) for what Just Figure It Out found - or
        None when there's nothing to apply."""
        return self._result

    def _tracks_from_musicbrainz(self) -> bool:
        return not self._paste_tracks and bool(self._mb_tracks)

    def title_source(self) -> str:
        if self._ai_result is not None:
            return ai_chapters.SOURCE
        return "musicbrainz" if self._tracks_from_musicbrainz() else "manual"

    def release_id(self) -> str | None:
        """The MusicBrainz release the names came from, if they did."""
        if self._figured is None and self._tracks_from_musicbrainz():
            return self._release_id
        return None

    def done(self, result: int) -> None:
        # Closing stops any measuring still running for this dialog.
        self._cancel.set()
        super().done(result)

    # --- the tracklist ---------------------------------------------------

    def tracks(self) -> list[dict]:
        """The tracklist: one pasted in, else the MusicBrainz release's
        chosen discs, else none."""
        if self._paste_tracks:
            return self._paste_tracks
        return self._chosen_disc_tracks()

    def _format(self) -> str:
        return "xml" if self.xml_radio.isChecked() else "json"

    def _save_format(self) -> None:
        settings = store.load_app_settings()
        settings["musicbrainz_format"] = self._format()
        store.save_app_settings(settings)

    def _update_mb_controls(self) -> None:
        """MusicBrainz's search, unless the video is private or Settings →
        Privacy doesn't allow it - saying which."""
        off = PRIVATE_NOTE if self._private else (
            "" if privacy.allowed(privacy.MUSICBRAINZ) else MUSICBRAINZ_OFF_NOTE
        )
        for widget in (self.query, self.search_button, self.xml_radio, self.json_radio):
            widget.setEnabled(not off)
        if off:
            self.mb_status.setText(off)
        elif self.mb_status.text() == MUSICBRAINZ_OFF_NOTE:
            self.mb_status.setText(LOOK_UP_HINT)

    def search(self) -> None:
        query = self.query.text().strip()
        if not query or self._private or not privacy.allowed(privacy.MUSICBRAINZ):
            return
        self.mb_status.setText("Searching MusicBrainz…")
        self.results.clear()
        self.search_button.setEnabled(False)
        fmt = self._format()
        self._jobs.append(run_job(
            self,
            lambda: musicbrainz.search_releases(query, fmt=fmt),
            on_done=self._show_results,
            on_failed=lambda message: self._mb_failed(f"Search failed: {message}"),
        ))

    def _show_results(self, results) -> None:
        self.search_button.setEnabled(True)
        self.results.setVisible(bool(results))
        self.mb_status.setText(
            f"{len(results)} release(s) found. Pick one." if results else
            "Nothing found. Try the artist and album alone - or paste a tracklist below."
        )
        QTimer.singleShot(0, self._fit_steps)
        for release in results:
            count = f"{release['track_count']} tracks" if release["track_count"] else "? tracks"
            item = QListWidgetItem(
                f"{release['artist']} - {release['title']} ({release['date']}) [{count}]"
            )
            item.setData(Qt.UserRole, release["id"])
            self.results.addItem(item)

    def _on_release_selected(self) -> None:
        item = self.results.currentItem()
        if item is None:
            return
        release_id = item.data(Qt.UserRole)
        self._release_id = release_id
        self.mb_status.setText("Fetching the tracklist…")
        fmt = self._format()
        self._jobs.append(run_job(
            self,
            lambda: musicbrainz.get_release_media(release_id, fmt=fmt),
            on_done=self.show_media,
            on_failed=lambda message: self._mb_failed(f"Lookup failed: {message}"),
        ))

    def _mb_failed(self, message: str) -> None:
        self.search_button.setEnabled(True)
        self.mb_status.setText(message)

    def show_media(self, media) -> None:
        """A release's discs, the ones making up this video ticked."""
        self._figured = None
        self._mb_media = media
        self._mb_tracks = musicbrainz.flatten(media)
        # With no lengths to go on there's no telling which discs are this
        # video, so all of them start ticked.
        picked = set(chaptergen.pick_media(media, self.video["duration"])) or set(
            range(len(media))
        )
        usable = set(chaptergen.usable_media(media))
        self.media_list.blockSignals(True)
        self.media_list.clear()
        for i, medium in enumerate(media):
            parts = [f"Disc {i + 1}"]
            if medium["format"]:
                parts.append(medium["format"])
            if medium["title"]:
                parts.append(medium["title"])
            parts.append(f"{len(medium['tracks'])} tracks")
            parts.append(
                utils.format_seconds(chaptergen.media_total(medium))
                if i in usable else "no lengths"
            )
            item = QListWidgetItem(" · ".join(parts))
            item.setData(Qt.UserRole, i)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if i in picked else Qt.Unchecked)
            self.media_list.addItem(item)
        self.media_list.blockSignals(False)
        several = len(media) > 1
        self.media_caption.setVisible(several)
        self.media_list.setVisible(several)
        QTimer.singleShot(0, self._fit_steps)
        self.mb_status.setText(f"{len(self._mb_tracks)} tracks on {len(media)} disc(s).")
        self.refresh()

    def _chosen_disc_tracks(self) -> list[dict]:
        if not self._mb_media:
            return []
        chosen = []
        for row in range(self.media_list.count()):
            item = self.media_list.item(row)
            if item.checkState() == Qt.Checked:
                chosen.extend(self._mb_media[item.data(Qt.UserRole)]["tracks"])
        return chosen

    def _on_discs_changed(self) -> None:
        self._figured = None
        self.refresh()

    def clear_proposal(self) -> None:
        """Back to nothing proposed: no tracklist, no method chosen, no
        answer from Just Figure It Out, the AI or the disc's menu. What's
        been measured is kept - it's the same video."""
        if self._figure_job is not None or self._ai_job is not None or self._menu_job is not None:
            return
        self._figured = None
        self._menu_result = None
        self._method, self._method_picked = None, False
        self._paste_timer.stop()
        self.paste.blockSignals(True)
        self.paste.clear()
        self.paste.blockSignals(False)
        self._paste_tracks = []
        self._mb_media, self._mb_tracks, self._release_id = [], None, None
        self.results.blockSignals(True)
        self.results.clearSelection()
        self.results.setCurrentItem(None)
        self.results.blockSignals(False)
        self.media_list.clear()
        self.media_list.hide()
        self.mb_status.setText(LOOK_UP_HINT)
        self.refresh()

    def _on_paste_changed(self) -> None:
        self._figured = None
        self._paste_tracks = utils.parse_pasted_tracklist(self.paste.toPlainText())
        self.refresh()

    def _load_current_titles(self) -> None:
        text = utils.export_named_chapters(self.video["chapters"])
        if text:
            self.paste_toggle.setChecked(True)
            self.paste.setPlainText(text)

    # --- choosing how ----------------------------------------------------

    def _pick_method(self, method: str) -> None:
        self._figured = None
        self._method_picked = True
        self._method = method
        self.refresh()

    def _update_method(self, tracks) -> None:
        # A tracklist picks the best way to use it. Without one nothing is
        # proposed until asked for: detecting from the audio straight away
        # filled the table with chapters nobody had asked for, there the
        # moment the dialog opened once the audio was measured - which, on
        # opening it again after a reset, looked like the last try left over.
        usable = methods.available(self.video, tracks, self._can_analyse)
        if not self._method_picked or self._method not in usable:
            self._method_picked = False
            self._method = (methods.choose(self.video, tracks, self._can_analyse)
                            if tracks else None)
        for method, radio in self.method_radios.items():
            radio.setEnabled(method in usable)
            radio.setChecked(method == self._method)
        if self._method is None:
            self.method_group.setExclusive(False)
            for radio in self.method_radios.values():
                radio.setChecked(False)
            self.method_group.setExclusive(True)

        lengths = self._method == methods.LENGTHS
        detect = self._method == methods.DETECT
        self.snap_check.setVisible(lengths)
        self.intro_check.setVisible(lengths)
        self.light_check.setVisible(detect)
        self.count_label.setVisible(detect)
        self.count.setVisible(detect)
        # A tracklist says how many songs there are.
        self.count.blockSignals(True)
        if detect and tracks:
            self.count.setValue(len(tracks))
        self.count.setEnabled(detect and not tracks)
        self.count.blockSignals(False)
        self.method_hint.setText(METHOD_HINTS.get(self._method, ""))

    # --- measuring -------------------------------------------------------

    def _needs_levels(self) -> bool:
        if not self._can_analyse:
            return False
        if self._method == methods.DETECT:
            return True
        return self._method == methods.LENGTHS and (
            self.snap_check.isChecked() or self.intro_check.isChecked()
        )

    def _needs_light(self) -> bool:
        return self._method == methods.DETECT and self._can_analyse and self.light_check.isChecked()

    def _ensure_measured(self) -> bool:
        """Start whatever measuring the method needs; True once it's all in."""
        if self._analysis_error:
            return True
        if self._needs_levels() and self._levels is None:
            self._start_levels()
            return False
        if self._needs_light() and self._light is None:
            self._light = audio_analysis.cached_light(self.video, self._levels)
            if self._light is None:
                if self._light_job is None:
                    self.progress.setFormat("Checking the stage lighting… %p%")
                    self.progress.setValue(0)
                    self.progress.show()
                    self._light_job = audio_analysis.start_light(
                        self, self.video, self._levels, self._cancel,
                        on_progress=self.progress.setValue,
                        on_done=self._on_light,
                        on_failed=lambda message: self._on_light({}),
                    )
                return False
        self.progress.hide()
        return True

    def _prefetch_levels(self) -> None:
        """Start measuring the audio while a tracklist is found, for a video
        that will want it - one without chapters of its own, whatever the
        tracklist turns out to be: placing by lengths splits intros with
        it, and detecting needs it."""
        wanted = self._can_analyse and (
            len(self.video["chapters"]) <= 1
            or self.video.get("chapter_origin") == library.ORIGIN_ESTIMATED
        )
        if wanted and self._levels is None and not self._analysis_error:
            self._start_levels()

    def _start_levels(self) -> None:
        """Measure the audio, unless that's already under way."""
        if self._levels_job is not None:
            return
        self.progress.setFormat("Measuring the audio… %p%")
        self.progress.setValue(0)
        self.progress.show()
        self._levels_job = audio_analysis.start(
            self, self.video, self._cancel,
            on_progress=self.progress.setValue,
            on_done=self._on_levels,
            on_failed=self._on_analysis_failed,
        )

    def _on_levels(self, levels) -> None:
        self._levels_job = None
        self._levels = levels
        self.refresh()
        if self._ai_waiting:
            self._ai_waiting = False
            self.ask_ai()

    def _on_light(self, light) -> None:
        self._light_job = None
        self._light = light
        self.refresh()

    def _on_analysis_failed(self, message: str) -> None:
        self._levels_job = None
        self._analysis_error = message
        self.progress.hide()
        self.refresh()

    # --- the preview -----------------------------------------------------

    def refresh(self) -> None:
        tracks = self.tracks()
        self._update_method(tracks)
        self.clear_button.setEnabled(bool(
            tracks or self._method or self._figured or self._menu_result
        ))
        # Once the options shown or hidden have been laid out.
        QTimer.singleShot(0, self._fit_width)
        self._result = None
        # Anything changing underneath the model's answer makes it stale.
        self._ai_result = None
        self.apply_button.setEnabled(False)
        self.tree.clear()
        tracklist = self.tabs.currentIndex() == TAB_TRACKLIST
        self.how_section.setVisible(tracklist)
        # Numbered where it's the third step; the menu tab has no second.
        self.ai_caption.setText(
            ("3 · " if tracklist else "") + "Optional: have the AI check"
        )
        self._update_figure_controls()

        if self._figured is not None:
            self._render_figured(self._figured)
            self._update_ai_controls()
            return

        if self.tabs.currentIndex() == TAB_MENU:
            # The menu's reading doesn't go stale: it's the disc's.
            if self._menu_result is not None:
                self._render_ai(self._menu_result)
            else:
                self.status.setText(
                    "Read the disc's own menu to name these chapters exactly as the disc does."
                )
            self._update_ai_controls()
            return

        if self._method is None:
            self._prefetch_levels()
            if len(self.video["chapters"]) > 1:
                hint = ("Search MusicBrainz or paste a tracklist to name these chapters - "
                        "or ask the AI to, or let Just Figure It Out.")
            elif self._can_analyse:
                hint = ("Search MusicBrainz or paste a tracklist, or choose "
                        f"“{METHOD_LABELS[methods.DETECT]}” to find the chapters where the "
                        "music stops - or let Just Figure It Out.")
            else:
                hint = ("Search MusicBrainz or paste a tracklist - there's nothing to "
                        "detect the chapters from without one.")
            self.status.setText(hint)
            self._update_ai_controls()
            return
        if not self._ensure_measured():
            self.status.setText("Measuring - the preview appears when it's done.")
            self._update_ai_controls()
            return

        if self._method == methods.NAME:
            self._render_naming(tracks)
        elif self._method == methods.LENGTHS:
            self._render_lengths(tracks)
        else:
            self._render_detected(tracks)
        self._update_ai_controls()

    # --- the AI ------------------------------------------------------------

    def _proposed_chapters(self) -> list[dict]:
        """The chapters as the table shows them: the video's own with the
        proposed names, or the ones a method placed."""
        if self._result is not None and self._result[0] == "replace":
            return [dict(ch) for ch in self._result[1][0]]
        if self._result is not None and self._result[0] == "change":
            return [dict(ch) for ch in self._result[1].chapters]
        chapters = [dict(ch) for ch in self.video["chapters"]]
        if self._result is not None:
            for index, title, *_rest in self._result[1]:
                if 0 <= index < len(chapters):
                    chapters[index] = dict(chapters[index], title=title)
        return chapters

    def _ai_mode(self) -> str | None:
        """How the model may treat the chapters: name them where they are,
        place them afresh, or nothing when there's nothing to go on."""
        if self._method in (methods.LENGTHS, methods.DETECT):
            return ai_chapters.PLACE
        if len(self.video["chapters"]) > 1:
            return ai_chapters.NAME
        return None

    def _ai_candidates(self) -> list[tuple[float, float]]:
        if self._levels is None:
            return []
        light = self._light if self.light_check.isChecked() else None
        return chaptergen.boundary_candidates(self._levels, self.video["duration"], light)

    def _ai_situation(self) -> ai_chapters.Situation | None:
        mode = self._ai_mode()
        if mode is None:
            return None
        return ai_chapters.Situation(
            video=self.video,
            chapters=self._proposed_chapters(),
            mode=mode,
            tracks=self.tracks(),
            tracks_source="musicbrainz" if self._tracks_from_musicbrainz() else "pasted",
            candidates=self._ai_candidates() if mode == ai_chapters.PLACE else [],
            context=ai_chapters.situation_context(self.video, self._library_root),
            translate=self.ai_translate_check.isChecked(),
            frames_per_chapter=(
                self._ai_settings[ai.SETTING_FRAMES] if self.ai_frames_check.isChecked() else 0
            ),
            online=self.ai_online_check.isChecked() and self.ai_online_check.isEnabled(),
        )

    def _update_ai_controls(self) -> None:
        configured = ai.is_configured(self._ai_settings) and not self._private
        situation = self._ai_situation() if configured else None
        busy = self._ai_job is not None or self._ai_waiting or self._figure_job is not None
        self.ask_ai_button.setEnabled(configured and situation is not None and not busy)
        if self._private:
            self.ai_hint.setText("This video is private: the AI isn't used for it.")
            return
        if not configured:
            self.ai_hint.setText(ai.not_ready(self._ai_settings))
            return
        model = ai.short_model_name(self._ai_settings[ai.SETTING_MODEL])
        if situation is None:
            self.ai_hint.setText(
                f"{model} needs something to look at: chapters, a tracklist, or a video "
                "file whose audio can be measured."
            )
            return
        if busy:
            return
        frames = len(ai_chapters.frame_plan(situation))
        what = "check and name them" if situation.mode == ai_chapters.NAME else (
            "check where they start and name them"
        )
        self.ai_hint.setText(
            f"Sends the chapter times{', the tracklist' if situation.tracks else ''}"
            f"{f' and {frames} frames' if frames else ''} to {model} via Nano-GPT"
            f"{', with a web search,' if situation.online else ''} to "
            f"{what}. Costs a few cents; nothing is applied until Apply."
        )

    def ask_ai(self) -> None:
        if self._ai_job is not None or self._private:
            return
        # Placing wants the audio's candidate boundaries, so measure first.
        if (self._ai_mode() == ai_chapters.PLACE and self._can_analyse
                and self._levels is None and not self._analysis_error):
            self._ai_waiting = True
            self._start_levels()
            self.ai_hint.setText("Measuring the audio first - the model is asked when it's done.")
            self._update_ai_controls()
            return
        situation = self._ai_situation()
        if situation is None:
            return
        self._figured = None
        settings = self._ai_settings
        model = ai.short_model_name(settings[ai.SETTING_MODEL])
        self.progress.setFormat(f"Asking {model}… %p%")
        self.progress.setValue(0)
        self.progress.show()
        self.ai_hint.setText(f"Taking frames and asking {model}. This can take a minute.")
        cancel = self._cancel

        def work(progress_cb):
            return ai_chapters.run(
                situation, settings,
                progress_cb=lambda percent: progress_cb(str(percent)), cancel=cancel,
            )

        self._ai_job = run_job(
            self, work, wants_progress=True,
            on_progress=lambda text: self.progress.setValue(int(text)),
            on_done=self._on_ai_done,
            on_failed=self._on_ai_failed,
        )
        self._jobs.append(self._ai_job)
        self._update_ai_controls()

    def _on_ai_done(self, result) -> None:
        self._ai_job = None
        self.progress.hide()
        self._render_ai(result)
        self._update_ai_controls()

    def _on_ai_failed(self, message: str) -> None:
        self._ai_job = None
        self.progress.hide()
        self._update_ai_controls()
        self.ai_hint.setText(f"The model couldn't be asked: {message}")

    # --- the disc's menu -----------------------------------------------------

    def read_menu(self) -> None:
        """Read the disc's menu (once), then have the model read its buttons."""
        if self._menu_job is not None or self._private:
            return
        if self._disc_menu is not None:
            self._ask_menu()
            return
        self.menu_button.setEnabled(False)
        self.menu_status.setText("Reading the disc's menus…")
        self.progress.setFormat("Reading the disc's menu… %p%")
        self.progress.setValue(0)
        self.progress.show()
        video, cancel = self.video, self._cancel

        def work(progress_cb):
            return menu_chapters.read(
                video, progress_cb=lambda percent: progress_cb(str(percent)), cancel=cancel
            )

        self._menu_job = run_job(
            self, work, wants_progress=True,
            on_progress=lambda text: self.progress.setValue(int(text)),
            on_done=self._on_menu_read,
            on_failed=self._on_menu_failed,
        )
        self._jobs.append(self._menu_job)

    def _on_menu_read(self, disc: menu_chapters.DiscMenu) -> None:
        self._menu_job = None
        self._disc_menu = disc
        self.progress.hide()
        page = next((p.picture for p in disc.pages if p.picture), None)
        if page:
            pixmap = QPixmap()
            if pixmap.loadFromData(page):
                self.menu_picture.setPixmap(
                    pixmap.scaledToHeight(MENU_PICTURE_HEIGHT, Qt.SmoothTransformation)
                )
                self.menu_picture.show()
        chapters = disc.chapters()
        found = (
            f"{len(disc.buttons)} button(s) on {len(disc.pages)} menu page(s) play "
            f"{len(chapters)} of the {len(self.video['chapters'])} chapters."
        )
        if not ai.is_configured(self._ai_settings):
            self.menu_button.setEnabled(True)
            self.menu_status.setText(f"{found} {ai.not_ready(self._ai_settings)}")
            return
        self.menu_status.setText(found)
        self._ask_menu()

    def _ask_menu(self) -> None:
        if not ai.is_configured(self._ai_settings):
            self.menu_status.setText(ai.not_ready(self._ai_settings))
            return
        disc, video, settings = self._disc_menu, self.video, self._ai_settings
        context = ai_chapters.situation_context(self.video, self._library_root)
        translate = self.ai_translate_check.isChecked()
        model = ai.short_model_name(settings[ai.SETTING_MODEL])
        self.menu_button.setEnabled(False)
        self.menu_status.setText(
            f"{model} is reading {len(disc.buttons)} button(s) off the menu…"
        )
        self.progress.setFormat(f"{model} is reading the menu…")
        self.progress.setRange(0, 0)
        self.progress.show()
        self._menu_job = run_job(
            self, lambda: menu_chapters.ask(video, disc, settings, context, translate),
            on_done=self._on_menu_answer,
            on_failed=self._on_menu_failed,
        )
        self._jobs.append(self._menu_job)

    def _on_menu_answer(self, result: ai_chapters.Result) -> None:
        self._menu_job = None
        self._figured = None
        self.progress.hide()
        self.progress.setRange(0, 100)
        self.menu_button.setEnabled(True)
        self.menu_button.setText("Read It Again")
        self._menu_result = result
        from_menu = sum(1 for r in result.rows if r.confidence == "high"
                        and r.note == menu_chapters.MENU_NOTE)
        self.menu_status.setText(f"{from_menu} chapter(s) named from the menu.")
        if self.tabs.currentIndex() == TAB_MENU:
            self._render_ai(result)
        self._update_figure_controls()

    def _on_menu_failed(self, message: str) -> None:
        self._menu_job = None
        self.progress.hide()
        self.progress.setRange(0, 100)
        self.menu_button.setEnabled(True)
        self.menu_status.setText(message)

    def _render_ai(self, result: ai_chapters.Result) -> None:
        from PySide6.QtGui import QColor

        self._ai_result = result
        self.clear_button.setEnabled(True)
        self.tree.clear()
        self.tree.setHeaderLabels(AI_HEADERS)
        from_menu = any(r.note == menu_chapters.MENU_NOTE for r in result.rows)
        self.table_caption.setText(
            f"Names from the disc's menu, read by {ai.short_model_name(result.model)}"
            if from_menu else
            f"Names from {ai.short_model_name(result.model)}"
            if result.mode == ai_chapters.NAME else
            f"Chapters from {ai.short_model_name(result.model)}"
        )
        for row in result.rows:
            notes = [row.confidence]
            if row.original_title:
                notes.append(row.original_title)
            if row.note:
                notes.append(row.note)
            mark = "~" if row.moved else ""
            item = QTreeWidgetItem([
                str(row.chapter),
                mark + utils.format_seconds(row.start),
                row.title or utils.title_or_number(row.chapter - 1, None),
                " · ".join(notes),
            ])
            item.setTextAlignment(0, Qt.AlignRight | Qt.AlignVCenter)
            item.setTextAlignment(1, Qt.AlignRight | Qt.AlignVCenter)
            item.setToolTip(3, " · ".join(notes))
            if row.moved:
                item.setToolTip(1, "Not where the estimate had it - worth a check")
            if row.confidence in CONFIDENCE_COLOURS:
                item.setForeground(3, QColor(CONFIDENCE_COLOURS[row.confidence]))
            self.tree.addTopLevelItem(item)

        parts = []
        if from_menu:
            named = sum(1 for r in result.rows if r.note == menu_chapters.MENU_NOTE)
            judged = sum(1 for r in result.rows if r.title and r.note != menu_chapters.MENU_NOTE)
            parts.append(
                f"{named} chapter(s) named as the disc's menu names them"
                + (f"; {judged} more the menu doesn't list, named by judgement "
                   "(check those)." if judged else ".")
            )
        else:
            if result.show:
                parts.append(f"It thinks this is {result.show}.")
            if result.setlist:
                parts.append(f"Setlist it went by: {', '.join(result.setlist)}.")
            counts = {c: sum(1 for r in result.rows if r.confidence == c)
                      for c in ("high", "low")}
            parts.append(
                f"{len(result.rows)} chapter(s): {counts['high']} sure, {counts['low']} guessed"
                f"{f', from {result.frames_sent} frames' if result.frames_sent else ''}."
            )
        if result.notes:
            parts.append(result.notes)
        if result.mode == ai_chapters.NAME:
            mapping = result.mapping()
            self._result = ("name", mapping) if mapping else None
        else:
            origin = (
                library.ORIGIN_ESTIMATED
                if result.moved_any or self._method == methods.DETECT
                else library.ORIGIN_TRACKLIST
            )
            if len(self.video["chapters"]) > 1:
                parts.append(f"This replaces the video's {len(self.video['chapters'])} chapters.")
            self._result = ("replace", (result.chapters, origin))
        self.status.setText(" ".join(parts))
        self.apply_button.setEnabled(self._result is not None)

    def _open_settings(self) -> None:
        from mediabrowser.gui.dialogs.settings_dialog import (
            TAB_AI,
            TAB_PRIVACY,
            SettingsDialog,
        )

        # Straight to the key and model, unless the AI isn't allowed at all.
        tab = TAB_AI if ai.allowed(self._ai_settings) else TAB_PRIVACY
        if SettingsDialog(self, tab).exec():
            self._ai_settings = ai.load_settings()
            self._update_mb_controls()
            self._update_online_check()
            self._update_ai_controls()
            self._update_figure_controls()

    def _update_online_check(self) -> None:
        """The AI's web search, unless the endpoint can't or Settings →
        Privacy doesn't allow it."""
        if not ai.searches_the_web(self._ai_settings):
            tip = "Web search is Nano-GPT's own; the endpoint in Settings → AI is another."
        elif not ai.may_search(self._ai_settings):
            tip = privacy.OFF[privacy.WEB_SEARCH]
        else:
            tip = ""
        if tip or not self.ai_online_check.isEnabled():
            # Unticked while it can't be had; ticked again, the default,
            # once it can.
            self.ai_online_check.setChecked(not tip)
        self.ai_online_check.setEnabled(not tip)
        self.ai_online_check.setToolTip(tip or self._online_tip)

    # --- the whole job ---------------------------------------------------------

    def _build_hints(self) -> QWidget:
        """What the person knows about the video, for Just Figure It Out:
        what it's known as, how many songs, the setlist. All optional."""
        self.hint_known_as = QLineEdit()
        self.hint_known_as.setPlaceholderText(
            "What it's likely to be found under - e.g. BABYMETAL LEGEND - METAL FORTH"
        )
        self.hint_songs = QSpinBox()
        self.hint_songs.setRange(0, 200)
        self.hint_songs.setSpecialValueText("Don't know")
        self.hint_songs.setToolTip("How many songs this video has")
        self.hint_setlist = QPlainTextEdit()
        self.hint_setlist.setPlaceholderText(
            "The songs in order, one per line (numbers and times are fine)…"
        )
        self.hint_setlist.setMinimumHeight(90)
        note = QLabel(HINTS_NOTE)
        note.setObjectName("hintLabel")
        note.setWordWrap(True)
        note.setMinimumWidth(1)
        known_row = QHBoxLayout()
        known_row.setContentsMargins(0, 0, 0, 0)
        known_row.setSpacing(8)
        known_row.addWidget(QLabel("Known as:"))
        known_row.addWidget(self.hint_known_as, 1)
        known_row.addWidget(QLabel("Songs:"))
        known_row.addWidget(self.hint_songs)
        area = QWidget()
        layout = QVBoxLayout(area)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        layout.addWidget(note)
        layout.addLayout(known_row)
        layout.addWidget(self.hint_setlist)
        area.hide()
        return area

    def _show_hints(self, shown: bool) -> None:
        self.hints_area.setVisible(shown)

    def figure_hints(self) -> autoname.Hints:
        """What the person filled in of the hints; nothing while they're
        put away."""
        if not self.hints_toggle.isChecked():
            return autoname.Hints()
        setlist = utils.parse_pasted_tracklist(self.hint_setlist.toPlainText())
        return autoname.Hints(
            known_as=self.hint_known_as.text().strip(),
            songs=self.hint_songs.value(),
            setlist=setlist,
        )

    def _figure_options(self) -> autoname.Options:
        """Everything there is, the most accurate first - someone is here to
        check, so the AI's guesses are shown too, marked."""
        methods_wanted = set(autoname.METHODS)
        if not self.ai_translate_check.isChecked():
            methods_wanted.discard(autoname.TRANSLATE)
        if not privacy.allowed(privacy.MUSICBRAINZ):
            methods_wanted.discard(autoname.MUSICBRAINZ)
        return autoname.Options(
            methods=methods_wanted,
            ai_policy=autoname.ACCURATE_FIRST,
            ai_budget=autoname.ONE_VIDEO_AI_BUDGET,
            only_sure=False,
            translate=self.ai_translate_check.isChecked(),
        )

    def _update_figure_controls(self) -> None:
        model = ai.short_model_name(self._ai_settings[ai.SETTING_MODEL])
        busy = self._figure_job is not None
        self.hints_toggle.setEnabled(not self._private and ai.is_configured(self._ai_settings))
        if self._private:
            self.figure_button.setEnabled(False)
            self.figure_note.setText(
                "This video is private, so there's nothing to figure out with: the AI "
                "and MusicBrainz aren't used for it."
            )
        elif not ai.is_configured(self._ai_settings):
            self.figure_button.setEnabled(False)
            self.figure_note.setText(
                "Uses the AI, which can't be asked: " + ai.not_ready(self._ai_settings)
            )
        elif not busy:
            self.figure_button.setEnabled(
                self._ai_job is None and self._menu_job is None
            )
            self.figure_note.setText(FIGURE_NOTE.format(model=model))

    def figure_it_out(self) -> None:
        """Identify this video as Identify Library would, with every method
        and the most accurate first, and show what it would change."""
        if (self._figure_job is not None or self._private
                or not ai.is_configured(self._ai_settings)):
            return
        video = dict(self.video, chapters=[dict(c) for c in self.video["chapters"]])
        options = self._figure_options()
        hints = self.figure_hints()
        settings = self._ai_settings
        root, cancel = self._library_root, self._cancel
        mb_format = self._format()

        def work():
            return autoname.identify(
                "", video, options, settings, autoname.Services(mb_format),
                autoname.Budget(options.ai_budget), root, cancel, hints=hints,
            )

        self.figure_button.setEnabled(False)
        self.figure_note.setText(
            "Working it out: the menu, MusicBrainz, the audio and the AI, as far as "
            "this video needs. This can take a minute or two."
        )
        self.progress.setFormat("Figuring it out…")
        self.progress.setRange(0, 0)
        self.progress.show()
        self._figure_job = run_job(
            self, work, on_done=self._on_figured, on_failed=self._on_figure_failed,
        )
        self._jobs.append(self._figure_job)
        self._update_ai_controls()

    def _figure_finished(self) -> None:
        self._figure_job = None
        self.progress.hide()
        self.progress.setRange(0, 100)

    def _on_figured(self, outcome: autoname.Outcome) -> None:
        self._figure_finished()
        self._report = outcome.report_text()
        self.report_button.setEnabled(bool(self._report))
        if outcome.error:
            self._on_figure_failed(outcome.error)
            return
        self._figured = outcome
        self.refresh()

    def _on_figure_failed(self, message: str) -> None:
        self._figure_finished()
        self._update_figure_controls()
        self._update_ai_controls()
        self.status.setText(f"It couldn't be worked out: {message}")

    def show_report(self) -> None:
        """The last Just Figure It Out, at length, to read and copy."""
        if not self._report:
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("What Just Figure It Out Did")
        text = QPlainTextEdit(self._report)
        text.setReadOnly(True)
        text.setLineWrapMode(QPlainTextEdit.WidgetWidth)
        copy = QPushButton("Copy")
        copy.clicked.connect(lambda: QApplication.clipboard().setText(self._report))
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(dialog.reject)
        buttons.addButton(copy, QDialogButtonBox.ActionRole)
        layout = QVBoxLayout(dialog)
        layout.addWidget(text, 1)
        layout.addWidget(buttons)
        dialog.resize(760, 620)
        self._report_dialog = dialog  # for tests
        dialog.open()

    def _render_figured(self, outcome: autoname.Outcome) -> None:
        from PySide6.QtGui import QColor

        self.tree.setHeaderLabels(AI_HEADERS)
        self.table_caption.setText("What Just Figure It Out found")
        chapters = outcome.change.chapters if outcome.change else self.video["chapters"]
        for i, chapter in enumerate(chapters):
            source = chapter.get("source") or "auto-numbered"
            notes = [FIGURE_SOURCES.get(source, "")]
            if chapter.get("original_title"):
                notes.append(chapter["original_title"])
            item = QTreeWidgetItem([
                str(i + 1),
                ("~" if chapter.get("estimated") else "")
                + utils.format_seconds(chapter["start"]),
                chapter.get("title") or utils.title_or_number(i, None),
                " · ".join(n for n in notes if n),
            ])
            item.setTextAlignment(0, Qt.AlignRight | Qt.AlignVCenter)
            item.setTextAlignment(1, Qt.AlignRight | Qt.AlignVCenter)
            if not chapter.get("title"):
                item.setForeground(2, QColor("#565c69"))
            self.tree.addTopLevelItem(item)
        said = " ".join(line for line in outcome.log if line)
        if outcome.change is None:
            self.status.setText(f"Nothing more could be found. {said}".strip())
            self._result = None
        else:
            self.status.setText(
                f"{outcome.before.describe()} → {outcome.after.describe()}. {said}".strip()
            )
            self._result = ("change", outcome.change)
        self.apply_button.setEnabled(self._result is not None)
        self._update_figure_controls()

    def _render_naming(self, tracks) -> None:
        self.tree.setHeaderLabels(NAME_HEADERS)
        self.table_caption.setText("Proposed names")
        result = proposal.build(self.video["chapters"], tracks)
        for row in result.rows:
            note = " · ".join(part for part in (row.flag, row.note) if part)
            item = QTreeWidgetItem([row.chapter, row.duration, row.proposed, note])
            item.setTextAlignment(0, Qt.AlignRight | Qt.AlignVCenter)
            item.setTextAlignment(1, Qt.AlignRight | Qt.AlignVCenter)
            if note:
                item.setToolTip(3, note)
            if row.flag in FLAG_COLOURS:
                from PySide6.QtGui import QColor

                colour = QColor(FLAG_COLOURS[row.flag])
                item.setForeground(3, colour)
                if row.chapter_index is None:
                    item.setForeground(2, colour)
            self.tree.addTopLevelItem(item)
        mapping = result.mapping()
        self.status.setText(result.summary)
        if mapping:
            self._result = ("name", mapping)
            self.apply_button.setEnabled(True)

    def _render_lengths(self, tracks) -> None:
        duration = self.video["duration"]
        source = self.title_source()
        chapters = chaptergen.chapters_from_tracks(tracks, duration, source)
        notes = []
        if self._levels is not None:
            if self.snap_check.isChecked():
                starts = chaptergen.snap_starts([c["start"] for c in chapters], self._levels)
                chapters = chaptergen.chapters_from_starts(
                    starts, duration, [c["title"] for c in chapters], source
                )
            if self.intro_check.isChecked():
                before = len(chapters)
                chapters = chaptergen.split_intros(chapters, self._levels)
                if len(chapters) > before:
                    notes.append(f"{len(chapters) - before} intro(s) split off.")
        elif self._analysis_error:
            notes.append(f"The audio couldn't be measured: {self._analysis_error}")

        total = sum(t["length"] for t in tracks)
        difference = duration - total
        summary = (
            f"{len(chapters)} chapter(s) from {len(tracks)} track(s). The tracks add up "
            f"to {utils.format_seconds(total)}; this video is {utils.format_seconds(duration)}"
        )
        if round(difference):
            summary += (
                f" ({utils.format_seconds(abs(difference))} "
                f"{'longer' if difference > 0 else 'shorter'})."
            )
        else:
            summary += "."
        if abs(difference) > max(TOTAL_WARNING_MIN_SECONDS, TOTAL_WARNING_FRACTION * duration):
            summary += (
                " That's a big difference - check that only the discs making up this "
                "video are chosen."
            )
        self._show_placed(chapters, " ".join([summary] + notes))
        self._result = ("replace", (chapters, library.ORIGIN_TRACKLIST))

    def _render_detected(self, tracks) -> None:
        if self._levels is None:
            self.status.setText(f"The audio couldn't be measured: {self._analysis_error}")
            return
        duration = self.video["duration"]
        titles = [t["title"] for t in tracks]
        wanted = len(tracks) if tracks else (self.count.value() or None)
        light = self._light if self.light_check.isChecked() else None
        starts = chaptergen.estimate_starts(self._levels, duration, wanted, light=light)
        chapters = chaptergen.chapters_from_starts(
            starts, duration, titles, source=self.title_source(), estimated=True
        )
        summary = f"{len(chapters)} chapter(s) estimated from the sound"
        summary += " and the stage lighting." if light else "."
        if wanted and len(chapters) < wanted:
            summary += (
                f" Only {len(chapters)} of the {wanted} songs could be told apart - "
                "split the rest by hand while the video plays."
            )
        unused = titles[len(chapters):]
        if unused:
            summary += f" Not yet placed: {', '.join(unused)}."
        self._show_placed(chapters, summary)
        self._result = ("replace", (chapters, library.ORIGIN_ESTIMATED))

    def _show_placed(self, chapters, summary: str) -> None:
        self.tree.setHeaderLabels(PLACE_HEADERS)
        self.table_caption.setText("Proposed chapters")
        for i, chapter in enumerate(chapters):
            mark = "~" if chapter.get("estimated") else ""
            item = QTreeWidgetItem([
                str(i + 1),
                mark + utils.format_seconds(chapter["start"]),
                chapter["title"] or utils.title_or_number(i, None),
                utils.format_seconds(chapter["end"] - chapter["start"]),
            ])
            for column in (0, 1, 3):
                item.setTextAlignment(column, Qt.AlignRight | Qt.AlignVCenter)
            if mark:
                item.setToolTip(1, "Found from the audio - worth a check")
            self.tree.addTopLevelItem(item)
        if len(self.video["chapters"]) > 1:
            summary += f" This replaces the video's {len(self.video['chapters'])} chapters."
        self.status.setText(summary)
        self.apply_button.setEnabled(bool(chapters))
