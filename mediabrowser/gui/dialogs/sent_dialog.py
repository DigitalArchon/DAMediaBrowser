# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""What Was Sent: every request the app has made since it started.

Settings → Privacy says what each service may be sent; this says what it
was sent - which service, what it carried in words, how it travelled, and
whether it went - from core.outbound's list, as each request is made. A
request picked shows the rest: what it was for, the address, what an
enclave proved, and for the AI, the words it was sent (the frames left out).

The list is kept in memory only and is gone when the app closes: it names
the videos, as the requests did. Copy puts it on the clipboard; Clear
empties it.
"""

from __future__ import annotations

import time

from PySide6.QtCore import QObject, Qt, Signal, Slot
from PySide6.QtGui import QBrush, QColor, QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QHeaderView,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from mediabrowser.core import outbound

INTRO = (
    "Everything this app has sent since it started: to whom, what it carried, and how "
    "it travelled. The list is kept in memory only, and is gone when the app closes."
)
EMPTY = "Nothing has been sent since the app started."

HOW = {
    outbound.SEALED: "sealed end to end",
    outbound.TEE: "to an attested enclave",
    outbound.CLEAR: "over https",
    outbound.CHECK: "an attestation",
}
RESULT = {outbound.SENT: "sent", outbound.REFUSED: "refused", outbound.FAILED: "failed"}
# The stylesheet's semantic colours (style.qss).
OK, WARNING, ERROR = QColor("#9ece6a"), QColor("#e0af68"), QColor("#f7768e")


class _Bridge(QObject):
    """Carries a new entry from whichever thread sent it to the GUI thread."""

    arrived = Signal(object)


def summary(entries) -> str:
    """"14 requests: 3 sealed end to end, 9 over https, 2 attestations; 1
    refused." for the line above the list."""
    if not entries:
        return EMPTY
    counts = {}
    for entry in entries:
        counts[entry.protection] = counts.get(entry.protection, 0) + 1
    parts = []
    for protection, one, many in (
        (outbound.SEALED, "sealed end to end", "sealed end to end"),
        (outbound.TEE, "to an attested enclave", "to attested enclaves"),
        (outbound.CLEAR, "over https", "over https"),
        (outbound.CHECK, "attestation", "attestations"),
    ):
        count = counts.get(protection, 0)
        if count:
            parts.append(f"{count} {one if count == 1 else many}")
    refused = sum(1 for e in entries if e.outcome == outbound.REFUSED)
    failed = sum(1 for e in entries if e.outcome == outbound.FAILED)
    tail = [f"{refused} refused before anything was sent"] if refused else []
    tail += [f"{failed} failed"] if failed else []
    total = len(entries)
    text = f"{total} request{'s' if total != 1 else ''}: {', '.join(parts)}"
    return text + (f"; {', '.join(tail)}." if tail else ".")


def describe(entry: outbound.Entry) -> str:
    """One entry in full, for the pane below the list and for Copy."""
    when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(entry.when))
    lines = [f"{when}  {entry.service} ({entry.host or 'no address'})"]
    if entry.purpose:
        lines.append(f"For: {entry.purpose}")
    lines.append(f"What: {entry.what}")
    lines.append(f"How: {entry.how}")
    result = RESULT.get(entry.outcome, entry.outcome)
    if entry.outcome == outbound.REFUSED:
        result += ", and nothing was sent"
    lines.append(f"Result: {result}" + (f" - {entry.detail}" if entry.detail else ""))
    if entry.content:
        lines += ["", "What it carried, the frames left out:", entry.content]
    return "\n".join(lines)


