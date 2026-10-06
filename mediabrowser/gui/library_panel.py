# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""The scanned folders, down the left.

This used to be a dialog reached from a "Switch Library..." button, which
made moving between two libraries a three-click round trip. Listing them
permanently costs nothing and makes the app's shape obvious: these are the
folders it knows about.

Each has a tick box: every library ticked is on the shelf together.
Double-clicking one (or Open) shows it alone.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from mediabrowser.core import network, protection, sharing, store
from mediabrowser.gui.plain import tooltip

LIBRARY_FLAG_TEXT = {
    protection.LOCKED: (
        "Lock Library",
        "Nothing in it changes - no renaming, no chapter edits, no scans or "
        "identifying altering it - whatever each video's own setting.",
    ),
    protection.PRIVATE: (
        "Make Library Private",
        "Its videos can be changed by hand and detected locally, but nothing about "
        "any of them is sent to MusicBrainz or the AI.",
    ),
}


class LibraryPanel(QWidget):
    library_chosen = Signal(str)  # show this one alone
    libraries_chosen = Signal(list)  # show these together
    reset_requested = Signal(str)  # root
    add_requested = Signal()
    network_requested = Signal()
    flag_toggled = Signal(str, str, bool)  # root, protection flag, on
    locate_requested = Signal(str, bool)  # root, on a network share
    forgotten = Signal(str)  # root
    restore_requested = Signal()
    erase_requested = Signal(str)  # root: delete its data for good

    def __init__(self) -> None:
        super().__init__()
        self._roots: list[str] = []
        self._current: list[str] = []
        self.setMinimumWidth(170)

        self.list = QListWidget()
        self.list.setObjectName("libraryList")
        self.list.setSelectionMode(QAbstractItemView.SingleSelection)
        # "MusicVids on mediaserver.local" is wider than the panel; wrapping
        # beats cutting it off.
        self.list.setWordWrap(True)
        self.list.itemActivated.connect(self._on_activated)
        self.list.itemChanged.connect(self._on_ticked)
        self.list.itemSelectionChanged.connect(self._update_buttons)
        self.list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._on_context_menu)
        self._flags: dict[str, dict[str, bool]] = {}

        self.add_button = QPushButton("Add Folder…")
        self.add_button.clicked.connect(self.add_requested)
        # Shorter than the menu's "Add Network Folder…": the panel is narrow.
        self.network_button = QPushButton("Add Network…")
        self.network_button.setToolTip(
            "A folder on a network share (smb://, sftp://...) your file manager can reach"
        )
        self.network_button.clicked.connect(self.network_requested)
        self.hint = QLabel("Tick several to see them together.")
        self.hint.setObjectName("hintLabel")
        self.hint.setWordWrap(True)
        self.open_button = QPushButton("Open")
        self.open_button.setToolTip("Show the selected library alone")
        self.open_button.clicked.connect(self._open_selected)
        self.forget_button = QPushButton("Forget")
        self.forget_button.setObjectName("dangerButton")
        self.forget_button.setToolTip(
            "Forget this library. Titles and chapters another library shares are kept;\n"
            "it says what would be lost first. Your media files are never touched."
        )
        self.forget_button.clicked.connect(self._forget_selected)
        self.restore_button = QPushButton("Restore…")
        self.restore_button.setToolTip(
            "Bring back a library from the backup kept when it was forgotten,\n"
            "folded into another or moved"
        )
        self.restore_button.clicked.connect(self.restore_requested)

        buttons = QHBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.setSpacing(6)
        buttons.addWidget(self.open_button)
        buttons.addWidget(self.forget_button)
        buttons.addStretch(1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)
        layout.addWidget(self.add_button)
        layout.addWidget(self.network_button)
        layout.addWidget(self.list, 1)
        layout.addWidget(self.hint)
        layout.addLayout(buttons)
        # A row of its own: the panel is too narrow for three side by side.
        layout.addWidget(self.restore_button)

        self.refresh()

    # --- population ------------------------------------------------------

    def refresh(self, current=None) -> None:
        """Reload the list, with the libraries on show - `current`, a root or
        a list of them, or whichever were before - ticked."""
        if current is not None:
            roots = [current] if isinstance(current, str) else list(current)
            self._current = [str(Path(root).resolve()) for root in roots if root]

        self.list.blockSignals(True)
        self.list.clear()
        self._roots = []
        self._flags = {}

        for lib in store.list_libraries():
            root = lib["root"]
            uri = lib.get("network_uri")
            self._roots.append(root)
            # A share's local folder is named by the bridge
            # ("smb-share:server=nas,share=media"), so a network library is
            # named from its address instead.
            name = network.display_name(uri) if uri else (Path(root).name or root)
            item = QListWidgetItem(name)
            # Folder names alone are ambiguous - two libraries can both be
            # called "Videos" - so the full path is always one hover away.
            tip = f"{network.without_password(uri)}\n{root}" if uri else root
            item.setData(Qt.UserRole, root)
            item.setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            count = lib["video_count"]
            text = f"{name}\n{count} video{'' if count == 1 else 's'}"
            self._flags[root] = {flag: bool(lib.get(flag)) for flag in protection.FLAGS}
            within = lib.get("within")
            if within:
                text += f" · in {Path(within).name or within}"
                tip += (f"\n\nInside the {Path(within).name or within} library: they share "
                        "titles and chapters.")
            if lib.get("locked"):
                text += " · Locked"
                tip += f"\n\n{protection.LOCKED_TIP}"
            elif lib.get("private"):
                text += " · Private"
                tip += f"\n\n{protection.PRIVATE_TIP}"
            item.setText(text)
            item.setToolTip(tooltip(tip))
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if root in self._current else Qt.Unchecked)
            self.list.addItem(item)
            if self._current and root == self._current[0]:
                self.list.setCurrentItem(item)

        self.list.blockSignals(False)
        self.hint.setVisible(len(self._roots) > 1)
        self._update_buttons()

    def ticked(self) -> list[str]:
        """The ticked libraries, in the order listed."""
        return [self.list.item(row).data(Qt.UserRole) for row in range(self.list.count())
                if self.list.item(row).checkState() == Qt.Checked]

    def _on_ticked(self, _item: QListWidgetItem) -> None:
        roots = self.ticked()
        if roots:
            self.libraries_chosen.emit(roots)
        else:
            # The shelf is never empty: untick the last, and it stays on.
            self.refresh()

    def selected_root(self) -> str | None:
        item = self.list.currentItem()
        return item.data(Qt.UserRole) if item is not None else None

    # --- actions ---------------------------------------------------------

    def flag(self, root: str, flag: str) -> bool:
        return self._flags.get(root, {}).get(flag, False)

    def _on_context_menu(self, point) -> None:
        item = self.list.itemAt(point)
        if item is None:
            return
        self.context_menu_for(item.data(Qt.UserRole)).exec(
            self.list.viewport().mapToGlobal(point)
        )

    def context_menu_for(self, root: str) -> QMenu:
        menu = QMenu(self)
        open_action = QAction("Open", menu)
        open_action.triggered.connect(lambda: self.library_chosen.emit(root))
        menu.addAction(open_action)
        menu.addSeparator()
        reset = QAction("Reset to Defaults…", menu)
        reset.setToolTip("Clear everything added to it and read it afresh; can be undone")
        reset.setStatusTip(reset.toolTip())
        reset.triggered.connect(lambda: self.reset_requested.emit(root))
        menu.addAction(reset)
        menu.addSeparator()
        for label, on_network, tip in (
            ("Locate Moved Folder…", False,
             "It was renamed or moved, or the drive is mounted somewhere else: point to "
             "where it is now, and its titles and chapters go with it."),
            ("Locate on Network…", True,
             "The NAS has a new address, or the library is on a new NAS: connect to it "
             "there, and its titles and chapters go with it."),
        ):
            action = QAction(label, menu)
            action.setToolTip(tip)
            action.setStatusTip(tip)
            action.triggered.connect(
                lambda _checked=False, n=on_network: self.locate_requested.emit(root, n)
            )
            menu.addAction(action)
        menu.addSeparator()
        for flag, (label, tip) in LIBRARY_FLAG_TEXT.items():
            action = QAction(label, menu)
            action.setCheckable(True)
            action.setChecked(self.flag(root, flag))
            action.setToolTip(tip)
            action.setStatusTip(tip)
            action.toggled.connect(
                lambda on, f=flag: self.flag_toggled.emit(root, f, on)
            )
            menu.addAction(action)
        menu.addSeparator()
        erase = QAction("Delete Library Data…", menu)
        erase.setToolTip(
            "Permanently delete every title and chapter recorded for this library, "
            "backups included - unlike Forget, nothing can be restored. Its videos are "
            "never touched."
        )
        erase.setStatusTip(erase.toolTip())
        erase.triggered.connect(lambda: self.erase_requested.emit(root))
        menu.addAction(erase)
        menu.setToolTipsVisible(True)
        return menu

    def _on_activated(self, item: QListWidgetItem) -> None:
        self.library_chosen.emit(item.data(Qt.UserRole))

    def _open_selected(self) -> None:
        root = self.selected_root()
        if root is not None:
            self.library_chosen.emit(root)

    def _forget_selected(self) -> None:
        root = self.selected_root()
        if root is None:
            return
        if not self._confirm_forget(root):
            return
        store.delete_library(root)
        self.refresh()
        self.forgotten.emit(root)

    def _confirm_forget(self, root: str) -> bool:
        """What forgetting it loses and what other libraries keep, then
        Forget or Cancel (the default)."""
        box = QMessageBox(QMessageBox.Warning, "Forget Library",
                          f"Forget the library “{sharing.name(root)}”?\n\n{root}",
                          QMessageBox.Cancel, self)
        box.setTextFormat(Qt.PlainText)
        box.setInformativeText(sharing.forgetting(root))
        forget = box.addButton("Forget", QMessageBox.DestructiveRole)
        box.setDefaultButton(QMessageBox.Cancel)
        box.exec()
        return box.clickedButton() is forget

    def _update_buttons(self) -> None:
        has_selection = self.list.currentItem() is not None
        self.open_button.setEnabled(has_selection)
        self.forget_button.setEnabled(has_selection)
