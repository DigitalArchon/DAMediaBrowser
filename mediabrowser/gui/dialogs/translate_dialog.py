# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Romanise a video's existing chapter titles.

A disc's embedded chapter names, or a tracklist from a Japanese release,
leave the songs unsearchable by the names anyone outside Japan knows them
by. That name is rarely a translation: "いいね!" is released in the West as
"Iine!", not "So Good". This asks the model for each title as the song is
officially known in English-language releases - romanised, or its
official English title where it has one -
shows the before and after, and keeps the original on the chapter so a
search in either script finds it.

Nothing is sent when it opens: it lists the titles and says where they would
go, and only Romanise sends them.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QHeaderView,
    QLabel,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from mediabrowser.core import ai, ai_chapters
from mediabrowser.gui.worker import run_job


class TranslateDialog(QDialog):
    def __init__(self, parent, video: dict) -> None:
        super().__init__(parent)
        self.setWindowTitle("Romanise Titles")
        self.setModal(True)
        self.resize(720, 520)
        self._jobs: list = []
        self._settings = ai.load_settings()
        # (chapter_index, current title) for every chapter that has one.
        self._titled = [(i, ch["title"]) for i, ch in enumerate(video["chapters"]) if ch["title"]]
        self._translated: list[tuple[str, str | None]] | None = None

        self.tree = QTreeWidget()
        self.tree.setColumnCount(4)
        self.tree.setHeaderLabels(["#", "Now", "Romanised / English", "Kept as original"])
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(True)
        self.tree.setSelectionMode(QAbstractItemView.NoSelection)
        header = self.tree.header()
        header.setSectionResizeMode(0, QHeaderView.Fixed)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        header.setSectionResizeMode(3, QHeaderView.Stretch)
        self.tree.setColumnWidth(0, 40)

        self.status = QLabel("")
        self.status.setObjectName("hintLabel")
        self.status.setTextFormat(Qt.PlainText)
        self.status.setWordWrap(True)

        buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
        self.romanise_button = buttons.addButton("Romanise", QDialogButtonBox.ActionRole)
        self.romanise_button.setObjectName("primaryButton")
        self.romanise_button.clicked.connect(self.start)
        self.apply_button = buttons.addButton("Apply", QDialogButtonBox.AcceptRole)
        self.apply_button.setEnabled(False)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)
        layout.addWidget(self.tree, 1)
        layout.addWidget(self.status)
        layout.addWidget(buttons)

        for i, title in self._titled:
            item = QTreeWidgetItem([str(i + 1), title, "", ""])
            item.setTextAlignment(0, Qt.AlignRight | Qt.AlignVCenter)
            self.tree.addTopLevelItem(item)
        self.romanise_button.setEnabled(self._ready())
        if self._ready():
            where, _protection = ai.destination(self._settings)
            self.status.setText(
                f"Romanise sends these {len(self._titled)} titles {where}. One AI "
                "request, a fraction of a cent; nothing changes until Apply.")

    def _ready(self) -> bool:
        """Whether there's anything to send, and somewhere it may go - saying
        why not when there isn't."""
        if not self._titled:
            self.status.setText("None of these chapters has a name yet.")
            return False
        if not ai.is_configured(self._settings):
            self.status.setText(ai.not_ready(self._settings))
            return False
        return True

    def start(self) -> None:
        if self._jobs or not self._ready():
            return
        settings = self._settings
        titles = [title for _, title in self._titled]
        where, _protection = ai.destination(settings)
        self.romanise_button.setEnabled(False)
        self.status.setText(f"Sending {len(titles)} titles {where}…")
        self._jobs.append(run_job(
            self,
            lambda: ai_chapters.translate_titles(titles, settings),
            on_done=self._show,
            on_failed=self._failed,
            purpose="Romanise Titles",
        ))

    def _failed(self, message: str) -> None:
        self._jobs.clear()
        self.romanise_button.setEnabled(True)
        self.status.setText(f"That didn't work: {message}")

    def _show(self, translated) -> None:
        self._translated = translated
        changed = 0
        for row, (title, original) in enumerate(translated):
            item = self.tree.topLevelItem(row)
            item.setText(2, title)
            item.setText(3, original or "")
            if title == self._titled[row][1]:
                item.setForeground(2, QColor("#565c69"))
            else:
                changed += 1
        self.status.setText(
            f"{changed} title(s) would change; the rest are already as they're known "
            "in English." if changed else
            "Every title is already as it's known in English - nothing to change."
        )
        self.apply_button.setEnabled(changed > 0)
        if changed:
            self.apply_button.setObjectName("primaryButton")
            self.apply_button.style().unpolish(self.apply_button)
            self.apply_button.style().polish(self.apply_button)

    def mapping(self) -> list[tuple[int, str, str | None]]:
        """(chapter_index, title, original_title) for each title that
        changes - what Apply writes."""
        if not self._translated:
            return []
        return [
            (index, title, original)
            for (index, current), (title, original) in zip(
                self._titled, self._translated, strict=True
            )
            if title != current
        ]