class SentDialog(QDialog):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("What Was Sent")
        self.resize(900, 600)
        self._entries: list[outbound.Entry] = []

        intro = QLabel(INTRO)
        intro.setObjectName("hintLabel")
        intro.setWordWrap(True)
        self.summary = QLabel()
        self.summary.setObjectName("toneLabel")
        self.summary.setWordWrap(True)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(5)
        self.tree.setHeaderLabels(["Time", "To", "What", "How", "Result"])
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(True)
        self.tree.setSelectionMode(QAbstractItemView.SingleSelection)
        self.tree.setUniformRowHeights(True)
        header = self.tree.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        for column, width in ((0, 70), (1, 140), (3, 150), (4, 150)):
            header.setSectionResizeMode(column, QHeaderView.Interactive)
            self.tree.setColumnWidth(column, width)
        self.tree.currentItemChanged.connect(lambda item, _old: self._show(item))

        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setPlaceholderText("Pick a request to see what it carried.")

        splitter = QSplitter(Qt.Vertical)
        splitter.addWidget(self.tree)
        splitter.addWidget(self.details)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        self.copy_button = QPushButton("Copy")
        self.copy_button.setToolTip("Put the whole list on the clipboard, in full")
        self.copy_button.clicked.connect(self.copy)
        self.clear_button = QPushButton("Clear")
        self.clear_button.setToolTip("Empty the list. Nothing else is changed.")
        self.clear_button.clicked.connect(self.clear)
        buttons.addButton(self.copy_button, QDialogButtonBox.ActionRole)
        buttons.addButton(self.clear_button, QDialogButtonBox.ActionRole)
        buttons.rejected.connect(self.close)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)
        layout.addWidget(intro)
        layout.addWidget(self.summary)
        layout.addWidget(splitter, 1)
        layout.addWidget(buttons)

        self._bridge = _Bridge()
        self._bridge.arrived.connect(self._arrived)
        outbound.add_listener(self._bridge.arrived.emit)
        for entry in outbound.entries():
            self._add(entry)
        self._update_summary()

    def done(self, result: int) -> None:
        outbound.remove_listener(self._bridge.arrived.emit)
        super().done(result)

    def closeEvent(self, event) -> None:
        outbound.remove_listener(self._bridge.arrived.emit)
        super().closeEvent(event)

    def showEvent(self, event) -> None:
        # Shown again after it was closed: listening again, and up to date.
        outbound.remove_listener(self._bridge.arrived.emit)
        outbound.add_listener(self._bridge.arrived.emit)
        self.reload()
        super().showEvent(event)

    def reload(self) -> None:
        self.tree.clear()
        self._entries = []
        for entry in outbound.entries():
            self._add(entry)
        self._update_summary()

    @Slot(object)
    def _arrived(self, entry) -> None:
        self._add(entry)
        self._update_summary()

    def _add(self, entry: outbound.Entry) -> None:
        self._entries.append(entry)
        item = QTreeWidgetItem([
            time.strftime("%H:%M:%S", time.localtime(entry.when)),
            entry.service,
            entry.what,
            HOW.get(entry.protection, entry.protection),
            RESULT.get(entry.outcome, entry.outcome),
        ])
        item.setData(0, Qt.UserRole, len(self._entries) - 1)
        item.setToolTip(2, entry.what)
        item.setToolTip(3, entry.how)
        if entry.detail:
            item.setToolTip(4, entry.detail)
        if entry.protection == outbound.SEALED:
            item.setForeground(3, QBrush(OK))
        elif entry.protection == outbound.TEE:
            item.setForeground(3, QBrush(WARNING))
        if entry.outcome == outbound.REFUSED:
            item.setForeground(4, QBrush(WARNING))
        elif entry.outcome == outbound.FAILED:
            item.setForeground(4, QBrush(ERROR))
        self.tree.addTopLevelItem(item)

    def _update_summary(self) -> None:
        self.summary.setText(summary(self._entries))
        self.copy_button.setEnabled(bool(self._entries))
        self.clear_button.setEnabled(bool(self._entries))

    def _show(self, item) -> None:
        if item is None:
            self.details.clear()
            return
        entry = self._entries[item.data(0, Qt.UserRole)]
        self.details.setPlainText(describe(entry))

    def copy(self) -> None:
        text = "\n\n".join(describe(entry) for entry in self._entries)
        QGuiApplication.clipboard().setText(summary(self._entries) + "\n\n" + text)

    def clear(self) -> None:
        outbound.clear()
        self.reload()
        self.details.clear()
