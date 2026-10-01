# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Edit a whole tracklist at once, and fix a match that came out shifted.

The common repair is a tracklist that is right but off by one, because the
disc has an intro chapter the release doesn't list. Retyping every name to
correct that is absurd, so names can be moved up and down the list instead,
and a name pushed off the end waits in "unused" rather than being lost.

A title can also be dragged to where it belongs: the chapters stay put,
with their numbers and lengths, and only the names move. Dropped on an
unnamed chapter it fills it; dropped on a named one, or between two, it
goes there and the names in between close up. Names can be dragged out to
"unused" and back in.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
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

# Where a dragged name was dropped, relative to the row under it.
ONTO, ABOVE, BELOW = "onto", "above", "below"
# Where it came from.
FROM_TABLE, FROM_UNUSED = "table", "unused"


def _where(view) -> str:
    return {
        QAbstractItemView.AboveItem: ABOVE,
        QAbstractItemView.BelowItem: BELOW,
        QAbstractItemView.OnItem: ONTO,
    }.get(view.dropIndicatorPosition(), BELOW)


class _TitleTable(QTreeWidget):
    """The chapters, whose names can be dragged about. Qt isn't left to
    move the rows: a row is a chapter, and only its name moves."""

    dropped = Signal(str, int, int, str)  # from, its row, onto row, where

    def __init__(self) -> None:
        super().__init__()
        self.unused: QListWidget | None = None
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QAbstractItemView.DragDrop)
        self.setDefaultDropAction(Qt.MoveAction)

    def dragEnterEvent(self, event) -> None:
        if event.source() in (self, self.unused):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:
        super().dragMoveEvent(event)
        if event.source() in (self, self.unused):
            event.accept()

    def dropEvent(self, event) -> None:
        source = event.source()
        if source not in (self, self.unused) or self.topLevelItemCount() == 0:
            event.ignore()
            return
        item = self.itemAt(event.position().toPoint())
        if item is None:
            row, where = self.topLevelItemCount() - 1, BELOW
        else:
            row, where = self.indexOfTopLevelItem(item), _where(self)
        if source is self:
            kind, origin = FROM_TABLE, self.indexOfTopLevelItem(self.currentItem())
        else:
            kind, origin = FROM_UNUSED, source.currentRow()
        # Copy, not move: a move would have Qt delete the row it came from.
        event.setDropAction(Qt.CopyAction)
        event.accept()
        # After the drag has finished with the rows, which this rebuilds.
        QTimer.singleShot(0, lambda: self.dropped.emit(kind, origin, row, where))


class _UnusedList(QListWidget):
    """Names with no chapter, which a name can be dragged out to."""

    dropped = Signal(int)  # the chapter whose name was dragged here

    def __init__(self) -> None:
        super().__init__()
        self.table: QTreeWidget | None = None
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDrop)
        self.setDefaultDropAction(Qt.MoveAction)

    def dragEnterEvent(self, event) -> None:
        if event.source() is self.table:
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:
        if event.source() is self.table:
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event) -> None:
        if event.source() is not self.table:
            event.ignore()
            return
        row = self.table.indexOfTopLevelItem(self.table.currentItem())
        event.setDropAction(Qt.CopyAction)
        event.accept()
        QTimer.singleShot(0, lambda: self.dropped.emit(row))


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

        self.tree = _TitleTable()
        self.tree.dropped.connect(self.drop)
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

        hint = QLabel(
            "Double-click a title to edit it, or drag it to the chapter it belongs on - "
            "the chapters stay where they are, only the names move."
        )
        hint.setWordWrap(True)
        hint.setObjectName("hintLabel")

        self.unused_list = _UnusedList()
        self.unused_list.setMaximumHeight(110)
        self.unused_list.table = self.tree
        self.unused_list.dropped.connect(self.set_aside)
        self.tree.unused = self.unused_list
        unused_caption = QLabel("Unused names")
        unused_caption.setObjectName("sectionCaption")
        self.unused_hint = QLabel(
            "Names with no chapter: shifted off the end, or dragged here. Shifting up "
            "puts the first back; or drag one onto the chapter it belongs on."
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
            item.setFlags(item.flags() | Qt.ItemIsEditable | Qt.ItemIsDragEnabled
                          | Qt.ItemIsDropEnabled)
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

    # --- dragging --------------------------------------------------------

    def drop(self, kind: str, origin: int, row: int, where: str) -> None:
        """A name dragged onto chapter `row` (ONTO it, or ABOVE or BELOW
        it), from the table's row `origin` or the unused list's."""
        n = len(self._titles)
        if not 0 <= row < n:
            return
        if kind == FROM_TABLE:
            if not 0 <= origin < n:
                return
            title = self._titles[origin]
            if not title:
                return
            if where == ONTO and not self._titles[row]:
                self._titles[row], self._titles[origin] = title, ""
                target = row
            else:
                # Its row once the name has left the one it was on.
                if where == ONTO:
                    target = row
                elif where == ABOVE:
                    target = row if origin > row else row - 1
                else:
                    target = row + 1 if origin > row else row
                target = max(0, min(target, n - 1))
                del self._titles[origin]
                self._titles.insert(target, title)
        else:
            if not 0 <= origin < len(self._unused):
                return
            title = self._unused.pop(origin)
            if where == ONTO and not self._titles[row]:
                self._titles[row] = title
                target = row
            else:
                target = row + 1 if where == BELOW else row
                self._titles.insert(target, title)
                # One name too many now: the first gap after it closes up,
                # else the last name waits in unused.
                gap = next((i for i in range(target + 1, len(self._titles))
                            if not self._titles[i]), None)
                if gap is not None:
                    del self._titles[gap]
                else:
                    pushed = self._titles.pop()
                    if pushed:
                        self._unused.insert(0, pushed)
                target = min(target, n - 1)
        self._refresh()
        self._select(target)

    def set_aside(self, row: int) -> None:
        """A chapter's name dragged out to unused: the chapter is left
        unnamed, its name kept."""
        if not 0 <= row < len(self._titles) or not self._titles[row]:
            return
        self._unused.append(self._titles[row])
        self._titles[row] = ""
        self._refresh()
        self._select(row)

    def _select(self, index: int) -> None:
        if 0 <= index < self.tree.topLevelItemCount():
            self.tree.setCurrentItem(self.tree.topLevelItem(index))

    def _update_buttons(self) -> None:
        has_selection = self._selected() is not None
        self.up_button.setEnabled(has_selection)
        self.down_button.setEnabled(has_selection)
        self.clear_button.setEnabled(has_selection)
