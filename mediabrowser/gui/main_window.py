# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""The main window.

Holds the panels together and owns the library data and the player. Anything
with real logic lives in mediabrowser.core: this decides what to show and
when, not what any of it means.
"""

from __future__ import annotations

import threading
from pathlib import Path

from PySide6.QtCore import QStandardPaths, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QActionGroup, QCursor, QKeySequence
from PySide6.QtWidgets import (
    QCheckBox,
    QDockWidget,
    QFileDialog,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QStackedWidget,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from mediabrowser.core import (
    artwork,
    autoname,
    catalog,
    chapter_edit,
    folders,
    letterbox,
    library,
    naming,
    network,
    playback,
    playlists,
    protection,
    reset,
    store,
    utils,
)
from mediabrowser.core.player import Player
from mediabrowser.core.shelf import Shelf
from mediabrowser.gui.chapter_editor import ChapterEditor
from mediabrowser.gui.detail_view import DetailView
from mediabrowser.gui.grid_view import GridView
from mediabrowser.gui.library_panel import LibraryPanel
from mediabrowser.gui.list_view import ListView
from mediabrowser.gui.now_playing import NowPlayingBar
from mediabrowser.gui.queue_panel import QueuePanel
from mediabrowser.gui.video_page import VideoPage
from mediabrowser.gui.video_surface import can_embed
from mediabrowser.gui.worker import run_job

# Typing filters the whole shelf, so it waits for a pause rather than
# rebuilding on every keystroke the way the old UI did.
SEARCH_DEBOUNCE_MS = 180

# How often the transport bar asks mpv where it has got to. Fine enough for a
# seek bar to look live, coarse enough that the round trips cost nothing.
POLL_MS = 400

# Which libraries were on show, so the next start shows them again.
SHOWN_SETTING = "shown_libraries"

# Where video plays: in the main area (true, the default where it can) or
# in mpv's own window. An app setting.
VIDEO_IN_APP_SETTING = "video_in_app"

# What a chapter plays as when nothing says which: on a double-click (or
# Enter), the main Play button, and Add to Queue into an empty queue. Audio
# (true, the default) or video. An app setting.
AUDIO_BY_DEFAULT_SETTING = "play_audio_by_default"

# Where the S key's screenshots go, in the Pictures folder.
SCREENSHOT_FOLDER = "DA Media Browser"

# In fullscreen, how often the pointer is looked at, and how long it must be
# still before the transport bar gets out of the way.
POINTER_WATCH_MS = 250
CONTROLS_HIDE_MS = 2500

PAGE_SHELF = 0
PAGE_DETAIL = 1
PAGE_EMPTY = 2
PAGE_EDITOR = 3
PAGE_VIDEO = 4

SHELF_GRID = 0
SHELF_LIST = 1

# How the chosen shelf view is written in the app settings, so it survives
# a restart.
VIEW_SETTING = "shelf_view"
VIEW_NAMES = {SHELF_GRID: "tiles", SHELF_LIST: "list"}

# How far Ctrl+Left / Ctrl+Right jump.
SEEK_STEP_SECONDS = 10.0

SORT_MODES = (
    ("name", "Name", lambda pair: pair[1]["display_name"].lower()),
    ("duration", "Length", lambda pair: -(pair[1]["duration"] or 0)),
    ("chapters", "Chapter count", lambda pair: -len(pair[1]["chapters"])),
    ("named", "Least named", lambda pair: naming.status(pair[1]).fraction),
)


class MainWindow(QMainWindow):
    library_changed = Signal()

    def __init__(self, *, bluray_available: bool = True) -> None:
        super().__init__()
        self.setWindowTitle("DA Media Browser")
        self.resize(1180, 780)

        self.bluray_available = bluray_available
        self.player = Player()
        self.player.screenshot_dir = _screenshot_dir()
        self.now_playing: tuple[str, int, bool] | None = None
        self.queue = playback.Queue()
        self._jobs: list = []
        self._artwork_job = None
        self._scanning = False
        self._scan_cancel: threading.Event | None = None
        self._sort = SORT_MODES[0]
        self._missing: set[str] = set()
        self._open_video_id: str | None = None
        # Whether videos in hidden folders are on the shelf. Deliberately not
        # remembered across restarts: hidden is meant to stay hidden.
        self._show_hidden = False
        # The search the shelf was last filled for. Refilling it for the same
        # search (a video hidden, renamed, rescanned) keeps it scrolled where
        # it was; a new search starts from the top.
        self._shelf_search: str | None = None
        self._preferred_view = {name: mode for mode, name in VIEW_NAMES.items()}.get(
            store.load_app_settings().get(VIEW_SETTING), SHELF_GRID
        )
        # The slice of the file the current chapter occupies. Positions
        # reported by mpv are absolute within the file, so the transport bar
        # needs this to show them as a position within the chapter.
        self._window: tuple[float, float | None] = (0.0, None)
        # The last position and file length mpv reported.
        self._last_position: float | None = None
        self._last_duration: float | None = None
        # The queue, played through one mpv (core.playback.Session).
        self.session = playback.Session(self.player, self.queue, self._playable_video)
        # Video in the app: the page shown before it, and fullscreen.
        self._page_before_video = PAGE_SHELF
        self._fullscreen = False
        self._hidden_for_fullscreen: list = []
        self._was_maximized = False
        self._pointer = None
        self._pointer_still = 0
        # Identify Library, once opened: it isn't modal, and a run in it
        # carries on with the window closed.
        self._identify_dialog = None
        # A catalog from elsewhere the last scan carried over, to say so.
        self._adopted = None
        # Videos being identified from their right-click menu.
        self._identifying_videos: set[str] = set()
        # Videos whose black bars are being measured (letterbox).
        self._measuring_bars: set[str] = set()

        self.shown = Shelf([self._startup_library()])
        known = {lib["root"] for lib in store.list_libraries()}
        for root in store.load_app_settings().get(SHOWN_SETTING, [])[1:]:
            if root in known and self.shown.library_at(root) is None:
                self.shown.replace(self._prepared(store.load_library_for_root(root)))

        self._build_ui()
        self._build_actions()
        self.refresh_library()

    # --- the libraries on show -------------------------------------------------

    @property
    def data(self) -> dict:
        """The library the window works on: the one on show, or - with
        several - the shelf's stand-in for all of them (core.shelf)."""
        return self.shown.view()

    @data.setter
    def data(self, value: dict) -> None:
        self.shown = Shelf([value])

    def _startup_library(self) -> dict:
        known = [lib["root"] for lib in store.list_libraries()]
        shown = [root for root in store.load_app_settings().get(SHOWN_SETTING, [])
                 if root in known]
        first = shown[0] if shown else (known[0] if known else None)
        return self._prepared(
            store.load_library_for_root(first) if first else store.default_library()
        )

    def _lib(self, video_id: str | None) -> dict:
        """The library a video belongs to."""
        return self.shown.library_of(video_id)

    def _root_of(self, video_id: str) -> str | None:
        return self._lib(video_id)["settings"].get("library_root")

    def _save(self, video_id: str | None = None) -> None:
        self.shown.save(video_id)

    def _remember_shown(self) -> None:
        settings = store.load_app_settings()
        settings[SHOWN_SETTING] = self.shown.roots()
        store.save_app_settings(settings)

    def show_libraries(self, roots) -> None:
        """Put these libraries on show together (in this order)."""
        roots = [root for root in dict.fromkeys(roots) if root]
        if not roots:
            return
        if self.identifying():
            self._set_status("Identify Library is running - stop it first.", "warning")
            self.libraries.refresh(self.shown.roots())
            return
        loaded = []
        for root in roots:
            shown = self.shown.library_at(root)
            loaded.append(shown if shown is not None
                          else self._prepared(store.load_library_for_root(root)))
        self.shown = Shelf(loaded)
        self._remember_shown()
        self._shelf_search = None  # another shelf starts at the top
        # What's playing plays on if it's still on the shelf.
        if self.now_playing is not None and self.now_playing[0] not in self.data["videos"]:
            self.stop_playback()
        self.show_grid()
        self.refresh_library()

    # --- construction ----------------------------------------------------

    def _build_ui(self) -> None:
        self.grid = GridView()
        self.grid.video_activated.connect(self.open_video)
        self.grid.enqueue_requested.connect(self.enqueue_video)
        self.grid.play_requested.connect(self.play_from)

        self.list = ListView()
        self.list.video_activated.connect(self.open_video)
        self.list.enqueue_requested.connect(self.enqueue_video)
        self.list.chapter_activated.connect(
            lambda video_id, index: self.play_from(video_id, index, self.default_audio_only())
        )
        self.list.play_requested.connect(self.play_from)
        self.list.chapter_enqueue_requested.connect(
            lambda video_id, index: self.enqueue_chapters(video_id, [index])
        )
        self.grid.menu_extender = self._add_folder_actions
        self.list.menu_extender = self._add_folder_actions
        for view in (self.grid, self.list):
            view.video_choices = self._other_video_places
            view.play_video_at.connect(
                lambda video_id, index, in_app: self.play_from(video_id, index, False, in_app)
            )
        self.list.protection_of = lambda video_id, video: (
            protection.label(self._lib(video_id), video),
            protection.tip(self._lib(video_id), video),
        )

        self.shelf = QStackedWidget()
        self.shelf.addWidget(self.grid)
        self.shelf.addWidget(self.list)

        self.detail = DetailView()
        self.detail.back_requested.connect(self.show_grid)
        self.detail.play_requested.connect(self._play_selected_chapter)
        self.detail.enqueue_requested.connect(self._enqueue_open_chapters)
        # Hiding the video that is open would leave the page showing
        # something that is no longer on the shelf, so only the folder is
        # offered here.
        self.detail.menu_extender = (
            lambda menu, video_id: self._add_folder_actions(menu, video_id, allow_hiding=False)
        )
        self.detail.protection_of = lambda video_id, video: (
            protection.is_locked(self._lib(video_id), video),
            protection.label(self._lib(video_id), video),
        )
        self.detail.video_choices = self._other_video_places
        self.detail.set_default_audio_only(self.default_audio_only())
        self.detail.play_video_at.connect(
            lambda index, in_app: self._play_selected_chapter(index, False, in_app)
        )
        self.detail.rename_requested.connect(self.rename_chapter)
        self.detail.checked_requested.connect(self.mark_name_checked)
        self.detail.chapters_requested.connect(self.chapters_and_names)
        self.detail.edit_requested.connect(self.edit_tracklist)
        self.detail.reset_requested.connect(self.reset_chapters)
        self.detail.split_requested.connect(self.split_at_playhead)
        self.detail.mark_requested.connect(self.edit_chapters)
        self.detail.merge_requested.connect(self.merge_with_next)
        self.detail.nudge_requested.connect(self.nudge_chapter_start)

        self.pages = QStackedWidget()
        self.pages.addWidget(self.shelf)
        self.pages.addWidget(self.detail)

        self.empty_label = QLabel(
            "No videos here yet.\n\nChoose a folder to scan, and every video and "
            "Blu-ray disc inside it will appear on this shelf."
        )
        self.empty_label.setObjectName("placeholder")
        self.empty_label.setAlignment(Qt.AlignCenter)
        self.empty_label.setTextFormat(Qt.PlainText)
        self.pages.addWidget(self.empty_label)

        # Manual Edit: the video in the main area, where the chapters were.
        self.editor = ChapterEditor()
        self.editor.saved.connect(self._on_editor_saved)
        self.editor.closed.connect(self._on_editor_closed)
        self.pages.addWidget(self.editor)

        # Video playing in the app.
        self.video_page = VideoPage()
        self.video_page.back_requested.connect(self._leave_video_page)
        self.video_page.fullscreen_requested.connect(self.toggle_fullscreen)
        self.video_page.leave_fullscreen_requested.connect(lambda: self.set_fullscreen(False))
        self.video_page.play_pause_requested.connect(self.toggle_play_pause)
        self.video_page.jump_requested.connect(self._nudge)
        self.video_page.screenshot_requested.connect(self.take_screenshot)
        self.video_page.cycle_requested.connect(self.cycle_track)
        self.pages.addWidget(self.video_page)
        self.pages.currentChanged.connect(lambda _index: self._update_video_controls())
        self._pointer_watch = QTimer(self)
        self._pointer_watch.setInterval(POINTER_WATCH_MS)
        self._pointer_watch.timeout.connect(self._watch_pointer)

        self.now_playing_bar = NowPlayingBar()
        self.now_playing_bar.play_pause_requested.connect(self.toggle_play_pause)
        self.now_playing_bar.next_requested.connect(lambda: self._skip(1))
        self.now_playing_bar.previous_requested.connect(lambda: self._skip(-1))
        self.now_playing_bar.restart_requested.connect(self.restart_chapter)
        self.now_playing_bar.stop_requested.connect(self.stop_playback)
        self.now_playing_bar.seek_requested.connect(self.seek)
        self.now_playing_bar.show_video_requested.connect(self.show_video_page)
        self.now_playing_bar.fullscreen_requested.connect(self.toggle_fullscreen)
        self.now_playing_bar.move_requested.connect(self.move_video)

        self._poll = QTimer(self)
        self._poll.setInterval(POLL_MS)
        self._poll.timeout.connect(self._tick)

        centre_layout = QVBoxLayout()
        centre_layout.setContentsMargins(0, 0, 0, 0)
        self._centre_margins = (0, 0, 0, 0)
        centre_layout.setSpacing(10)
        centre_layout.addWidget(self.pages, 1)
        centre_layout.addWidget(self.now_playing_bar)

        centre = QWidget()
        centre.setObjectName("centre")
        centre.setLayout(centre_layout)
        self.setCentralWidget(centre)

        self.libraries = LibraryPanel()
        self.libraries.library_chosen.connect(self.load_root)
        self.libraries.libraries_chosen.connect(self.show_libraries)
        self.libraries.add_requested.connect(self.choose_folder)
        self.libraries.network_requested.connect(self.add_network_folder)
        self.libraries.flag_toggled.connect(self.set_library_flag)
        self.libraries.reset_requested.connect(self.reset_library)
        left = QDockWidget("Libraries", self)
        left.setObjectName("librariesDock")
        left.setWidget(self.libraries)
        left.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea)
        self.addDockWidget(Qt.LeftDockWidgetArea, left)
        self.libraries_dock = left

        self.queue_panel = QueuePanel()
        self.queue_panel.entry_activated.connect(self.play_queue_entry)
        self.queue_panel.audio_only_toggled.connect(self.set_queue_audio_only)
        self.queue_panel.entries_removed.connect(self.remove_from_queue)
        self.queue_panel.order_changed.connect(self.reorder_queue)
        self.queue_panel.playlists_menu = self._fill_playlists_menu
        self.queue_panel.cleared.connect(self.clear_queue)
        self.queue_panel.shuffle_toggled.connect(self._set_shuffle)
        self.queue_panel.repeat_changed.connect(self._set_repeat)
        right = QDockWidget("Queue", self)
        right.setObjectName("queueDock")
        right.setWidget(self.queue_panel)
        right.setAllowedAreas(Qt.RightDockWidgetArea | Qt.LeftDockWidgetArea)
        self.addDockWidget(Qt.RightDockWidgetArea, right)
        self.queue_dock = right
        self.resizeDocks([left, right], [240, 280], Qt.Horizontal)

        self._build_toolbar()
        self._build_status_bar()

    def _build_toolbar(self) -> None:
        bar = QToolBar("Main")
        bar.setMovable(False)
        self.addToolBar(bar)

        self.rescan_action = QAction("Rescan", self)
        self.rescan_action.setToolTip("Re-read this folder, keeping the names you've given")
        self.rescan_action.triggered.connect(lambda: self.rescan())
        bar.addAction(self.rescan_action)

        # Not on the toolbar: the Libraries panel has it, and the File menu.
        self.choose_action = QAction("Add Folder…", self)
        self.choose_action.triggered.connect(self.choose_folder)

        self.network_action = QAction("Add Network Folder…", self)
        self.network_action.setToolTip(
            "Add a folder on a network share (smb://, sftp://...) your file manager can reach"
        )
        self.network_action.triggered.connect(self.add_network_folder)

        self.grid_button = QToolButton()
        self.grid_button.setText("Tiles")
        self.grid_button.setToolTip("Show the library as covers")
        self.grid_button.setCheckable(True)
        self.grid_button.setChecked(self._preferred_view == SHELF_GRID)
        self.grid_button.clicked.connect(lambda: self.set_view(SHELF_GRID))
        self.list_button = QToolButton()
        self.list_button.setText("List")
        self.list_button.setToolTip("Show the library as a list")
        self.list_button.setCheckable(True)
        self.list_button.setChecked(self._preferred_view == SHELF_LIST)
        self.list_button.clicked.connect(lambda: self.set_view(SHELF_LIST))
        bar.addWidget(self.grid_button)
        bar.addWidget(self.list_button)

        self.sort_button = QToolButton()
        self.sort_button.setText("Sort: Name")
        self.sort_button.setToolTip("How the shelf is ordered")
        self.sort_button.setPopupMode(QToolButton.InstantPopup)
        sort_menu = QMenu(self.sort_button)
        for key, label, _ in SORT_MODES:
            action = QAction(label, sort_menu)
            action.triggered.connect(
                lambda _checked=False, k=key, text=label: self._choose_sort(k, text)
            )
            sort_menu.addAction(action)
        self.sort_button.setMenu(sort_menu)
        bar.addWidget(self.sort_button)

        self.show_hidden_check = QCheckBox("Show hidden")
        self.show_hidden_check.setToolTip(
            "Show videos you've hidden, so they can be played or unhidden"
        )
        self.show_hidden_check.toggled.connect(self.set_show_hidden)
        bar.addWidget(self.show_hidden_check)

        self.needs_work_check = QCheckBox("Needs identifying")
        self.needs_work_check.setToolTip(
            "Show only videos whose chapters aren't all named yet, or that haven't "
            "been split into songs"
        )
        self.needs_work_check.toggled.connect(lambda _on: self.refresh_library())
        bar.addWidget(self.needs_work_check)

        bar.addSeparator()

        self.search = QLineEdit()
        self.search.setObjectName("searchField")
        self.search.setPlaceholderText("Search videos and chapters…")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._on_search_typed)
        bar.addWidget(self.search)

        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(SEARCH_DEBOUNCE_MS)
        self._search_timer.timeout.connect(self.refresh_library)

    def set_view(self, mode: int, remember: bool = True) -> None:
        """Switch the shelf between covers and list.

        `remember` is what keeps a search from overwriting the choice: a
        search forces the list, and clearing it puts back whichever view was
        being used before. A remembered choice is also saved, so the app
        reopens on it.
        """
        if remember and mode != self._preferred_view:
            self._preferred_view = mode
            settings = store.load_app_settings()
            settings[VIEW_SETTING] = VIEW_NAMES[mode]
            store.save_app_settings(settings)
        self.shelf.setCurrentIndex(mode)
        self.grid_button.setChecked(mode == SHELF_GRID)
        self.list_button.setChecked(mode == SHELF_LIST)

    def _choose_sort(self, key: str, label: str) -> None:
        self.sort_button.setText(f"Sort: {label}")
        self.set_sort(key)

    def _build_status_bar(self) -> None:
        self.status_label = QLabel("Ready.")
        self.status_label.setObjectName("statusLabel")
        self.status_label.setTextFormat(Qt.PlainText)

        self.progress = QProgressBar()
        self.progress.setRange(0, 0)  # indeterminate; a scan has no known length
        self.progress.setMaximumWidth(160)
        self.progress.hide()

        # Picking the wrong folder - a whole drive, say - starts a scan that
        # can run for a very long time to no purpose.
        self.cancel_scan_button = QPushButton("Cancel")
        self.cancel_scan_button.setToolTip("Stop scanning. Nothing already stored is changed.")
        self.cancel_scan_button.clicked.connect(self.cancel_scan)
        self.cancel_scan_button.hide()

        bar = self.statusBar()
        bar.addWidget(self.status_label, 1)
        bar.addPermanentWidget(self.progress)
        bar.addPermanentWidget(self.cancel_scan_button)

    def _set_status(self, text: str, state: str = "") -> None:
        """Say something in the status bar, in the colour it deserves."""
        self.status_label.setText(text)
        self.status_label.setProperty("state", state)
        # A changed property needs the style re-applied to take effect.
        self.status_label.style().unpolish(self.status_label)
        self.status_label.style().polish(self.status_label)

    def _build_actions(self) -> None:
        menu = self.menuBar()

        file_menu = menu.addMenu("&File")
        file_menu.addAction(self.choose_action)
        file_menu.addAction(self.network_action)
        file_menu.addAction(self.rescan_action)
        self.rescan_action.setShortcut(QKeySequence("Ctrl+R"))
        # A rescan keeps videos whose files are away (a dropped network
        # share must not cost their names), so letting go of ones that are
        # gone for good is its own, deliberate step.
        file_menu.addSeparator()
        export_action = QAction("Export Catalog…", self)
        export_action.setToolTip("Save this library's chapters and names to a file")
        export_action.triggered.connect(self.export_catalog)
        file_menu.addAction(export_action)
        import_action = QAction("Import Catalog…", self)
        import_action.setToolTip(
            "Bring in a catalog made on another device - the same folder, wherever "
            "it is mounted here"
        )
        import_action.triggered.connect(self.import_catalog)
        file_menu.addAction(import_action)
        file_menu.addSeparator()
        self.remove_missing_action = QAction("Remove Missing Videos…", self)
        self.remove_missing_action.triggered.connect(self.remove_missing_videos)
        file_menu.addAction(self.remove_missing_action)
        self.cancel_scan_action = QAction("Cancel Scan", self)
        self.cancel_scan_action.setEnabled(False)
        self.cancel_scan_action.triggered.connect(self.cancel_scan)
        file_menu.addAction(self.cancel_scan_action)
        file_menu.addSeparator()
        identify_action = QAction("Identify Library…", self)
        identify_action.setShortcut(QKeySequence("Ctrl+I"))
        identify_action.setToolTip("Name every video that still needs it, unattended")
        identify_action.triggered.connect(self.identify_library)
        file_menu.addAction(identify_action)
        self.undo_identify_action = QAction("Undo Last Identification", self)
        self.undo_identify_action.setToolTip(
            "Put back every video the last Identify Library run changed"
        )
        self.undo_identify_action.triggered.connect(self._undo_identify_from_menu)
        file_menu.addAction(self.undo_identify_action)
        file_menu.addSeparator()
        reset_action = QAction("Reset Library to Defaults…", self)
        reset_action.setToolTip(
            "Clear everything added to a library - names, chapters, hidden folders - "
            "and read it afresh; can be undone"
        )
        reset_action.triggered.connect(self.reset_library_on_show)
        file_menu.addAction(reset_action)
        self.undo_reset_action = QAction("Undo Reset", self)
        self.undo_reset_action.setToolTip("Put back what the last reset cleared")
        self.undo_reset_action.triggered.connect(self.undo_reset)
        file_menu.addAction(self.undo_reset_action)

        def before_file_menu():
            busy = self.identifying() or self._scanning
            self.undo_identify_action.setEnabled(self.can_undo_identify() and not busy)
            undo = self._last_reset()
            self.undo_reset_action.setEnabled(undo is not None and not busy)
            self.undo_reset_action.setText(
                f"Undo {reset.describe(undo[1])}" if undo else "Undo Reset"
            )
            reset_action.setEnabled(not busy)

        file_menu.aboutToShow.connect(before_file_menu)
        file_menu.addSeparator()
        ai_settings_action = QAction("AI Settings…", self)
        ai_settings_action.setToolTip("The Nano-GPT key and model used to look at videos")
        ai_settings_action.triggered.connect(self.ai_settings)
        file_menu.addAction(ai_settings_action)
        file_menu.addSeparator()
        quit_action = QAction("&Quit", self)
        quit_action.setShortcut(QKeySequence.Quit)
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

        view_menu = menu.addMenu("&View")
        focus_search = QAction("Search", self)
        focus_search.setShortcut(QKeySequence.Find)
        focus_search.triggered.connect(self.search.setFocus)
        view_menu.addAction(focus_search)

        tiles_action = QAction("Tiles", self)
        tiles_action.setShortcut(QKeySequence("Ctrl+1"))
        tiles_action.triggered.connect(lambda: self.set_view(SHELF_GRID))
        view_menu.addAction(tiles_action)

        list_action = QAction("List", self)
        list_action.setShortcut(QKeySequence("Ctrl+2"))
        list_action.triggered.connect(lambda: self.set_view(SHELF_LIST))
        view_menu.addAction(list_action)
        view_menu.addSeparator()
        # Either panel can be closed; these bring it back.
        for dock in (self.libraries_dock, self.queue_dock):
            view_menu.addAction(dock.toggleViewAction())
        view_menu.addSeparator()

        back = QAction("Back to shelf", self)
        back.setShortcut(QKeySequence(Qt.Key_Escape))
        back.triggered.connect(self.show_grid)
        view_menu.addAction(back)

        play_menu = menu.addMenu("&Playback")
        # Ctrl+Space rather than bare Space: a shortcut takes the key before
        # the focused widget sees it, so plain Space would be swallowed
        # whenever anyone typed it into the search box.
        for label, sequence, slot in (
            ("Play / Pause", "Ctrl+Space", self.toggle_play_pause),
            ("Next Chapter", "Ctrl+Shift+Right", lambda: self._skip(1)),
            ("Previous Chapter", "Ctrl+Shift+Left", lambda: self._skip(-1)),
            ("Forward 10s", "Ctrl+Right", lambda: self._nudge(SEEK_STEP_SECONDS)),
            ("Back 10s", "Ctrl+Left", lambda: self._nudge(-SEEK_STEP_SECONDS)),
            ("Stop", "Ctrl+.", self.stop_playback),
        ):
            action = QAction(label, self)
            action.setShortcut(QKeySequence(sequence))
            action.triggered.connect(slot)
            play_menu.addAction(action)
        play_menu.addSeparator()
        self.in_app_action = QAction("Play Video in the App", self)
        self.in_app_action.setCheckable(True)
        self.in_app_action.setChecked(self.video_in_app())
        if can_embed():
            self.in_app_action.setToolTip(
                "Video plays in the main area, with a Fullscreen button; unticked, in "
                "mpv's own window"
            )
        else:
            self.in_app_action.setEnabled(False)
            self.in_app_action.setToolTip(
                "Only on X11 or XWayland - the app is running on Wayland itself, so video "
                "plays in mpv's own window"
            )
        self.in_app_action.toggled.connect(self.set_video_in_app)
        play_menu.addAction(self.in_app_action)
        default_menu = play_menu.addMenu("Play by Default")
        default_menu.setToolTip(
            "What a double-click plays a chapter as, what the main Play button does, and "
            "what Add to Queue starts an empty queue as"
        )
        self.default_group = QActionGroup(self)
        self.default_actions = {}
        for label, audio_only in (("Audio", True), ("Video", False)):
            action = QAction(label, self.default_group)
            action.setCheckable(True)
            action.setChecked(audio_only == self.default_audio_only())
            action.triggered.connect(
                lambda _checked=False, a=audio_only: self.set_default_audio_only(a)
            )
            default_menu.addAction(action)
            self.default_actions[audio_only] = action
        self.show_video_action = QAction("Show Video", self)
        self.show_video_action.setShortcut(QKeySequence("Ctrl+Shift+V"))
        self.show_video_action.setToolTip("Back to the video playing in the app")
        self.show_video_action.setEnabled(False)
        self.show_video_action.triggered.connect(self.show_video_page)
        play_menu.addAction(self.show_video_action)
        fullscreen_action = QAction("Fullscreen", self)
        fullscreen_action.setShortcut(QKeySequence(Qt.Key_F11))
        fullscreen_action.setToolTip("The video playing in the app, filling the screen")
        fullscreen_action.triggered.connect(self.toggle_fullscreen)
        play_menu.addAction(fullscreen_action)
        self.move_video_action = QAction("Pop Out to mpv's Window", self)
        self.move_video_action.setShortcut(QKeySequence("Ctrl+Shift+P"))
        self.move_video_action.setEnabled(False)
        self.move_video_action.triggered.connect(self.move_video)
        play_menu.addAction(self.move_video_action)
        play_menu.addSeparator()
        playlists_menu = play_menu.addMenu("Playlists")
        playlists_menu.aboutToShow.connect(
            lambda: (playlists_menu.clear(), self._fill_playlists_menu(playlists_menu))
        )

        # For fixing boundaries while a video plays: pause where a song
        # really starts and split, without reaching for the mouse.
        chapters_menu = menu.addMenu("&Chapters")
        for label, sequence, slot in (
            ("Split at Playhead", "Ctrl+K", self.split_at_playhead),
            ("Merge with Next", "Ctrl+M", self._merge_selected),
            ("Move Start Earlier", "Ctrl+[", lambda: self._nudge_selected(-1.0)),
            ("Move Start Later", "Ctrl+]", lambda: self._nudge_selected(1.0)),
        ):
            action = QAction(label, self)
            action.setShortcut(QKeySequence(sequence))
            action.triggered.connect(slot)
            chapters_menu.addAction(action)
        chapters_menu.addSeparator()
        names_action = QAction("Detect Chapters…", self)
        names_action.triggered.connect(self.chapters_and_names)
        chapters_menu.addAction(names_action)
        mark_action = QAction("Manual Edit…", self)
        mark_action.setShortcut(QKeySequence("Ctrl+Shift+K"))
        mark_action.setToolTip(
            "Play the video here and set its chapters and names by hand"
        )
        mark_action.triggered.connect(self.edit_chapters)
        chapters_menu.addAction(mark_action)
        translate_action = QAction("Romanise Titles with AI…", self)
        translate_action.setToolTip(
            "Give this video's chapter titles as the songs are officially known in "
            "English-language releases - usually romanised (Iine!), never translated "
            "(not \"So Good\"). The originals are kept for searching. One AI request as "
            "soon as it opens - a fraction of a cent."
        )
        translate_action.triggered.connect(self.translate_titles)
        chapters_menu.addAction(translate_action)
        chapters_menu.addAction(self.detail.reset_action)

        help_menu = menu.addMenu("&Help")
        about_action = QAction("About DA Media Browser…", self)
        about_action.setMenuRole(QAction.AboutRole)
        about_action.triggered.connect(self.show_about)
        help_menu.addAction(about_action)

        # The keys on a keyboard that already mean this.
        for key, slot in (
            (Qt.Key_MediaTogglePlayPause, self.toggle_play_pause),
            (Qt.Key_MediaPlay, self.toggle_play_pause),
            (Qt.Key_MediaPause, self.toggle_play_pause),
            (Qt.Key_MediaNext, lambda: self._skip(1)),
            (Qt.Key_MediaPrevious, lambda: self._skip(-1)),
            (Qt.Key_MediaStop, self.stop_playback),
        ):
            action = QAction(self)
            action.setShortcut(QKeySequence(key))
            action.triggered.connect(slot)
            self.addAction(action)

    def show_about(self) -> None:
        from mediabrowser.gui.dialogs.about_dialog import show_about

        show_about(self)

    # --- library ---------------------------------------------------------

    def _hidden_ids(self) -> set[str]:
        hidden = [folder for data in self.shown.libraries
                  for folder in folders.hidden_folders(data)]
        return {
            video_id for video_id, video in self.data["videos"].items()
            if folders.is_hidden(video, hidden)
        }

    def _sorted_videos(self, hidden_ids=None) -> list[tuple[str, dict]]:
        text = self.search.text().strip().lower()
        if hidden_ids is None:
            hidden_ids = self._hidden_ids()
        videos = [
            (video_id, video)
            for video_id, video in self.data["videos"].items()
            if (self._show_hidden or video_id not in hidden_ids)
            and utils.matches_search(video, video["chapters"], text)
            and (not self.needs_work_check.isChecked() or naming.status(video).needs_work)
        ]
        videos.sort(key=self._sort[2])
        return videos

    def set_show_hidden(self, on: bool) -> None:
        self._show_hidden = on
        if self.show_hidden_check.isChecked() != on:
            self.show_hidden_check.setChecked(on)
        self.refresh_library()

    def set_sort(self, key: str) -> None:
        for mode in SORT_MODES:
            if mode[0] == key:
                self._sort = mode
                break
        self.refresh_library()

    def refresh_library(self) -> None:
        search = self.search.text().strip().lower()
        hidden_ids = self._hidden_ids()
        videos = self._sorted_videos(hidden_ids)
        self._missing = set(library.missing_videos(self.data))
        # Only shown videos can be marked; with hidden ones filtered out
        # there is nothing to mark.
        marked = hidden_ids if self._show_hidden else set()
        keep = search == self._shelf_search
        self._shelf_search = search

        self.grid.populate(videos, self._missing, marked, keep_position=keep)
        self.list.populate(videos, self._missing, search, marked, keep_position=keep)

        # A search is a question about chapters, and only the list can answer
        # it - so searching switches to it, and clearing puts back whichever
        # view was in use before.
        self.set_view(SHELF_LIST if search else self._preferred_view, remember=False)

        if not self.data["videos"]:
            self.pages.setCurrentIndex(PAGE_EMPTY)
        elif self.pages.currentIndex() == PAGE_EMPTY:
            self.pages.setCurrentIndex(PAGE_SHELF)

        self._resolve_artwork()

        roots = self.shown.roots()
        self.libraries.refresh(roots)
        if roots:
            total = len(self.data["videos"])
            shown = len(videos)
            suffix = "" if shown == total else f" ({shown} shown)"
            names = (Path(roots[0]).name or roots[0]) if len(roots) == 1 else (
                f"{len(roots)} libraries"
            )
            text = f"{names} - {total} video{'' if total == 1 else 's'}{suffix}"
            state = ""
            waiting = sum(
                1 for video_id, video in self.data["videos"].items()
                if video_id not in hidden_ids and naming.status(video).needs_work
            )
            if waiting:
                text += f" · {waiting} to identify"
            if hidden_ids:
                text += f" · {len(hidden_ids)} hidden"
            if self._missing:
                text += f" · {len(self._missing)} missing"
                state = "warning"
            self._set_status(text, state)

    def _resolve_artwork(self) -> None:
        """Find covers for anything that hasn't been looked at yet.

        Each one shells out to ffmpeg, so this runs on a worker thread and
        reports videos one at a time - the grid fills in as they arrive
        rather than waiting for the whole library.
        """
        # A private video's release isn't looked up online, not even for
        # its cover: the Cover Art Archive is MusicBrainz's.
        pending = [
            (video_id, video,
             None if protection.is_private(self._lib(video_id), video)
             else video.get("musicbrainz_release_id"))
            for video_id, video in self.data["videos"].items()
            if not artwork.is_resolved(video_id)
        ]
        if not pending or self._artwork_job is not None:
            return

        def work(progress_cb):
            for video_id, video, release_id in pending:
                if artwork.find(video_id, video, release_id):
                    progress_cb(video_id)

        self._artwork_job = run_job(
            self,
            work,
            wants_progress=True,
            on_progress=self._set_cover,
            on_done=lambda _result: self._artwork_finished(),
            on_failed=lambda _message: self._artwork_finished(),
        )

    def _set_cover(self, video_id: str) -> None:
        """A cover has been found; both shelf views show it."""
        self.grid.set_cover(video_id)
        self.list.set_cover(video_id)

    def _artwork_finished(self) -> None:
        self._artwork_job = None
        current = self.current_video()
        if current is not None:
            self.detail.show_video(current[0], current[1], keep_selection=True)

    def load_root(self, root: str) -> None:
        """Show this library alone."""
        self.show_libraries([root])

    @staticmethod
    def _prepared(data: dict) -> dict:
        """A library as loaded, brought up to date with what's newer than
        it: single-chapter videos named after their files."""
        if data["settings"].get("library_root") and library.name_single_chapters(data):
            store.save_library(data)
        return data

    def choose_folder(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Choose media library folder")
        if path:
            self.rescan(path)

    def add_network_folder(self) -> None:
        from mediabrowser.gui.dialogs.network_dialog import NetworkFolderDialog

        if self._scanning:
            return
        dialog = NetworkFolderDialog(self)
        if dialog.exec() and dialog.result_folder():
            path, uri = dialog.result_folder()
            self.rescan(path, network_uri=uri)

    def rescan(self, root: str | None = None, network_uri: str | None = None) -> None:
        if self._scanning:
            return
        if self.identifying():
            # The run is writing into this library as it goes; a rescan
            # would swap it out underneath.
            self._set_status("Identify Library is running - rescan when it's done.", "warning")
            return
        # A folder given (Add Folder) is scanned and shown alone; otherwise
        # every library on show is rescanned.
        explicit = root is not None
        roots = [root] if explicit else self.shown.roots()
        if not roots:
            QMessageBox.information(
                self, "No folder chosen", "Choose a library folder to scan first."
            )
            return

        self._scanning = True
        self._scan_cancel = cancel = threading.Event()
        self._set_scan_controls(False)
        self.progress.show()
        self.cancel_scan_button.setEnabled(True)
        self.cancel_scan_button.show()
        self._set_status("Scanning…")

        def scan(root, progress_cb):
            target = root
            if not network_uri:
                # A library added from a network share whose folder has gone
                # (the share dropped, or KDE put it somewhere new this
                # session) is reconnected before it is scanned.
                uri = library.network_uri_for(root)
                if uri and not Path(root).is_dir():
                    progress_cb(f"Reconnecting to {network.display_name(uri)}…")
                    target = library.reconnect(root, network.resolve)
            if not store.has_library(target):
                # A folder new to this device may be one a catalog was made
                # of elsewhere - copied across with the app's data, from a
                # device that mounts the share at another path.
                progress_cb("Looking for this folder's catalog…")
                found = catalog.find_moved(target, network_uri)
                if found is not None:
                    progress_cb(f"Carrying over the catalog made at {found.old_root}…")
                    catalog.adopt(found)
                    self._adopted = found
            return library.rescan(
                target, progress_cb=progress_cb, cancel=cancel, network_uri=network_uri
            )

        def work(progress_cb):
            return explicit, [scan(root, progress_cb) for root in roots]

        thread_job = run_job(
            self,
            work,
            wants_progress=True,
            on_progress=self._on_scan_progress,
            on_done=self._on_scan_done,
            on_failed=self._on_scan_failed,
        )
        self._jobs.append(thread_job)

    def _on_scan_progress(self, text: str) -> None:
        if self._scan_cancel is not None and self._scan_cancel.is_set():
            return
        self._set_status(text if text.endswith("…") else f"Scanning: {text}")

    def cancel_scan(self) -> None:
        if not self._scanning or self._scan_cancel is None:
            return
        self._scan_cancel.set()
        self.cancel_scan_button.setEnabled(False)
        self._set_status("Cancelling…")

    def _on_scan_done(self, result) -> None:
        explicit, scanned = result
        if explicit:
            self.shown = Shelf(scanned)
        else:
            for data in scanned:
                self.shown.replace(data)
        self._remember_shown()
        self._end_scan()
        self.show_grid()
        self.refresh_library()
        count = sum(len(data["videos"]) for data in scanned)
        text = f"Done. {count} video{'' if count == 1 else 's'} found."
        adopted, self._adopted = self._adopted, None
        if adopted is not None:
            text += (f" Its names came across from the catalog made at {adopted.old_root} "
                     f"({adopted.found} of {adopted.looked} videos checked were here).")
        self._set_status(text)

    def _on_scan_failed(self, message: str) -> None:
        cancelled = self._scan_cancel is not None and self._scan_cancel.is_set()
        self._end_scan()
        if cancelled:
            self._set_status("Scan cancelled - nothing was changed.")
            return
        self._set_status("Not rescanned - nothing was changed.", "error")
        QMessageBox.warning(self, "Couldn't rescan", message)

    def _end_scan(self) -> None:
        self._scanning = False
        self._scan_cancel = None
        self.progress.hide()
        self.cancel_scan_button.hide()
        self._set_scan_controls(True)

    def _set_scan_controls(self, enabled: bool) -> None:
        self.rescan_action.setEnabled(enabled)
        self.choose_action.setEnabled(enabled)
        self.network_action.setEnabled(enabled)
        self.cancel_scan_action.setEnabled(not enabled)

    def _on_search_typed(self) -> None:
        self._search_timer.start()

    # --- navigation ------------------------------------------------------

    def show_grid(self) -> None:
        if self._fullscreen:
            # Esc in fullscreen leaves fullscreen, and nothing more.
            self.set_fullscreen(False)
            return
        if self.editor.active():
            # Esc while editing leaves the editor, not the video.
            self.editor.request_close()
            return
        if self.data["videos"]:
            self.pages.setCurrentIndex(PAGE_SHELF)

    def open_video(self, video_id: str) -> None:
        video = self.data["videos"].get(video_id)
        if video is None:
            return
        self._open_video_id = video_id
        self.detail.show_video(video_id, video)
        self.pages.setCurrentIndex(PAGE_DETAIL)

    def current_video(self) -> tuple[str, dict] | None:
        video_id = self._open_video_id
        if video_id is None:
            return None
        video = self.data["videos"].get(video_id)
        return (video_id, video) if video is not None else None

    # --- playback --------------------------------------------------------

    def play_from(self, video_id: str, chapter_index: int, audio_only: bool | None = None,
                  in_app: bool | None = None) -> None:
        """Play one chapter, and line the rest of its video up behind it.
        As audio or video as asked, else as the setting says.

        Starting a track part-way down an album and having it stop at the
        end of that one track is not what anybody means by Play.
        """
        video = self.data["videos"].get(video_id)
        if video is None:
            return
        if audio_only is None:
            audio_only = self.default_audio_only()
        self.queue.set_entries(
            playback.queue_entries_for(video_id, video, audio_only), start=chapter_index
        )
        self._refresh_queue()
        self.play_chapter(video_id, chapter_index, audio_only, in_app=in_app)

    def _play_selected_chapter(self, chapter_index: int, audio_only: bool,
                               in_app: bool | None = None) -> None:
        current = self.current_video()
        if current is not None:
            self.play_from(current[0], chapter_index, audio_only, in_app)

    def _other_video_places(self) -> list[tuple[str, bool]]:
        """Where else Play Video can play than where it does by default."""
        if not can_embed():
            return []
        if self.video_in_app():
            return [("Play Video in mpv's Own Window", False)]
        return [("Play Video in the App", True)]

    def enqueue_video(self, video_id: str, audio_only: bool | None = None) -> None:
        """Add every chapter of a video to the end of the queue."""
        video = self.data["videos"].get(video_id)
        if video is None:
            return
        self.enqueue_chapters(video_id, range(len(video["chapters"])), audio_only)

    def enqueue_chapters(self, video_id: str, indices, audio_only: bool | None = None) -> None:
        """Add some of a video's chapters to the end of the queue, in the
        order given, playing as the queue does. Into an empty one, as audio
        or video as asked, else as the setting says - and starts playing.
        """
        video = self.data["videos"].get(video_id)
        if video is None:
            return
        if audio_only is None:
            audio_only = self.default_audio_only()
        every = playback.queue_entries_for(video_id, video, audio_only)
        entries = [every[i] for i in indices if 0 <= i < len(every)]
        if not entries:
            return
        was_empty = self.queue.is_empty()
        self.queue.append(entries)
        self._queue_edited()
        if was_empty:
            entry = self.queue.current()
            if entry is not None:
                self.play_chapter(entry.video_id, entry.chapter_index, entry.audio_only)
        elif len(entries) == len(every) and len(every) > 1:
            self._set_status(f"Queued {video['display_name']}.")
        elif len(entries) == 1:
            self._set_status(f"Queued {entries[0].title}.")
        else:
            self._set_status(f"Queued {len(entries)} chapters of {video['display_name']}.")

    def _enqueue_open_chapters(self, indices) -> None:
        current = self.current_video()
        if current is not None:
            self.enqueue_chapters(current[0], indices)

    # --- queue -----------------------------------------------------------

    def _refresh_queue(self) -> None:
        self.queue_panel.show_queue(self.queue.entries(), self.queue.current_index())
        self.queue_panel.set_audio_only(self.queue.audio_only)

    def _queue_edited(self) -> None:
        """The queue changed: show it, and let what's playing follow."""
        self._refresh_queue()
        if self.session.running():
            self.session.queue_changed()

    def play_queue_entry(self, entry_index: int) -> None:
        entry = self.queue.jump_to(entry_index)
        if entry is None:
            return
        self._refresh_queue()
        self.play_chapter(entry.video_id, entry.chapter_index, entry.audio_only)

    def set_queue_audio_only(self, audio_only: bool) -> None:
        """The whole queue as audio or as video - what's playing too, going
        on from the same moment: video where it plays by default."""
        if not self.queue.set_audio_only(audio_only):
            return
        self._refresh_queue()
        self._set_status("The queue plays as audio." if audio_only
                         else "The queue plays as video.")
        if self.now_playing is None or not self.session.running():
            return
        in_app = not audio_only and self.video_in_app()
        if self._fullscreen:
            self.set_fullscreen(False)
        try:
            segment = self.session.switch(self.video_page.window_id() if in_app else None)
        except OSError as exc:
            QMessageBox.critical(self, "Playback failed", f"Could not start mpv: {exc}")
            return
        if segment is None:
            self.stop_playback()
            return
        self._last_duration = None
        self._refresh_queue()
        self._show_now_playing()
        if in_app:
            self.show_video_page()
        elif self.pages.currentIndex() == PAGE_VIDEO:
            self.pages.setCurrentIndex(self._page_before_video)
            self._update_video_controls()

    def remove_from_queue(self, entry_indices) -> None:
        if isinstance(entry_indices, int):
            entry_indices = [entry_indices]
        playing = self.queue.current_index() if self.now_playing is not None else None
        self.queue.remove_many(entry_indices)
        # Removing whatever is playing stops it; the queue has moved on, but
        # the sound coming out of the speakers has not.
        if playing in entry_indices:
            self._refresh_queue()
            self.stop_playback()
        else:
            self._queue_edited()

    def reorder_queue(self, new_order) -> None:
        self.queue.reorder(new_order)
        self._queue_edited()

    def clear_queue(self) -> None:
        self.queue.clear()
        self._refresh_queue()
        self.stop_playback()

    def _set_shuffle(self, on: bool) -> None:
        self.queue.set_shuffle(on)
        self._queue_edited()

    def _set_repeat(self, mode: str) -> None:
        self.queue.repeat = mode
        self._queue_edited()

    # --- playing -----------------------------------------------------------

    def _playable_video(self, video_id: str) -> dict | None:
        """A video the session may play: known, and its file there."""
        if video_id in self._missing:
            return None
        return self.data["videos"].get(video_id)

    def video_in_app(self) -> bool:
        """Whether video plays in the main area rather than mpv's own window."""
        return can_embed() and bool(store.load_app_settings().get(VIDEO_IN_APP_SETTING, True))

    def set_video_in_app(self, on: bool) -> None:
        settings = store.load_app_settings()
        settings[VIDEO_IN_APP_SETTING] = bool(on)
        store.save_app_settings(settings)
        self._set_status(
            "Video will play here, in the app." if on
            else "Video will play in mpv's own window."
        )

    def default_audio_only(self) -> bool:
        """Whether a chapter plays as audio when nothing says which."""
        return bool(store.load_app_settings().get(AUDIO_BY_DEFAULT_SETTING, True))

    def set_default_audio_only(self, audio_only: bool) -> None:
        settings = store.load_app_settings()
        settings[AUDIO_BY_DEFAULT_SETTING] = bool(audio_only)
        store.save_app_settings(settings)
        self.default_actions[bool(audio_only)].setChecked(True)
        self.detail.set_default_audio_only(bool(audio_only))
        self._set_status(
            "Chapters will play as audio by default." if audio_only
            else "Chapters will play as video by default."
        )

    def play_chapter(self, video_id: str, chapter_index: int, audio_only: bool,
                     in_app: bool | None = None) -> None:
        """Play the queue from its current entry - which callers have put on
        this chapter. `in_app` overrides the setting for where video plays."""
        video = self.data["videos"].get(video_id)
        if video is None or not 0 <= chapter_index < len(video["chapters"]):
            self.stop_playback()
            return
        if video_id in self._missing:
            QMessageBox.warning(
                self,
                "File missing",
                f"This file is no longer where it was scanned from:\n\n{video['path']}\n\n"
                "Its chapter names are still stored - rescan the folder once the "
                "file is back, or if it has moved for good.",
            )
            return
        in_app = self.video_in_app() if in_app is None else (in_app and can_embed())
        window_id = self.video_page.window_id() if in_app else None
        try:
            segment = self.session.play(window_id)
        except OSError as exc:
            QMessageBox.critical(self, "Playback failed", f"Could not start mpv: {exc}")
            return
        if segment is None:
            self.stop_playback()
            return
        self._refresh_queue()
        self._show_now_playing()
        self.now_playing_bar.set_paused(False)
        self._poll.start()
        if in_app and not segment.audio_only:
            self.show_video_page()

    def _show_now_playing(self) -> None:
        """The transport bar (and video page) for the queue's current entry."""
        entry = self.queue.current()
        video = self.data["videos"].get(entry.video_id) if entry else None
        if video is None or not 0 <= entry.chapter_index < len(video["chapters"]):
            return
        chapter = video["chapters"][entry.chapter_index]
        self.now_playing = (entry.video_id, entry.chapter_index, entry.audio_only)
        # Audio's bar spans its chapter; video's the whole file, whose length
        # mpv says.
        self._window = (chapter["start"], chapter["end"] if entry.audio_only else None)
        title = utils.chapter_label(entry.chapter_index, chapter)
        self.now_playing_bar.show_playing(
            title=title,
            subtitle=f"{'Audio' if entry.audio_only else 'Video'} · {video['display_name']}",
            video_id=entry.video_id,
            cover_name=video["display_name"],
        )
        self.video_page.set_title(f"{title} · {video['display_name']}")
        if entry.audio_only:
            self.now_playing_bar.set_duration(chapter["end"] - chapter["start"])
        elif self._last_duration:
            self.now_playing_bar.set_duration(self._last_duration)
        self.now_playing_bar.set_chapters(
            [] if entry.audio_only else
            [(chapter["start"], chapter["title"]) for chapter in video["chapters"]]
        )
        self._update_video_controls()
        self._measure_bars()

    def _measure_bars(self) -> None:
        """Measure the black bars of the video playing, and of the one
        handed to mpv next, if they never have been - so they're cropped
        off (letterbox) now, and from the start every time after."""
        for segment in (self.session.current(), self.session.upcoming()):
            if (segment is None or segment.audio_only
                    or segment.video_id in self._measuring_bars
                    or letterbox.measured(segment.video_id)):
                continue
            video = self.data["videos"].get(segment.video_id)
            if video is None:
                continue
            video_id = segment.video_id
            self._measuring_bars.add(video_id)
            self._jobs.append(run_job(
                self, lambda video=dict(video): letterbox.detect(video),
                on_done=lambda crop, video_id=video_id: self._bars_measured(video_id, crop),
                on_failed=lambda _message, video_id=video_id:
                    self._measuring_bars.discard(video_id),
            ))

    def _bars_measured(self, video_id: str, crop) -> None:
        self._measuring_bars.discard(video_id)
        letterbox.remember(video_id, crop)
        if crop is not None and self.session.running():
            self.session.crop_measured(video_id)

    def _video_playing_here(self) -> bool:
        return (self.session.running() and self.session.window_id() is not None
                and self.now_playing is not None and not self.now_playing[2])

    def _video_playing_elsewhere(self) -> bool:
        """Video playing in mpv's own window - which could come into the app."""
        return (can_embed() and self.session.running() and self.session.window_id() is None
                and self.now_playing is not None and not self.now_playing[2])

    def _update_video_controls(self) -> None:
        here = self._video_playing_here()
        elsewhere = self._video_playing_elsewhere()
        self.now_playing_bar.set_video_controls(
            here, self.pages.currentIndex() == PAGE_VIDEO, self._fullscreen, elsewhere,
        )
        self.show_video_action.setEnabled(here)
        self.move_video_action.setEnabled(here or elsewhere)
        self.move_video_action.setText(
            "Bring Video into the App" if elsewhere else "Pop Out to mpv's Window"
        )

    def toggle_play_pause(self) -> None:
        if self.editor.active():
            self.editor.play_pause()
            return
        if self.now_playing is None:
            return
        self.player.toggle_pause()

    def restart_chapter(self) -> None:
        if self.now_playing is None:
            return
        self.play_chapter(*self.now_playing, in_app=self.session.window_id() is not None)

    def stop_playback(self) -> None:
        self._poll.stop()
        self.session.stop()
        self.now_playing = None
        self._last_position = None
        self._last_duration = None
        self.now_playing_bar.clear()
        self.detail.set_playhead(None)
        if self._fullscreen:
            self.set_fullscreen(False)
        if self.pages.currentIndex() == PAGE_VIDEO:
            self.pages.setCurrentIndex(self._page_before_video)

    def playhead_of(self, video_id: str) -> float | None:
        """Where playback of `video_id` has got to, or None while it isn't
        the one playing."""
        if self.now_playing is None or self.now_playing[0] != video_id:
            return None
        return self._last_position

    def seek(self, value: float) -> None:
        """Move playback to `value` seconds into whatever the bar is showing."""
        if self.now_playing is None:
            return
        start, end = self._window
        # For audio the bar spans the chapter, so the slider is an offset from
        # its start; for video it spans the file and is already absolute.
        self.player.seek(start + value if end is not None else value)

    def _nudge(self, delta: float) -> None:
        """Seek relative to where playback has got to."""
        if self.editor.active():
            self.editor.step(delta)
            return
        if self.now_playing is None or self._last_position is None:
            return
        start, end = self._window
        current = self._last_position - start if end is not None else self._last_position
        self.seek(max(0.0, current + delta))

    def _tick(self) -> None:
        """Follow the playing mpv: where it is, and when it moves on."""
        tick = self.session.tick()
        if tick.closed:
            # Its window was closed: that's stop.
            self.stop_playback()
            return
        if tick.finished:
            self.stop_playback()
            self._set_status("Finished.")
            return
        if tick.moved:
            self._refresh_queue()
            self._show_now_playing()
        if tick.position is not None:
            self._last_position = tick.position
            start, end = self._window
            self.now_playing_bar.position_changed(
                tick.position - start if end is not None else tick.position
            )
        self.detail.set_playhead(self.playhead_in_open_video())
        if tick.paused is not None:
            self.now_playing_bar.set_paused(tick.paused)
        if tick.duration and tick.duration != self._last_duration:
            self._last_duration = tick.duration
            if self._window[1] is None:
                self.now_playing_bar.set_duration(tick.duration)

    def playhead_in_open_video(self) -> float | None:
        """Where playback has got to, if the video playing is the one open
        in the detail view. Absolute within the file, as mpv reports it.
        """
        current = self.current_video()
        return self.playhead_of(current[0]) if current is not None else None

    def _skip(self, direction: int) -> None:
        """Step through the queue, which may cross from one video to another."""
        if self.editor.active():
            self.editor.go_to_chapter(direction)
            return
        if self.queue.is_empty():
            return
        entry = self.queue.next() if direction > 0 else self.queue.previous()
        if entry is None:
            return
        self._refresh_queue()
        self.play_chapter(entry.video_id, entry.chapter_index, entry.audio_only,
                          in_app=self.session.window_id() is not None
                          if self.session.running() else None)

    # --- playlists ---------------------------------------------------------------

    def _all_playlists(self) -> list[tuple[dict, dict]]:
        """(library, playlist) for every playlist of every library on show."""
        return [(data, playlist) for data in self.shown.libraries
                for playlist in playlists.all_playlists(data)]

    def _find_playlist(self, name: str, root: str | None = None):
        for data, playlist in self._all_playlists():
            if playlist["name"] == name and (root is None
                                             or data["settings"].get("library_root") == root):
                return data, playlist
        return None, None

    def _fill_playlists_menu(self, menu) -> None:
        """Save the queue as a playlist; and each saved one, to play as it
        was saved, or as audio or as video - here or in mpv's own window - or
        add to the queue."""
        save = menu.addAction("Save Queue as Playlist…")
        save.setEnabled(not self.queue.is_empty())
        save.triggered.connect(self.save_queue_as_playlist)
        saved = self._all_playlists()
        if not saved:
            empty = menu.addAction("No playlists yet")
            empty.setEnabled(False)
            return
        menu.addSeparator()
        embed = can_embed()
        several = self.shown.several()
        for data, playlist in saved:
            name = playlist["name"]
            root = data["settings"].get("library_root")
            count = len(playlist["entries"])
            label = f"{name}  ·  {count} · {utils.format_seconds(playlists.length(playlist))}"
            if several:
                label += f"  ·  {Path(root).name}"
            sub = menu.addMenu(label)
            # Play: as it was saved. The rest say how.
            saved_as = "Audio" if playlists.plays_audio_only(
                playlist, self.default_audio_only()) else "Video"
            choices = [(f"Play ({saved_as})", None, None), ("Play as Audio", True, None)]
            if embed:
                choices += [("Play as Video Here", False, True),
                            ("Play as Video in Its Own Window", False, False)]
            else:
                choices += [("Play as Video", False, False)]
            for text, audio_only, in_app in choices:
                action = sub.addAction(text)
                action.triggered.connect(
                    lambda _checked=False, n=name, a=audio_only, i=in_app, r=root:
                    self.play_playlist(n, a, i, root=r)
                )
            add = sub.addAction("Add to Queue")
            add.triggered.connect(
                lambda _checked=False, n=name, r=root: self.enqueue_playlist(n, root=r)
            )
            sub.addSeparator()
            rename = sub.addAction("Rename…")
            rename.triggered.connect(
                lambda _checked=False, n=name, r=root: self.rename_playlist(n, root=r)
            )
            delete = sub.addAction("Delete…")
            delete.triggered.connect(
                lambda _checked=False, n=name, r=root: self.delete_playlist(n, root=r)
            )

    def save_queue_as_playlist(self) -> None:
        """Saved in the library most of the queue comes from."""
        if self.queue.is_empty():
            return
        name, accepted = QInputDialog.getText(
            self, "Save as Playlist",
            "Name (a playlist of the same name is replaced):", QLineEdit.Normal,
        )
        if not accepted or not name.strip():
            return
        entries = self.queue.entries()
        counts: dict[int, int] = {}
        for entry in entries:
            owner = self._lib(entry.video_id)
            counts[id(owner)] = counts.get(id(owner), 0) + 1
        home = max(self.shown.libraries, key=lambda data: counts.get(id(data), 0))
        playlist = playlists.save(home, name, entries, videos=self.data["videos"],
                                  audio_only=self.queue.audio_only)
        self._save()
        self._set_status(
            f"Saved {len(playlist['entries'])} chapter(s) as the playlist “{playlist['name']}”."
        )

    def _playlist_entries(self, name: str, audio_only: bool | None, root: str | None = None):
        """A playlist's entries: as saved, or as audio or as video."""
        _data, playlist = self._find_playlist(name, root)
        if playlist is None:
            return None
        # Entries are looked for on the whole shelf: a playlist can take in
        # videos of every library on show.
        entries, missing = playlists.queue_entries(
            {"videos": self.data["videos"]}, playlist, audio_only,
            default_audio_only=self.default_audio_only(),
        )
        if missing:
            self._set_status(
                f"{missing} of “{playlist['name']}”'s chapters aren't on the shelf - left "
                "out. (Show the libraries they're in as well to have them.)", "warning",
            )
        return entries

    def play_playlist(self, name: str, audio_only: bool | None = None,
                      in_app: bool | None = None, root: str | None = None) -> None:
        """Replace the queue with a playlist and play it: as saved, or as
        audio or as video."""
        entries = self._playlist_entries(name, audio_only, root)
        if not entries:
            return
        self.queue.set_entries(entries)
        entry = self.queue.current()
        self._refresh_queue()
        self.play_chapter(entry.video_id, entry.chapter_index, entry.audio_only, in_app=in_app)

    def enqueue_playlist(self, name: str, root: str | None = None) -> None:
        """A playlist added to the end of the queue - playing as the queue
        does, or into an empty one, as it was saved."""
        entries = self._playlist_entries(name, None, root)
        if not entries:
            return
        was_empty = self.queue.is_empty()
        self.queue.append(entries)
        self._queue_edited()
        if was_empty:
            entry = self.queue.current()
            self.play_chapter(entry.video_id, entry.chapter_index, entry.audio_only)
        else:
            self._set_status(f"Queued the playlist “{name}”.")

    def rename_playlist(self, name: str, root: str | None = None) -> None:
        data, playlist = self._find_playlist(name, root)
        if playlist is None:
            return
        new, accepted = QInputDialog.getText(
            self, "Rename Playlist", "Name:", QLineEdit.Normal, name
        )
        if not accepted or not new.strip() or new.strip() == name:
            return
        try:
            playlists.rename(data, name, new)
        except ValueError as exc:
            self._set_status(f"Not renamed: {exc}.", "warning")
            return
        self._save()

    def delete_playlist(self, name: str, root: str | None = None) -> None:
        data, playlist = self._find_playlist(name, root)
        if playlist is None:
            return
        answer = QMessageBox.question(
            self, "Delete Playlist",
            f"Delete the playlist “{name}”? The chapters in it aren't touched.",
        )
        if answer == QMessageBox.Yes and playlists.delete(data, name):
            self._save()
            self._set_status(f"Deleted the playlist “{name}”.")

    # --- video in the app ------------------------------------------------------

    def show_video_page(self) -> None:
        if not self._video_playing_here():
            return
        if self.pages.currentIndex() != PAGE_VIDEO:
            self._page_before_video = self.pages.currentIndex()
            self.pages.setCurrentIndex(PAGE_VIDEO)
        self.video_page.setFocus()
        self._update_video_controls()

    def _leave_video_page(self) -> None:
        if self._fullscreen:
            self.set_fullscreen(False)
        self.pages.setCurrentIndex(self._page_before_video)
        self._update_video_controls()

    def move_video(self) -> None:
        """Video playing in the app out into mpv's own window, or playing
        there back into the app - carrying on from the same moment."""
        here = self._video_playing_here()
        if not here and not self._video_playing_elsewhere():
            return
        if self._fullscreen:
            self.set_fullscreen(False)
        try:
            segment = self.session.move_to(None if here else self.video_page.window_id())
        except OSError as exc:
            QMessageBox.critical(self, "Playback failed", f"Could not start mpv: {exc}")
            return
        if segment is None:
            self.stop_playback()
            return
        if here:
            if self.pages.currentIndex() == PAGE_VIDEO:
                self.pages.setCurrentIndex(self._page_before_video)
            self._update_video_controls()
            self._set_status("Video moved to mpv's own window.")
        else:
            self.show_video_page()
            self._set_status("Video moved into the app.")

    def take_screenshot(self) -> None:
        """The picture playing in the app, as it is now, saved as a PNG in
        the screenshot folder."""
        if not self._video_playing_here():
            return
        video = self.data["videos"].get(self.now_playing[0])
        name = video["display_name"] if video else "Screenshot"
        folder = self.player.screenshot_dir
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._set_status(f"No screenshot: {exc}", "warning")
            return
        path = _unused_path(folder, name, self._last_position or 0.0)
        if self.player.screenshot(path):
            self._set_status(f"Screenshot saved to {path}")
            self.player.show_text("Screenshot saved")
        else:
            self._set_status("mpv couldn't take a screenshot.", "warning")

    def cycle_track(self, name: str) -> None:
        """Subtitles on or off, or the next subtitles or audio track, in the
        video playing in the app - as mpv's V, J and # would."""
        if not self._video_playing_here():
            return
        value = self.player.cycle(name)
        if name == "sub-visibility":
            text = "Subtitles shown" if value else "Subtitles hidden"
        else:
            kind = "Subtitles" if name == "sub" else "Audio"
            track = self.player.current_track(name)
            text = f"{kind}: {_track_label(track)}" if track else f"{kind}: none"
        self._set_status(text + ".")
        self.player.show_text(text)

    def toggle_fullscreen(self) -> None:
        self.set_fullscreen(not self._fullscreen)

    def set_fullscreen(self, on: bool) -> None:
        """The video alone, filling the screen; the transport bar comes back
        while the mouse moves."""
        if on == self._fullscreen or (on and not self._video_playing_here()):
            return
        self._fullscreen = on
        chrome = [self.menuBar(), self.statusBar(), *self.findChildren(QToolBar),
                  *self.findChildren(QDockWidget)]
        if on:
            self.show_video_page()
            self._hidden_for_fullscreen = [w for w in chrome if w.isVisible()]
            for widget in self._hidden_for_fullscreen:
                widget.hide()
            self._was_maximized = self.isMaximized()
            self.video_page.set_fullscreen(True)
            self.centralWidget().layout().setContentsMargins(0, 0, 0, 0)
            self.showFullScreen()
            self._pointer = QCursor.pos()
            self._pointer_still = 0
            self._pointer_watch.start()
        else:
            self._pointer_watch.stop()
            for widget in self._hidden_for_fullscreen:
                widget.show()
            self._hidden_for_fullscreen = []
            self.video_page.set_fullscreen(False)
            self.centralWidget().layout().setContentsMargins(*self._centre_margins)
            self.now_playing_bar.show()
            if self._was_maximized:
                self.showMaximized()
            else:
                self.showNormal()
            self.video_page.setFocus()
        self._update_video_controls()

    def _watch_pointer(self) -> None:
        """In fullscreen, show the transport bar while the pointer moves and
        hide it when it has been still a while - away from the bar."""
        pointer = QCursor.pos()
        if pointer != self._pointer:
            self._pointer = pointer
            self._pointer_still = 0
            self.now_playing_bar.show()
            return
        self._pointer_still += POINTER_WATCH_MS
        over_bar = self.now_playing_bar.geometry().contains(
            self.now_playing_bar.parentWidget().mapFromGlobal(pointer)
        )
        if self._pointer_still >= CONTROLS_HIDE_MS and not over_bar:
            self.now_playing_bar.hide()

    # --- locked and private -----------------------------------------------

    def _target(self, video_id: str | None) -> tuple[str, dict] | None:
        if video_id is None:
            return self.current_video()
        video = self.data["videos"].get(video_id)
        return (video_id, video) if video is not None else None

    def _refuse_locked(self, video_id: str | None = None) -> bool:
        """Whether the video - the open one unless given - is locked, which
        is then said; a caller about to change it stops."""
        target = self._target(video_id)
        if target is None:
            return True
        video = target[1]
        owner = self._lib(target[0])
        if not protection.is_locked(owner, video):
            return False
        whole = protection.library_flag(owner, protection.LOCKED)
        self._set_status(
            f"{video['display_name']} is locked"
            + (" (the whole library is)" if whole else "")
            + " - unlock it to change anything.",
            "warning",
        )
        return True

    def _refuse_online(self, video_id: str | None = None) -> bool:
        """Whether the video may not be sent to MusicBrainz or the AI."""
        if self._refuse_locked(video_id):
            return True
        target = self._target(video_id)
        if protection.may_go_online(self._lib(target[0]), target[1]):
            return False
        self._set_status(
            f"{target[1]['display_name']} is private: nothing about it goes to "
            "MusicBrainz or the AI.",
            "warning",
        )
        return True

    def locked_ids(self) -> set[str]:
        return {video_id for data in self.shown.libraries
                for video_id, video in data["videos"].items()
                if protection.is_locked(data, video)}

    def private_ids(self) -> set[str]:
        return {video_id for data in self.shown.libraries
                for video_id, video in data["videos"].items()
                if protection.is_private(data, video)}

    def set_video_flag(self, video_id: str, flag: str, on: bool) -> None:
        video = self.data["videos"].get(video_id)
        if video is None:
            return
        protection.set_flag(video, flag, on)
        self._save_and_refresh()
        word = "locked" if flag == protection.LOCKED else "private"
        self._set_status(
            f"{video['display_name']} is {'now' if on else 'no longer'} {word}."
        )

    def set_library_flag(self, root: str, flag: str, on: bool) -> None:
        """Lock a whole library, or make it private - the open one or not."""
        word = "locked" if flag == protection.LOCKED else "private"
        shown = self.shown.library_at(root)
        if shown is not None:
            protection.set_library_flag(shown, flag, on)
            self._save_and_refresh()
        else:
            data = store.load_library_for_root(root)
            protection.set_library_flag(data, flag, on)
            store.save_library(data)
            self.libraries.refresh(self.shown.roots())
        self._set_status(f"{Path(root).name or root} is {'now' if on else 'no longer'} {word}.")

    # --- naming ----------------------------------------------------------

    def _save_and_refresh(self) -> None:
        self._save()
        current = self.current_video()
        if current is not None:
            self.detail.show_video(current[0], current[1], keep_selection=True)
        self.refresh_library()

    def rename_chapter(self, chapter_index: int) -> None:
        current = self.current_video()
        if current is None or self._refuse_locked():
            return
        chapter = current[1]["chapters"][chapter_index]

        new_title, accepted = QInputDialog.getText(
            self,
            "Rename Chapter",
            "Chapter title:",
            QLineEdit.Normal,
            chapter["title"] or "",
        )
        if not accepted:
            return
        utils.set_chapter_title(chapter, new_title)
        self._save_and_refresh()

    def mark_name_checked(self, chapter_index: int) -> None:
        current = self.current_video()
        if current is None or self._refuse_locked():
            return
        chapter = current[1]["chapters"][chapter_index]
        if chapter.get("title"):
            utils.set_chapter_title(chapter, chapter["title"], "manual",
                                    original_title=chapter.get("original_title"))
            self._save_and_refresh()

    def apply_track_mapping(self, mapping, source: str) -> None:
        """Name the open video's chapters from (index, title) pairs, or
        (index, title, original_title[, source]) where a title is a
        translation or a name has a source of its own."""
        current = self.current_video()
        if current is None or self._refuse_locked():
            return
        chapters = current[1]["chapters"]
        for chapter_index, title, *rest in mapping:
            if 0 <= chapter_index < len(chapters):
                utils.set_chapter_title(
                    chapters[chapter_index], title,
                    rest[1] if len(rest) > 1 and rest[1] else source,
                    original_title=rest[0] if rest else None,
                )
        self._save_and_refresh()

    def chapters_and_names(self) -> None:
        from mediabrowser.gui.dialogs.chapters_dialog import ChaptersDialog

        current = self.current_video()
        if current is None or self._refuse_locked():
            return
        dialog = ChaptersDialog(
            self, current[1], self._root_of(current[0]),
            private=protection.is_private(self._lib(current[0]), current[1]),
        )
        if dialog.exec():
            self.apply_chapters_result(dialog)

    def apply_chapters_result(self, dialog) -> None:
        """Whatever the Detect Chapters dialog proposed: names for the
        existing chapters, or new chapters in their place."""
        current = self.current_video()
        result = dialog.result()
        if current is None or result is None:
            return
        release_id = dialog.release_id()
        if release_id:
            # Kept so the Cover Art Archive can be asked for this release's
            # sleeve, which beats a frame grab.
            current[1]["musicbrainz_release_id"] = release_id
            artwork.forget(current[0])
        kind, payload = result
        if kind == "name":
            self.apply_track_mapping(payload, dialog.title_source())
        elif kind == "change":
            # Just Figure It Out: looked at and applied here, so it isn't a
            # run's to undo.
            self._apply_change(current[0], payload, journal=False)
        else:
            chapters, origin = payload
            self.replace_chapters(chapters, origin)

    def ai_settings(self) -> None:
        from mediabrowser.gui.dialogs.ai_settings_dialog import AISettingsDialog

        AISettingsDialog(self).exec()

    def translate_titles(self) -> None:
        from mediabrowser.gui.dialogs.translate_dialog import TranslateDialog

        current = self.current_video()
        if current is None or self._refuse_online():
            return
        dialog = TranslateDialog(self, current[1])
        if dialog.exec():
            self.apply_track_mapping(dialog.mapping(), "ai")
            self._set_status(f"{len(dialog.mapping())} title(s) romanised.")

    def edit_chapters(self) -> None:
        """Edit the open video's chapters in the main area, the video playing
        there - from where it had got to, if it was the one playing."""
        current = self.current_video()
        if current is None or self.editor.active() or self._refuse_locked():
            return
        video_id, video = current
        start = self.playhead_of(video_id) or 0.0
        # One mpv at a time: the editor's is the one now.
        self.stop_playback()
        self.pages.setCurrentIndex(PAGE_EDITOR)
        # The editor has its own transport; the bar would only say
        # "Nothing playing" under it.
        self.now_playing_bar.hide()
        self.editor.open(video_id, video, start)

    def _on_editor_closed(self) -> None:
        self.pages.setCurrentIndex(PAGE_DETAIL)
        self.now_playing_bar.show()

    def _on_editor_saved(self, video_id: str, chapters, origin) -> None:
        if video_id not in self.data["videos"] or self._refuse_locked(video_id):
            return
        self._restructure(chapters, lambda old: old, origin, video_id=video_id)
        named = sum(1 for c in chapters if c["title"])
        self._set_status(f"Saved {len(chapters)} chapter(s), {named} named.")

    def edit_tracklist(self) -> None:
        from mediabrowser.gui.dialogs.edit_dialog import EditTracklistDialog

        current = self.current_video()
        if current is None or self._refuse_locked():
            return
        dialog = EditTracklistDialog(self, current[1])
        if dialog.exec():
            for chapter, title in zip(
                current[1]["chapters"], dialog.titles(), strict=False
            ):
                utils.set_chapter_title(chapter, title)
            self._save_and_refresh()

    # --- folders ---------------------------------------------------------

    def _add_folder_actions(self, menu, video_id: str, allow_hiding: bool = True) -> None:
        """The containing-folder entries of a video's right-click menu."""
        video = self.data["videos"].get(video_id)
        if video is None:
            return
        owner = self._lib(video_id)
        locked = protection.is_locked(owner, video)
        uses_ai = self._identify_uses_ai(video_id)
        identify = QAction("Identify (Uses AI)" if uses_ai else "Identify", menu)
        identify.setToolTip(
            "Find this video's chapters and names in the background, the way Identify "
            "Library is set to - which methods, and the AI or not"
            + (f". The AI may be asked up to {autoname.ONE_VIDEO_AI_BUDGET} times: a few "
               "cents at most" if uses_ai else ": no AI, so it costs nothing")
        )
        identify.setEnabled(
            not locked and video_id not in self._identifying_videos
            and video_id not in self._missing and not self.identifying()
        )
        identify.triggered.connect(lambda: self.identify_video(video_id))
        menu.addAction(identify)
        # A disc's extras arrive as "Disc - Title 5"; this is where they get
        # a name worth reading.
        rename = QAction(
            f"Rename {'Title' if video['type'] == 'bluray' else 'Video'}…", menu
        )
        rename.setEnabled(not locked)
        rename.triggered.connect(lambda: self.rename_video(video_id))
        menu.addAction(rename)
        marked = QAction("Mark Named", menu)
        marked.setCheckable(True)
        marked.setChecked(bool(video.get(naming.MARKED_KEY)))
        marked.setToolTip(
            "It's fine as it is - shown as Marked named, and Identify Library leaves "
            "it alone, even with chapters left unnamed"
        )
        marked.setEnabled(not locked)
        marked.toggled.connect(lambda on: self.mark_video_named(video_id, on))
        menu.addAction(marked)
        reset_video = QAction("Reset to Defaults…", menu)
        reset_video.setToolTip(
            "Clear its names, chapters made here and the name you gave it, and read "
            "its chapters afresh; can be undone"
        )
        reset_video.setEnabled(not locked and video_id not in self._missing)
        reset_video.triggered.connect(lambda: self.reset_video(video_id))
        menu.addAction(reset_video)
        reveal = QAction("Open Containing Folder", menu)
        reveal.triggered.connect(lambda: self.reveal_video(video_id))
        menu.addAction(reveal)
        self._add_protection_actions(menu, video_id, video)
        if not allow_hiding:
            return
        if video_id in self._missing:
            remove = QAction("Remove from Library…", menu)
            remove.triggered.connect(lambda: self.remove_video(video_id))
            menu.addAction(remove)

        # A single video can be hidden without its folder: one Blu-ray title
        # among a disc's extras, where the folder is the whole disc.
        noun = "Title" if video["type"] == "bluray" else "Video"
        root = owner["settings"]["library_root"]
        covering = folders.hiding(video, folders.hidden_folders(owner))
        if video.get(folders.VIDEO_HIDDEN_KEY):
            action = QAction(f"Unhide This {noun}", menu)
            action.setEnabled(not locked)
            action.triggered.connect(lambda: self.unhide_video(video_id))
            menu.addAction(action)
        elif not covering:
            action = QAction(f"Hide This {noun}", menu)
            action.setEnabled(not locked)
            action.triggered.connect(lambda: self.hide_video(video_id))
            menu.addAction(action)
        for folder in covering:
            action = QAction(f"Unhide Folder “{folders.relative_name(folder, root)}”", menu)
            action.triggered.connect(lambda _checked=False, f=folder: self.unhide_folder(f))
            menu.addAction(action)
        if covering:
            return

        chain = folders.folder_chain(video, root)
        if len(chain) == 1:
            folder = chain[0]
            action = QAction(f"Hide Folder “{folders.relative_name(folder, root)}”", menu)
            action.triggered.connect(lambda: self.hide_folder(folder))
            menu.addAction(action)
        elif chain:
            # A video deep in a tree can be hidden at any level above it:
            # "Extras/Trailers" alone, or all of "Extras".
            submenu = menu.addMenu("Hide Folder")
            for folder in chain:
                action = QAction(folders.relative_name(folder, root), submenu)
                action.triggered.connect(lambda _checked=False, f=folder: self.hide_folder(f))
                submenu.addAction(action)

    def _add_protection_actions(self, menu, video_id: str, video: dict) -> None:
        """Locked and Private, ticked when on - and fixed on, when it's the
        whole library's setting."""
        menu.addSeparator()
        for flag, label, tip in (
            (protection.LOCKED, "Locked", protection.LOCKED_TIP),
            (protection.PRIVATE, "Private", protection.PRIVATE_TIP),
        ):
            whole = protection.library_flag(self._lib(video_id), flag)
            action = QAction(f"{label} (whole library)" if whole else label, menu)
            action.setCheckable(True)
            action.setChecked(whole or bool(video.get(flag)))
            action.setEnabled(not whole)
            action.setToolTip(tip)
            action.toggled.connect(
                lambda on, f=flag: self.set_video_flag(video_id, f, on)
            )
            menu.addAction(action)
        menu.setToolTipsVisible(True)
        menu.addSeparator()

    def _identify_options(self, video_id: str):
        """(options, AI settings, private) for identifying one video the way
        Identify Library was last set to."""
        from mediabrowser.core import ai
        from mediabrowser.gui.dialogs.identify_dialog import saved_options

        settings = ai.load_settings()
        options = saved_options(ai.is_configured(settings))
        video = self.data["videos"][video_id]
        return options, settings, protection.is_private(self._lib(video_id), video)

    def _identify_uses_ai(self, video_id: str) -> bool:
        """Whether Identify on this video may ask the AI - and so cost money."""
        options, _settings, private = self._identify_options(video_id)
        return not private and bool(set(options.methods) & set(autoname.AI_METHODS))

    def identify_video(self, video_id: str) -> None:
        """Identify one video in the background, with the choices Identify
        Library was last set to; its result can be undone like a run's."""
        video = self.data["videos"].get(video_id)
        if video is None or self._refuse_locked(video_id):
            return
        if self.identifying() or video_id in self._identifying_videos:
            self._set_status("That's being identified already.")
            return
        options, settings, private = self._identify_options(video_id)
        if not options.methods or (
            private and not set(options.methods) & set(autoname.LOCAL_METHODS)
        ):
            self._set_status(
                f"Nothing can identify {video['display_name']} with the methods Identify "
                "Library is set to" + (" for a private video." if private else "."),
                "warning",
            )
            return
        root = self._root_of(video_id)
        mb_format = store.load_app_settings().get("musicbrainz_format", "xml")
        snapshot = dict(video, chapters=[dict(c) for c in video["chapters"]])
        uses_ai = not private and bool(set(options.methods) & set(autoname.AI_METHODS))
        # One video never gets a whole run's worth of AI requests.
        budget = autoname.Budget(
            min(options.ai_budget, autoname.ONE_VIDEO_AI_BUDGET) if uses_ai else 0
        )

        def work():
            return autoname.identify(
                video_id, snapshot, options, settings, autoname.Services(mb_format),
                budget, root, private=private,
            )

        self._identifying_videos.add(video_id)
        self._set_status(
            f"Identifying {video['display_name']}…"
            + (f" (the AI may be asked up to {budget.remaining} times - a few cents)"
               if budget.remaining else "")
        )
        self._jobs.append(run_job(
            self, work,
            on_done=self._on_video_identified,
            on_failed=lambda message: self._on_video_identify_failed(video_id, message),
        ))

    def _on_video_identified(self, outcome) -> None:
        self._identifying_videos.discard(outcome.video_id)
        if outcome.error:
            self._on_video_identify_failed(outcome.video_id, outcome.error)
            return
        said = next((line for line in reversed(outcome.log) if line), "")
        if outcome.change is None:
            self._set_status(
                f"{outcome.name}: nothing more found. {said}".strip()
            )
            return
        if self.data["videos"].get(outcome.video_id) is None:
            return  # another library is open now
        autoname.start_journal(self.data)
        self.apply_identified(outcome.video_id, outcome.change)
        self._set_status(
            f"{outcome.name}: {outcome.before.describe()} → {outcome.after.describe()}. "
            f"{said} (File ▸ Undo Last Identification puts it back.)"
        )

    def _on_video_identify_failed(self, video_id: str, message: str) -> None:
        self._identifying_videos.discard(video_id)
        video = self.data["videos"].get(video_id)
        name = video["display_name"] if video else "That video"
        self._set_status(f"{name} couldn't be identified: {message}", "warning")

    def rename_video(self, video_id: str) -> None:
        video = self.data["videos"].get(video_id)
        if video is None or self._refuse_locked(video_id):
            return
        automatic = video.get("auto_name", video["display_name"])
        name, accepted = QInputDialog.getText(
            self,
            "Rename",
            f"Name (leave empty to go back to “{automatic}”):",
            QLineEdit.Normal,
            video.get("custom_name") or "",
        )
        if not accepted:
            return
        library.set_custom_name(video, name)
        self._save_and_refresh()

    def mark_video_named(self, video_id: str, on: bool) -> None:
        """Count a video as named whatever its chapters say - or not."""
        video = self.data["videos"].get(video_id)
        if video is None or self._refuse_locked(video_id):
            return
        if on:
            video[naming.MARKED_KEY] = True
        else:
            video.pop(naming.MARKED_KEY, None)
        self._save_and_refresh()
        self._set_status(
            f"{video['display_name']} is {'now marked' if on else 'no longer marked'} named."
        )

    def remove_video(self, video_id: str) -> None:
        video = self.data["videos"].get(video_id)
        if video is None or self._refuse_locked(video_id):
            return
        answer = QMessageBox.question(
            self,
            "Remove from Library",
            f"Forget {video['display_name']} and its chapter names?\n\n{video['path']}\n\n"
            "Only do this if it's gone for good - while its file is just away "
            "(a network share that's disconnected, say), keeping it costs nothing, "
            "and it comes back named when the file does.",
        )
        if answer != QMessageBox.Yes:
            return
        library.remove_videos(self.data, [video_id])
        self._save()
        self.refresh_library()
        self._set_status(f"Removed {video['display_name']}.")

    def remove_missing_videos(self) -> None:
        locked = self.locked_ids()
        missing = [vid for vid in library.missing_videos(self.data) if vid not in locked]
        if not missing:
            self._set_status("Nothing is missing.")
            return
        names = [self.data["videos"][vid]["display_name"] for vid in missing]
        listed = "\n".join(f"  {name}" for name in names[:12])
        if len(names) > 12:
            listed += f"\n  …and {len(names) - 12} more"
        answer = QMessageBox.question(
            self,
            "Remove Missing Videos",
            f"Forget these {len(names)} video(s), and their chapter names?\n\n{listed}\n\n"
            "If they're on a network share or drive that's only disconnected, "
            "reconnect it instead - they'll come back named.",
        )
        if answer != QMessageBox.Yes:
            return
        removed = library.remove_videos(self.data, missing)
        self._save()
        self.refresh_library()
        self._set_status(f"Removed {removed} missing video{'' if removed == 1 else 's'}.")

    def reveal_video(self, video_id: str) -> None:
        from mediabrowser.gui import reveal

        video = self.data["videos"].get(video_id)
        if video is None:
            return
        if not reveal.show_in_file_manager(video):
            self._set_status("That folder is no longer there.", "warning")

    def _count_under(self, folder) -> int:
        return sum(
            1 for video in self.data["videos"].values()
            if folders.hiding(video, [str(folder)])
        )

    def hide_video(self, video_id: str) -> None:
        video = self.data["videos"].get(video_id)
        if video is None or self._refuse_locked(video_id):
            return
        folders.hide_video(video)
        self._save()
        self.refresh_library()
        text = f"Hid {video['display_name']}."
        if not self._show_hidden:
            text += " Tick Show hidden to see or unhide it."
        self._set_status(text)

    def unhide_video(self, video_id: str) -> None:
        video = self.data["videos"].get(video_id)
        if video is None or self._refuse_locked(video_id):
            return
        folders.unhide_video(video)
        self._save()
        self.refresh_library()
        self._set_status(f"{video['display_name']} is shown again.")

    def hide_folder(self, folder) -> None:
        owner = self.shown.library_holding(folder)
        root = owner["settings"]["library_root"]
        folders.hide(owner, folder)
        self._save()
        self.refresh_library()
        count = self._count_under(folder)
        text = (
            f"Hid “{folders.relative_name(folder, root)}” "
            f"({count} video{'' if count == 1 else 's'})."
        )
        if not self._show_hidden:
            text += " Tick Show hidden to see or unhide it."
        self._set_status(text)

    def unhide_folder(self, folder) -> None:
        owner = self.shown.library_holding(folder)
        root = owner["settings"]["library_root"]
        folders.unhide(owner, folder)
        self._save()
        self.refresh_library()
        self._set_status(f"“{folders.relative_name(folder, root)}” is shown again.")

    # --- chapter boundaries ----------------------------------------------

    def _restructure(self, chapters, index_map, origin: str | None,
                     video_id: str | None = None) -> None:
        """Give the open video (or `video_id`'s) a new set of chapters,
        keeping the queue and whatever is playing pointed at the same music.

        `origin` None means these are the file's own chapters again.
        """
        if video_id is None:
            current = self.current_video()
            if current is None:
                return
            video_id = current[0]
        video = self.data["videos"].get(video_id)
        if video is None or self._refuse_locked(video_id):
            return
        video["chapters"] = chapters
        # Back to one piece - reset, or merged down - it goes by its file's name.
        library.name_single_chapters({"videos": {video_id: video}})
        if origin:
            video["chapter_origin"] = origin
        else:
            video.pop("chapter_origin", None)

        self.queue.remap_video(video_id, video, index_map)
        self._queue_edited()
        if self.now_playing is not None and self.now_playing[0] == video_id:
            _vid, index, audio_only = self.now_playing
            index = max(0, min(index_map(index), len(chapters) - 1))
            self.now_playing = (video_id, index, audio_only)
        self._save_and_refresh()

    def _edited_origin(self) -> str:
        """What a hand edit leaves a video's chapters as: whatever made them,
        if the app did, or else the file's own chapters, edited.
        """
        current = self.current_video()
        origin = current[1].get("chapter_origin") if current else None
        return origin or library.ORIGIN_EDITED

    def replace_chapters(self, chapters, origin: str) -> None:
        # There is no telling which new chapter an old one's music is in, so
        # queued entries keep their positions, clamped to the new count.
        self._restructure(chapters, lambda old: old, origin)

    def reset_chapters(self) -> None:
        current = self.current_video()
        if current is None or not current[1].get("chapter_origin") or self._refuse_locked():
            return
        answer = QMessageBox.question(
            self,
            "Reset Chapters",
            "Go back to the chapters in the file itself? The chapters made or "
            "edited here, and their names, will be lost.",
        )
        if answer != QMessageBox.Yes:
            return
        try:
            chapters = library.original_chapters(
                current[1], self._lib(current[0])["settings"]["min_bluray_title_seconds"]
            )
        except Exception as exc:
            QMessageBox.critical(self, "Reset failed", f"Couldn't read the chapters: {exc}")
            return
        self._restructure(chapters, lambda old: old, None)

    def split_at_playhead(self) -> None:
        if self.editor.active():
            self.editor.insert_here()
            return
        if self._refuse_locked():
            return
        position = self.playhead_in_open_video()
        current = self.current_video()
        if position is None or current is None:
            self._set_status("Play this video and pause where a chapter should start.")
            return
        try:
            chapters, index_map = chapter_edit.split(current[1]["chapters"], position)
        except chapter_edit.EditError as exc:
            self._set_status(f"Can't split there: {exc}.", "warning")
            return
        self._restructure(chapters, index_map, self._edited_origin())
        self._set_status(f"New chapter at {utils.format_seconds(position)}.")

    def merge_with_next(self, index: int) -> None:
        current = self.current_video()
        if current is None or self._refuse_locked():
            return
        try:
            chapters, index_map = chapter_edit.merge_with_next(current[1]["chapters"], index)
        except chapter_edit.EditError as exc:
            self._set_status(f"Can't merge: {exc}.", "warning")
            return
        self._restructure(chapters, index_map, self._edited_origin())

    def nudge_chapter_start(self, index: int, delta: float) -> None:
        current = self.current_video()
        if current is None or self._refuse_locked():
            return
        try:
            chapters, index_map = chapter_edit.move_start(
                current[1]["chapters"], index, delta
            )
        except chapter_edit.EditError as exc:
            self._set_status(f"Can't move it: {exc}.", "warning")
            return
        self._restructure(chapters, index_map, self._edited_origin())
        self._set_status(
            f"Chapter {index + 1} now starts at "
            f"{utils.format_seconds(chapters[index]['start'])}."
        )

    def _merge_selected(self) -> None:
        index = self.detail.selected_chapter()
        if self.pages.currentIndex() == PAGE_DETAIL and index is not None:
            self.merge_with_next(index)

    def _nudge_selected(self, delta: float) -> None:
        index = self.detail.selected_chapter()
        if self.pages.currentIndex() == PAGE_DETAIL and index is not None:
            self.nudge_chapter_start(index, delta)

    # --- resetting to defaults ----------------------------------------------------

    def _confirm(self, title: str, text: str, detail: str, action: str,
                 checkbox: str | None = None) -> tuple[bool, bool]:
        """A warning to accept or cancel (Cancel the default); and the
        checkbox's state, if there is one."""
        box = QMessageBox(QMessageBox.Warning, title, text, QMessageBox.Cancel, self)
        box.setInformativeText(detail)
        go = box.addButton(action, QMessageBox.DestructiveRole)
        box.setDefaultButton(QMessageBox.Cancel)
        check = None
        if checkbox:
            check = QCheckBox(checkbox)
            box.setCheckBox(check)
        box.exec()
        return box.clickedButton() is go, bool(check and check.isChecked())

    def reset_video(self, video_id: str) -> None:
        """A title back as first scanned: its own chapters read again, and
        everything added to it cleared - after a warning, and undoably."""
        video = self.data["videos"].get(video_id)
        if video is None or self._refuse_locked(video_id):
            return
        lost = reset.describe_video(video)
        cleared = (", ".join(lost[:-1]) + (" and " if len(lost) > 1 else "") + lost[-1]
                   if lost else "nothing has been added to it yet, but")
        accepted, _ = self._confirm(
            "Reset to Defaults",
            f"Reset “{video['display_name']}” to how it was first scanned?",
            f"This clears {cleared}; its chapters are read from the "
            f"{'disc' if video['type'] == 'bluray' else 'file'} again. Whether it's locked "
            "or private is kept.\n\nFile ▸ Undo Reset puts it back.",
            "Reset",
        )
        if not accepted:
            return
        owner = self._lib(video_id)
        minimum = owner["settings"]["min_bluray_title_seconds"]
        snapshot = dict(video)
        self._set_status(f"Reading {video['display_name']} afresh…")
        self._jobs.append(run_job(
            self, lambda: library.original_chapters(snapshot, minimum),
            on_done=lambda chapters: self._finish_video_reset(video_id, chapters),
            on_failed=lambda message: self._set_status(
                f"Not reset - it couldn't be read: {message}", "error"),
        ))

    def _finish_video_reset(self, video_id: str, chapters) -> None:
        owner = self._lib(video_id)
        try:
            video = reset.reset_video(owner, video_id, chapters)
        except reset.ResetError as exc:
            self._set_status(f"Not reset: {exc}.", "warning")
            return
        self._after_chapters_changed(video_id, video)
        self._set_status(
            f"{video['display_name']} is as first scanned. File ▸ Undo Reset puts it back."
        )

    def reset_library_on_show(self) -> None:
        data = self._choose_library("Reset Library to Defaults", "should be reset")
        if data is not None:
            self.reset_library(data["settings"]["library_root"])

    def reset_library(self, root: str) -> None:
        """A whole library back as first scanned - after a warning, and
        undoably."""
        if self._scanning or self.identifying():
            self._set_status("Wait for the scan or identification to finish first.", "warning")
            return
        data = self.shown.library_at(root) or store.load_library_for_root(root)
        name = Path(root).name or root
        if protection.library_flag(data, protection.LOCKED):
            self._set_status(f"{name} is locked - unlock it to reset it.", "warning")
            return
        count = len(data["videos"])
        saved = len(playlists.all_playlists(data))
        accepted, drop_playlists = self._confirm(
            "Reset Library to Defaults",
            f"Reset the whole of “{name}” ({count} video{'' if count == 1 else 's'}) to how "
            "it was first scanned?",
            "Every chapter name given here (names the files carry come back), the chapters "
            "made or edited here, names you gave videos, "
            "MusicBrainz releases and hidden videos are cleared, and so are the library's "
            "hidden folders and its last Identify run. Everything is read from the files "
            "and discs again, which can take a while.\n\n"
            "Kept: locked videos, whole; whether each video is locked or private; and any "
            "video whose file can't be read right now.\n\n"
            "File ▸ Undo Reset puts it all back.",
            "Reset Library",
            checkbox=(f"Also delete its {saved} playlist{'' if saved == 1 else 's'}"
                      if saved else None),
        )
        if not accepted:
            return
        self._scanning = True
        self._scan_cancel = cancel = threading.Event()
        self._set_scan_controls(False)
        self.progress.show()
        self.cancel_scan_button.setEnabled(True)
        self.cancel_scan_button.show()
        self._set_status(f"Resetting {name}…")
        self._jobs.append(run_job(
            self,
            lambda progress_cb: reset.reset_library(
                root, keep_playlists=not drop_playlists, progress_cb=progress_cb, cancel=cancel
            ),
            wants_progress=True,
            on_progress=self._on_scan_progress,
            on_done=self._on_library_reset,
            on_failed=self._on_scan_failed,
        ))

    def _on_library_reset(self, data: dict) -> None:
        self._end_scan()
        root = data["settings"]["library_root"]
        if self.shown.library_at(root) is not None:
            self.shown.replace(data)
            self.queue_panel.show_queue(self.queue.entries(), self.queue.current_index())
            self.show_grid()
            self.refresh_library()
        self._set_status(
            f"{Path(root).name or root} is as first scanned. File ▸ Undo Reset puts it back."
        )

    def _last_reset(self) -> tuple[dict, dict] | None:
        """(library, undo) for the latest reset on show that can be undone."""
        undos = [(data, reset.last(data)) for data in self.shown.libraries if reset.last(data)]
        return max(undos, key=lambda pair: pair[1]["when"]) if undos else None

    def undo_reset(self) -> None:
        found = self._last_reset()
        if found is None:
            self._set_status("There's no reset to undo.")
            return
        data, undo = found
        answer = QMessageBox.question(
            self, "Undo Reset", f"Undo {reset.describe(undo)}, putting back what it cleared?"
        )
        if answer != QMessageBox.Yes:
            return
        what = reset.undo(data)
        store.save_library(data)
        if undo["kind"] == "video":
            video = data["videos"].get(undo["video_id"])
            if video is not None:
                self.queue.remap_video(undo["video_id"], video, lambda old: old)
        self.shown.replace(data)
        self._queue_edited()
        self._save_and_refresh()
        self._set_status(f"Undone: {what}.")

    # --- catalogs between devices --------------------------------------------

    def _choose_library(self, title: str, verb: str) -> dict | None:
        """The library on show to act on: the one, or - with several - the
        one picked."""
        if not self.shown.several():
            return self.shown.primary
        names = [f"{Path(root).name or root}  ({root})" for root in self.shown.roots()]
        chosen, accepted = QInputDialog.getItem(
            self, title, f"Which library {verb}?", names, 0, False
        )
        if not accepted:
            return None
        return self.shown.libraries[names.index(chosen)]

    def export_catalog(self) -> None:
        data = self._choose_library("Export Catalog", "should be exported")
        if data is None:
            return
        root = data["settings"].get("library_root")
        if not root or not data["videos"]:
            self._set_status("There's no library open to export.")
            return
        default = str(Path.home() / f"{Path(root).name or 'library'} catalog.json")
        path, _filter = QFileDialog.getSaveFileName(
            self, "Export Catalog", default, "Catalogs (*.json)"
        )
        if not path:
            return
        if Path(path).suffix.lower() != ".json":
            path += ".json"
        try:
            catalog.export(data, path, [known["root"] for known in store.list_libraries()])
        except catalog.CatalogError as exc:
            QMessageBox.warning(self, "Export Catalog", str(exc))
            return
        except OSError as exc:
            QMessageBox.critical(self, "Export failed", f"Couldn't write the catalog: {exc}")
            return
        self._set_status(f"Catalog of {len(data['videos'])} videos saved to {path}.")

    def import_catalog(self) -> None:
        if self._scanning or self.identifying():
            self._set_status("Wait for the scan or identification to finish first.", "warning")
            return
        path, _filter = QFileDialog.getOpenFileName(
            self, "Import Catalog", str(Path.home()), "Catalogs (*.json);;All files (*)"
        )
        if not path:
            return
        try:
            incoming = catalog.read(path)
        except catalog.CatalogError as exc:
            QMessageBox.warning(self, "Import Catalog", str(exc))
            return
        made_at = incoming["settings"]["library_root"]
        folder = QFileDialog.getExistingDirectory(
            self, f"Where is “{Path(made_at).name}” on this device?", str(Path.home())
        )
        if not folder:
            return
        try:
            data, changed = catalog.import_into(incoming, folder)
        except catalog.CatalogError as exc:
            QMessageBox.warning(self, "Import Catalog", str(exc))
            return
        self.load_root(data["settings"]["library_root"])
        self._set_status(
            f"Imported: {changed} video(s) named from the catalog made at {made_at}. "
            "Rescanning for anything new…"
        )
        self.rescan()

    # --- identifying the library ---------------------------------------------

    def identify_library(self) -> None:
        from mediabrowser.gui.dialogs.identify_dialog import IdentifyDialog

        if self._identify_dialog is None:
            self._identify_dialog = IdentifyDialog(self)
        self._identify_dialog.show()
        self._identify_dialog.raise_()
        self._identify_dialog.activateWindow()

    def identifying(self) -> bool:
        return self._identify_dialog is not None and self._identify_dialog.running()

    def shelf_videos(self) -> list[tuple[str, dict]]:
        """Every video of the library, in the shelf's order."""
        videos = list(self.data["videos"].items())
        videos.sort(key=self._sort[2])
        return videos

    def identify_skips(self, include_hidden: bool = False) -> set[str]:
        """Videos a run leaves alone: missing ones, and hidden ones unless asked."""
        skip = set(library.missing_videos(self.data)) | self.locked_ids()
        if not include_hidden:
            skip |= self._hidden_ids()
        return skip

    def begin_identify_run(self) -> None:
        autoname.start_journal(self.data)
        self._save()

    def apply_identified(self, video_id: str, change) -> None:
        """One video's result from a run, applied as it arrives."""
        self._apply_change(video_id, change)

    def _apply_change(self, video_id: str, change, journal: bool = True) -> None:
        if self._refuse_locked(video_id):
            return
        previous_release = self.data["videos"].get(video_id, {}).get("musicbrainz_release_id")
        video = autoname.apply(self.data, video_id, change, journal=journal)
        if video is None:
            return
        if change.release_id and change.release_id != previous_release:
            artwork.forget(video_id)
        self._after_chapters_changed(video_id, video)

    def _after_chapters_changed(self, video_id: str, video: dict) -> None:
        # Nothing tells which new chapter an old one's music is in, so queued
        # entries keep their places, clamped to the new count.
        self.queue.remap_video(video_id, video, lambda old: old)
        self._queue_edited()
        if self.now_playing is not None and self.now_playing[0] == video_id:
            _vid, index, audio_only = self.now_playing
            self.now_playing = (video_id, min(index, len(video["chapters"]) - 1), audio_only)
        self._save_and_refresh()

    def show_identify_progress(self, n: int, total: int, name: str) -> None:
        self._set_status(f"Identifying {n + 1} of {total}: {name}…")

    def end_identify_run(self, summary: str) -> None:
        self._set_status(f"Identify Library - {summary}")

    def can_undo_identify(self) -> bool:
        return autoname.last_run(self.data) is not None

    def undo_identify(self) -> int:
        restored = autoname.undo(self.data)
        for video_id in restored:
            video = self.data["videos"][video_id]
            self.queue.remap_video(video_id, video, lambda old: old)
        self._queue_edited()
        self._save_and_refresh()
        return len(restored)

    def _undo_identify_from_menu(self) -> None:
        journal = autoname.last_run(self.data)
        if journal is None:
            return
        answer = QMessageBox.question(
            self, "Undo Last Identification",
            f"Put back the {len(journal['before'])} video(s) the run started "
            f"{journal['started']} changed, as they were before it?",
        )
        if answer == QMessageBox.Yes:
            count = self.undo_identify()
            self._set_status(f"Undone: {count} video(s) put back as they were.")

    # --- lifecycle -------------------------------------------------------

    def closeEvent(self, event) -> None:
        if self._identify_dialog is not None and self._identify_dialog.running():
            # A run finishes the video it's on and stops; what's done is kept.
            self._identify_dialog.stop()
        # mpv is a separate process: without this it keeps playing after the
        # window that started it has gone.
        self._poll.stop()
        self.session.stop()
        if self.editor.active():
            # Its own mpv, its poll and its hold on the keyboard go with it.
            self.editor._close()
        super().closeEvent(event)


def _screenshot_dir() -> Path:
    """Where screenshots go: a folder of the app's in Pictures."""
    pictures = QStandardPaths.writableLocation(QStandardPaths.PicturesLocation)
    return Path(pictures or Path.home()) / SCREENSHOT_FOLDER


def _unused_path(folder: Path, name: str, seconds: float) -> Path:
    """A screenshot's file: the video's name and the moment, never one
    that's there already."""
    name = name.replace("/", "-").replace("\0", "").strip(". ") or "Screenshot"
    stem = f"{name} {utils.format_seconds(seconds).replace(':', '-')}"
    path = folder / f"{stem}.png"
    count = 2
    while path.exists():
        path = folder / f"{stem} ({count}).png"
        count += 1
    return path


def _track_label(track: dict) -> str:
    """A track as mpv describes it, in words: "English · Commentary"."""
    parts = [str(track[key]) for key in ("lang", "title") if track.get(key)]
    return " · ".join(parts) or f"track {track.get('id', '?')}"
