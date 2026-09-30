# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Edit a whole tracklist at once, and fix a match that came out shifted.

The common repair is a tracklist that is right but off by one, because the
disc has an intro chapter the release doesn't list. Retyping every name to
correct that is absurd, so names can be moved up and down the list instead,
and a name pushed off the end waits in "unused" rather than being lost.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from mediabrowser.core import utils


class EditTracklistDialog(QDialog):
    def __init__(self, parent, video) -> None:
        super().__init__(parent)
        self.setWindowTitle("Edit Tracklist")
        self.setModal(True)
        self.resize(760, 700)
        self.setMinimumWidth(620)

        self.video = video
        self._titles: list[str] = [ch["title"] or "" for ch in video["chapters"]]
        self._durations = [ch["end"] - ch["start"] for ch in video["chapters"]]
        self._unused: list[str] = []

        self.tree = QTreeWidget()
        self.tree.setColumnCount(3)
        self.tree.setHeaderLabels(["#", "Length", "Title"])
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(True)
        self.tree.setSelectionMode(QAbstractItemView.SingleSelection)
        self.tree.setEditTriggers(
            QAbstractItemView.DoubleClicked | QAbstractItemView.SelectedClicked
        )
        self.tree.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.tree.itemChanged.connect(self._on_item_edited)
        self.tree.itemSelectionChanged.connect(self._update_buttons)
        header = self.tree.header()
        header.setSectionResizeMode(0, QHeaderView.Fixed)
        header.setSectionResizeMode(1, QHeaderView.Fixed)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        self.tree.setColumnWidth(0, 44)
        self.tree.setColumnWidth(1, 80)

        self.up_button = QPushButton("Shift Up")
        self.up_button.setToolTip("Move this name and every name below it up one chapter")
        self.up_button.clicked.connect(lambda: self._shift(-1))
        self.down_button = QPushButton("Shift Down")
        self.down_button.setToolTip("Move this name and every name below it down one chapter")
        self.down_button.clicked.connect(lambda: self._shift(1))
        self.clear_button = QPushButton("Clear")
        self.clear_button.setToolTip("Remove this chapter's name, leaving it numbered")
        self.clear_button.clicked.connect(self._clear_selected)

        shift_row = QHBoxLayout()
        shift_row.setContentsMargins(0, 0, 0, 0)
        shift_row.setSpacing(6)
        shift_row.addWidget(self.up_button)
        shift_row.addWidget(self.down_button)
        shift_row.addWidget(self.clear_button)
        shift_row.addStretch(1)

        hint = QLabel("Double-click a title to edit it.")
        hint.setObjectName("hintLabel")

        self.unused_list = QListWidget()
        self.unused_list.setMaximumHeight(110)
        unused_caption = QLabel("Unused names")
        unused_caption.setObjectName("sectionCaption")
        self.unused_hint = QLabel(
            "Names shifted off the end of the list. They are put back if you shift up again."
        )
        self.unused_hint.setObjectName("hintLabel")
        self.unused_hint.setWordWrap(True)

        buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
        save = buttons.addButton("Save", QDialogButtonBox.AcceptRole)
        save.setObjectName("primaryButton")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        caption = QLabel("Chapters")
        caption.setObjectName("sectionCaption")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)
        layout.addWidget(caption)
        layout.addWidget(hint)
        layout.addWidget(self.tree, 1)
        layout.addLayout(shift_row)
        layout.addWidget(unused_caption)
        layout.addWidget(self.unused_hint)
        layout.addWidget(self.unused_list)
        layout.addWidget(buttons)

        self._refresh()

    # --- results ---------------------------------------------------------

    def titles(self) -> list[str]:
        return list(self._titles)

    # --- table -----------------------------------------------------------

    def _refresh(self) -> None:
        # Repopulating fires itemChanged for every row; suppressed so the
        # rebuild isn't mistaken for the user editing every title at once.
        self.tree.blockSignals(True)
        self.tree.clear()
        for i, title in enumerate(self._titles):
            item = QTreeWidgetItem([
                str(i + 1),
                utils.format_seconds(self._durations[i]),
                title,
            ])
            item.setFlags(item.flags() | Qt.ItemIsEditable)
            item.setTextAlignment(0, Qt.AlignRight | Qt.AlignVCenter)
            item.setTextAlignment(1, Qt.AlignRight | Qt.AlignVCenter)
            if not title:
                from PySide6.QtGui import QColor

                item.setText(2, utils.title_or_number(i, title))
                item.setForeground(2, QColor("#3a3f4b"))
            self.tree.addTopLevelItem(item)
        self.tree.blockSignals(False)

        self.unused_list.clear()
        self.unused_list.addItems(self._unused)
        self.unused_list.setVisible(bool(self._unused))
        self.unused_hint.setVisible(bool(self._unused))
        self._update_buttons()

    def _on_item_edited(self, item: QTreeWidgetItem, column: int) -> None:
        if column != 2:
            return
        index = self.tree.indexOfTopLevelItem(item)
        text = item.text(2).strip()
        # An untitled row shows "Chapter 7" as a placeholder; leaving that
        # untouched must not turn it into a real title.
        if text == utils.title_or_number(index, ""):
            text = ""
        self._titles[index] = text
        self._refresh()

    def _selected(self) -> int | None:
        item = self.tree.currentItem()
        return self.tree.indexOfTopLevelItem(item) if item is not None else None

    def _clear_selected(self) -> None:
        index = self._selected()
        if index is None:
            return
        self._titles[index] = ""
        self._refresh()
        self._select(index)

    def _shift(self, direction: int) -> None:
        """Move every title from the selected row down by one chapter.

        Shifting down frees the selected chapter and pushes the last title
        into "unused"; shifting up pulls the first unused title back in.
        """
        index = self._selected()
        if index is None:
            return

        if direction > 0:
            tail = self._titles[index:]
            pushed_out = tail[-1] if tail and tail[-1] else None
            self._titles[index:] = [""] + tail[:-1]
            if pushed_out:
                self._unused.insert(0, pushed_out)
        else:
            if index + 1 >= len(self._titles) and not self._unused:
                return
            pulled_in = self._unused.pop(0) if self._unused else ""
            self._titles[index:] = self._titles[index + 1:] + [pulled_in]

        self._refresh()
        self._select(index)

    def _select(self, index: int) -> None:
        if 0 <= index < self.tree.topLevelItemCount():
            self.tree.setCurrentItem(self.tree.topLevelItem(index))

    def _update_buttons(self) -> None:
        has_selection = self._selected() is not None
        self.up_button.setEnabled(has_selection)
        self.down_button.setEnabled(has_selection)
        self.clear_button.setEnabled(has_selection)
