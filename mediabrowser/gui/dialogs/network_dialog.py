# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Add a folder on a network share as a library.

Lists the shares the file manager is connected to or has bookmarked, or
takes an address (smb://server/share/folder). Connecting turns the address
into a local folder through the desktop's FUSE bridge - see core.network -
and then a folder inside it is chosen the usual way. The address goes with
the library, so a later rescan can reconnect it.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
)

from mediabrowser.core import network
from mediabrowser.gui.plain import tooltip
from mediabrowser.gui.worker import run_job

INSTRUCTIONS = (
    "Pick a share your file manager is connected to, or type its address. "
    "The share is connected if it isn't already, and then you choose the "
    "folder in it to use as a library."
)


class NetworkFolderDialog(QDialog):
    def __init__(self, parent) -> None:
        super().__init__(parent)
        self.setWindowTitle("Add Network Folder")
        self.setModal(True)
        self.resize(560, 460)

        self._jobs: list = []
        self._result: tuple[str, str] | None = None

        instructions = QLabel(INSTRUCTIONS)
        instructions.setObjectName("hintLabel")
        instructions.setWordWrap(True)

        caption = QLabel("Connected and bookmarked")
        caption.setObjectName("sectionCaption")
        self.locations = QListWidget()
        self.locations.setSelectionMode(QAbstractItemView.SingleSelection)
        self.locations.currentItemChanged.connect(self._on_location_chosen)
        self.locations.itemActivated.connect(lambda _item: self.connect_to())
        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.clicked.connect(self.refresh)

        caption_row = QHBoxLayout()
        caption_row.setContentsMargins(0, 0, 0, 0)
        caption_row.addWidget(caption)
        caption_row.addStretch(1)
        caption_row.addWidget(self.refresh_button)

        self.address = QLineEdit()
        self.address.setPlaceholderText("smb://server/share/folder")
        self.address.textChanged.connect(self._update_buttons)
        self.address.returnPressed.connect(self.connect_to)

        self.status = QLabel("")
        self.status.setObjectName("hintLabel")
        self.status.setTextFormat(Qt.PlainText)
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextSelectableByMouse)

        buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
        self.connect_button = buttons.addButton(
            "Connect and Choose Folder…", QDialogButtonBox.ActionRole
        )
        self.connect_button.setObjectName("primaryButton")
        self.connect_button.clicked.connect(self.connect_to)
        buttons.rejected.connect(self.reject)

        address_caption = QLabel("Address")
        address_caption.setObjectName("sectionCaption")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)
        layout.addWidget(instructions)
        layout.addLayout(caption_row)
        layout.addWidget(self.locations, 1)
        layout.addWidget(address_caption)
        layout.addWidget(self.address)
        layout.addWidget(self.status)
        layout.addWidget(buttons)

        self.refresh()

    # --- results ---------------------------------------------------------

    def result_folder(self) -> tuple[str, str] | None:
        """(local folder, its network address), once accepted."""
        return self._result

    # --- locations -------------------------------------------------------

    def refresh(self) -> None:
        self.locations.clear()
        for location in network.known_locations():
            state = "connected" if location.connected else "bookmark"
            item = QListWidgetItem(f"{location.name}\n{location.uri}  ·  {state}")
            item.setData(Qt.UserRole, location.uri)
            item.setToolTip(tooltip(location.uri))
            self.locations.addItem(item)
        if not self.locations.count():
            self._set_status(
                "Nothing connected or bookmarked. Type the address of a share, or "
                "connect to it in your file manager first and press Refresh."
            )
        self._update_buttons()

    def _on_location_chosen(self, item, _previous=None) -> None:
        if item is not None:
            self.address.setText(item.data(Qt.UserRole))

    # --- connecting ------------------------------------------------------

    def connect_to(self) -> None:
        uri = self.address.text().strip()
        if not network.is_network_uri(uri):
            self._set_status(
                "That isn't a network address. It should start with smb://, sftp://, "
                "ftp://, dav:// or nfs://.",
                error=True,
            )
            return
        typed, uri = uri, network.normalise(uri)
        note = ""
        if network.without_password(typed) != typed:
            # Not kept, nor sent on by the app: the desktop asks for it once
            # and keeps it in the keyring.
            self.address.setText(uri)
            note = (" The password isn't kept: if the share asks for one, connect to it "
                    "in your file manager and choose to remember it.")
        self._set_status(f"Connecting to {network.display_name(uri)}…{note}")
        self.connect_button.setEnabled(False)
        self._jobs.append(run_job(
            self,
            lambda: network.resolve(uri),
            on_done=lambda path: self._connected(uri, path),
            on_failed=self._failed,
        ))

    def _connected(self, uri: str, path: str) -> None:
        self._update_buttons()
        self._set_status(f"Connected: {path}")
        chosen = QFileDialog.getExistingDirectory(
            self, f"Choose the library folder on {network.display_name(uri)}", path
        )
        if not chosen:
            return
        try:
            folder_uri = network.subfolder_uri(uri, path, chosen)
        except ValueError:
            self._set_status(
                f"{chosen} isn't on that share - choose a folder inside {path}.",
                error=True,
            )
            return
        self._result = (str(Path(chosen)), folder_uri)
        self.accept()

    def _failed(self, message: str) -> None:
        self._update_buttons()
        self._set_status(message, error=True)

    def _set_status(self, text: str, error: bool = False) -> None:
        self.status.setText(text)
        self.status.setObjectName("errorLabel" if error else "hintLabel")
        self.status.style().unpolish(self.status)
        self.status.style().polish(self.status)

    def _update_buttons(self) -> None:
        self.connect_button.setEnabled(bool(self.address.text().strip()))
