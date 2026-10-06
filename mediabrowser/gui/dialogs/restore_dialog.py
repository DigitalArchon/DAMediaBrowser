# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Restore Library: the backups kept before a library was forgotten, folded
into another, moved or restored into - newest first, each saying what it
holds and what bringing it back would do.

Restoring never loses what's here now (store.restore_backup), so it asks
nothing more; deleting a backup is for good, so that asks first.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from mediabrowser.core import sharing, store

NONE_YET = (
    "There are no backups yet. One is kept whenever a library is forgotten, folded "
    "into a library around it, moved, or restored into."
)


class RestoreDialog(QDialog):
    def __init__(self, parent) -> None:
        super().__init__(parent)
        self.setWindowTitle("Restore Library")
        self.setModal(True)
        self.resize(640, 520)
        self.restored: list[str] = []
        self._backups: list[dict] = []

        intro = QLabel("Backups of libraries, newest first. Pick one to see what it holds.")
        intro.setWordWrap(True)
        self.list = QListWidget()
        self.list.currentRowChanged.connect(self._show_detail)
        self.list.itemActivated.connect(lambda _item: self.restore())
        self.detail = QLabel()
        self.detail.setObjectName("hintLabel")
        self.detail.setWordWrap(True)
        self.detail.setTextFormat(Qt.PlainText)
        self.detail.setMinimumHeight(140)
        self.detail.setAlignment(Qt.AlignTop | Qt.AlignLeft)

        self.restore_button = QPushButton("Restore")
        self.restore_button.setObjectName("primaryButton")
        self.restore_button.clicked.connect(self.restore)
        self.delete_button = QPushButton("Delete Backup…")
        self.delete_button.setObjectName("dangerButton")
        self.delete_button.clicked.connect(self.delete)
        close = QDialogButtonBox(QDialogButtonBox.Close)
        close.rejected.connect(self.reject)
        buttons = QHBoxLayout()
        buttons.addWidget(self.delete_button)
        buttons.addStretch(1)
        buttons.addWidget(self.restore_button)
        buttons.addWidget(close)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)
        layout.addWidget(intro)
        layout.addWidget(self.list, 1)
        layout.addWidget(self.detail)
        layout.addLayout(buttons)
        self.refresh()

    def refresh(self) -> None:
        self._backups = store.list_backups()
        self.list.clear()
        for backup in self._backups:
            self.list.addItem(QListWidgetItem(sharing.backup_title(backup)))
        if self._backups:
            self.list.setCurrentRow(0)
        else:
            self._show_detail(-1)

    def selected(self) -> dict | None:
        row = self.list.currentRow()
        return self._backups[row] if 0 <= row < len(self._backups) else None

    def _show_detail(self, _row: int) -> None:
        backup = self.selected()
        self.detail.setText(sharing.backup_detail(backup) if backup else NONE_YET)
        self.restore_button.setEnabled(backup is not None)
        self.delete_button.setEnabled(backup is not None)

    def restore(self) -> None:
        backup = self.selected()
        if backup is None:
            return
        try:
            self.restored = store.restore_backup(backup["path"])
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "Couldn't restore", str(exc))
            return
        self.accept()

    def confirm_delete(self, backup: dict) -> bool:
        box = QMessageBox(QMessageBox.Warning, "Delete Backup",
                          f"Delete the backup “{sharing.backup_title(backup)}”?",
                          QMessageBox.Cancel, self)
        box.setTextFormat(Qt.PlainText)
        box.setInformativeText("It's deleted for good: it can't be restored afterwards.")
        go = box.addButton("Delete Backup", QMessageBox.DestructiveRole)
        box.setDefaultButton(QMessageBox.Cancel)
        box.exec()
        return box.clickedButton() is go

    def delete(self) -> None:
        backup = self.selected()
        if backup is None or not self.confirm_delete(backup):
            return
        store.delete_backup(backup["path"])
        self.refresh()
