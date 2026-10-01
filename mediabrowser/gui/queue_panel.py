# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""What is playing and what comes next, down the right.

The old UI had no queue at all: next and previous stepped within one video
and stopped at its edges, so playing a run of tracks across two discs meant
going back to the tree between each one.

Rows can be Ctrl- or Shift-selected several at a time, to remove or drag
together. A queue can be saved as a playlist, and the Playlists menu plays
saved ones - the window fills it (playlists_menu). A row's right-click
menu plays it as it was queued, as audio, or as video.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QItemSelectionModel, Qt, QTimer, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from mediabrowser.core import utils
from mediabrowser.core.playback import REPEAT_ALL, REPEAT_OFF, REPEAT_ONE

# Cycled by the one Repeat button, in this order.
REPEAT_CYCLE = (REPEAT_OFF, REPEAT_ALL, REPEAT_ONE)
REPEAT_LABELS = {
    REPEAT_OFF: ("Repeat", "Play the queue through once"),
    REPEAT_ALL: ("Repeat All", "Start the queue again when it ends"),
    REPEAT_ONE: ("Repeat One", "Play this chapter over and over"),
}


class QueuePanel(QWidget):
    entry_activated = Signal(int)
    play_as_requested = Signal(int, bool)  # entry index, audio only
    entries_removed = Signal(list)  # entry indices
    order_changed = Signal(list)  # every entry index, in the order now shown
    cleared = Signal()
    shuffle_toggled = Signal(bool)
    repeat_changed = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self._repeat = REPEAT_OFF

        # Without a minimum of its own the dock is sized by its widest row,
        # which on a long track name pushes the whole window wider.
        self.setMinimumWidth(190)

        self.list = QListWidget()
        self.list.setObjectName("queueList")
        self.list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.list.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        # Dragging rows is how a queue gets reordered; the window turns the
        # resulting row move back into a queue move.
        self.list.setDragDropMode(QAbstractItemView.InternalMove)
        self.list.itemActivated.connect(self._on_activated)
        self.list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._show_menu)
        self.list.itemSelectionChanged.connect(self._update_buttons)
        # A drag of several rows arrives as several moves; the order is read
        # once they're all done.
        self.list.model().rowsMoved.connect(lambda *_args: QTimer.singleShot(0, self._read_order))
        delete = QShortcut(QKeySequence(Qt.Key_Delete), self.list)
        delete.setContext(Qt.WidgetShortcut)
        delete.activated.connect(self._remove_selected)
        # The window fills the Playlists menu as it opens.
        self.playlists_menu: Callable[[QMenu], None] | None = None

        self.empty = QLabel("Nothing queued.\n\nPlay a chapter and\nthe rest follows it here.")
        self.empty.setObjectName("placeholder")
        self.empty.setAlignment(Qt.AlignCenter)
        self.empty.setTextFormat(Qt.PlainText)

        self.shuffle_button = QToolButton()
        self.shuffle_button.setText("Shuffle")
        self.shuffle_button.setCheckable(True)
        self.shuffle_button.setToolTip("Play the queue in a random order")
        self.shuffle_button.toggled.connect(self.shuffle_toggled)

        self.repeat_button = QToolButton()
        self.repeat_button.setText("Repeat")
        self.repeat_button.setToolTip(REPEAT_LABELS[REPEAT_OFF][1])
        self.repeat_button.clicked.connect(self._cycle_repeat)

        self.playlists_button = QToolButton()
        self.playlists_button.setText("Playlists")
        self.playlists_button.setToolTip("Save this queue as a playlist, or play a saved one")
        self.playlists_button.setPopupMode(QToolButton.InstantPopup)
        # Its own row: beside Shuffle and Repeat, a narrow dock cut all three
        # down to a letter or two.
        self.playlists_button.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.playlists_button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        menu = QMenu(self.playlists_button)
        menu.aboutToShow.connect(lambda: self._fill_playlists(menu))
        self.playlists_button.setMenu(menu)

        modes = QHBoxLayout()
        modes.setContentsMargins(0, 0, 0, 0)
        modes.setSpacing(6)
        modes.addWidget(self.shuffle_button)
        modes.addWidget(self.repeat_button)
        modes.addStretch(1)

        self.remove_button = QPushButton("Remove")
        self.remove_button.setToolTip("Take the selected chapters out of the queue (Delete)")
        self.remove_button.clicked.connect(self._remove_selected)
        self.clear_button = QPushButton("Clear")
        self.clear_button.clicked.connect(self.cleared)

        buttons = QHBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.setSpacing(6)
        buttons.addWidget(self.remove_button)
        buttons.addWidget(self.clear_button)
        buttons.addStretch(1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)
        layout.addLayout(modes)
        layout.addWidget(self.playlists_button)
        layout.addWidget(self.empty, 1)
        layout.addWidget(self.list, 1)
        layout.addLayout(buttons)

        self.show_queue([], None)

    # --- population ------------------------------------------------------

    def show_queue(self, entries, current_index: int | None) -> None:
        self.list.blockSignals(True)
        self.list.clear()
        for i, entry in enumerate(entries):
            item = QListWidgetItem(
                f"{entry.title}\n{entry.video_name} · {utils.format_seconds(entry.duration)}"
            )
            item.setData(Qt.UserRole, i)
            item.setToolTip(f"{entry.title}\n{entry.video_name}")
            if i == current_index:
                from PySide6.QtGui import QColor, QFont

                font = QFont(item.font())
                font.setBold(True)
                item.setFont(font)
                item.setForeground(QColor("#7aa2f7"))
            self.list.addItem(item)
        self.list.blockSignals(False)

        if current_index is not None and 0 <= current_index < self.list.count():
            # Current - for the keyboard - but not selected: it's marked as
            # playing already, and a selection made to remove others mustn't
            # take it along.
            self.list.setCurrentRow(current_index, QItemSelectionModel.NoUpdate)
            self.list.scrollToItem(self.list.item(current_index))

        has_entries = bool(entries)
        self.list.setVisible(has_entries)
        self.empty.setVisible(not has_entries)
        self.clear_button.setEnabled(has_entries)
        self._update_buttons()

    def set_shuffle(self, on: bool) -> None:
        self.shuffle_button.setChecked(on)

    # --- actions ---------------------------------------------------------

    def _on_activated(self, item: QListWidgetItem) -> None:
        self.entry_activated.emit(self.list.row(item))

    def menu_for(self, item: QListWidgetItem) -> QMenu:
        entry_index = item.data(Qt.UserRole)
        menu = QMenu(self.list)
        play = menu.addAction("Play")
        play.triggered.connect(lambda: self.entry_activated.emit(entry_index))
        for label, audio_only in (("Play Audio", True), ("Play Video", False)):
            action = menu.addAction(label)
            action.triggered.connect(
                lambda _checked=False, a=audio_only: self.play_as_requested.emit(entry_index, a)
            )
        menu.addSeparator()
        remove = menu.addAction("Remove")
        chosen = self.selected_entries()
        targets = chosen if entry_index in chosen else [entry_index]
        if len(targets) > 1:
            remove.setText(f"Remove {len(targets)}")
        remove.triggered.connect(lambda: self.entries_removed.emit(targets))
        return menu

    def _show_menu(self, point) -> None:
        item = self.list.itemAt(point)
        if item is not None:
            self.menu_for(item).exec(self.list.viewport().mapToGlobal(point))

    def selected_entries(self) -> list[int]:
        return sorted(item.data(Qt.UserRole) for item in self.list.selectedItems())

    def _update_buttons(self) -> None:
        count = len(self.list.selectedItems())
        self.remove_button.setEnabled(count > 0)
        self.remove_button.setText(f"Remove {count}" if count > 1 else "Remove")

    def _remove_selected(self) -> None:
        chosen = self.selected_entries()
        if chosen:
            self.entries_removed.emit(chosen)

    def _read_order(self) -> None:
        order = [self.list.item(row).data(Qt.UserRole) for row in range(self.list.count())]
        if order != sorted(order):
            self.order_changed.emit(order)

    def _fill_playlists(self, menu: QMenu) -> None:
        menu.clear()
        if self.playlists_menu is not None:
            self.playlists_menu(menu)

    def _cycle_repeat(self) -> None:
        index = REPEAT_CYCLE.index(self._repeat)
        self._repeat = REPEAT_CYCLE[(index + 1) % len(REPEAT_CYCLE)]
        label, tooltip = REPEAT_LABELS[self._repeat]
        self.repeat_button.setText(label)
        self.repeat_button.setToolTip(tooltip)
        self.repeat_button.setChecked(self._repeat != REPEAT_OFF)
        self.repeat_changed.emit(self._repeat)
