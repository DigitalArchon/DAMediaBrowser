# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Missing Videos: every video whose file isn't where the library last saw
it, under the folder it was in.

For when files have been moved somewhere else by mistake, or a drive or
share isn't connected: what's missing, and from where, at a glance. A
folder that isn't there at all says so - that's usually one folder moved
or renamed, or a whole share away, not many files lost one by one.

Nothing is changed here. Remove Missing Videos… hands back to the main
window, which asks before forgetting anything.
"""

from __future__ import annotations

import os

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

FOLDER_GONE = "folder not found"
INTRO = (
    "These videos' files aren't where the library last saw them, listed under the "
    "folder each was in. If they were moved by mistake, put them back and they'll "
    "be found named, as before. If a drive or network share is disconnected, "
    "reconnect it."
)


def _count(n: int) -> str:
    return f"{n} video{'' if n == 1 else 's'}"


def folder_note(group: dict) -> str:
    return FOLDER_GONE if not group["exists"] else f"{_count(len(group['videos']))} missing"


def folder_label(group: dict) -> str:
    return f"{group['folder']}  ({folder_note(group)})"


def as_text(groups: list[dict]) -> str:
    """The list as plain text, for the clipboard."""
    total = sum(len(g["videos"]) for g in groups)
    lines = [f"Missing videos ({total}), by the folder each was in:"]
    for group in groups:
        lines.append("")
        lines.append(folder_label(group))
        for _video_id, name, path in group["videos"]:
            file = os.path.basename(path)
            lines.append(f"  {name}" + (f"  -  {file}" if file != name else ""))
    return "\n".join(lines)


class MissingDialog(QDialog):
    def __init__(self, parent, groups: list[dict]) -> None:
        super().__init__(parent)
        self.setWindowTitle("Missing Videos")
        self.setModal(True)
        self.resize(760, 520)
        self.groups = groups
        # Set when the person asks to forget them; the main window does it.
        self.remove_requested = False

        total = sum(len(g["videos"]) for g in groups)
        intro = QLabel(INTRO)
        intro.setWordWrap(True)
        summary = QLabel(
            f"{_count(total)} missing, from {len(groups)} "
            f"folder{'' if len(groups) == 1 else 's'}."
        )
        summary.setObjectName("hintLabel")

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Video", "File"])
        self.tree.setSelectionMode(QAbstractItemView.SingleSelection)
        self.tree.setUniformRowHeights(True)
        # A folder's path is long and its end is what tells one from another.
        self.tree.setTextElideMode(Qt.ElideMiddle)
        header = self.tree.header()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.Interactive)
        header.resizeSection(1, 260)
        for group in groups:
            folder = QTreeWidgetItem([folder_label(group)])
            folder.setToolTip(0, group["folder"])
            font = folder.font(0)
            font.setBold(True)
            folder.setFont(0, font)
            folder.setFlags(Qt.ItemIsEnabled)
            for video_id, name, path in group["videos"]:
                row = QTreeWidgetItem([name, os.path.basename(path)])
                row.setData(0, Qt.UserRole, video_id)
                row.setToolTip(0, path)
                row.setToolTip(1, path)
                folder.addChild(row)
            self.tree.addTopLevelItem(folder)
            folder.setFirstColumnSpanned(True)
        self.tree.expandAll()

        self.copy_button = QPushButton("Copy List")
        self.copy_button.setToolTip("Copy every missing video and its folder, as text")
        self.copy_button.clicked.connect(self.copy_list)
        self.remove_button = QPushButton("Remove Missing Videos…")
        self.remove_button.setObjectName("dangerButton")
        self.remove_button.setToolTip(
            "Forget these videos and their chapter names - asks first"
        )
        self.remove_button.clicked.connect(self._remove)
        close = QDialogButtonBox(QDialogButtonBox.Close)
        close.rejected.connect(self.reject)
        buttons = QHBoxLayout()
        buttons.addWidget(self.remove_button)
        buttons.addStretch(1)
        buttons.addWidget(self.copy_button)
        buttons.addWidget(close)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)
        layout.addWidget(intro)
        layout.addWidget(summary)
        layout.addWidget(self.tree, 1)
        layout.addLayout(buttons)

    def copy_list(self) -> None:
        QApplication.clipboard().setText(as_text(self.groups))
        self.copy_button.setText("Copied")

    def _remove(self) -> None:
        self.remove_requested = True
        self.accept()
