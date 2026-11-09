# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""The status bar's line on what goes online, always there.

It says where the app can send things as Settings has it now - "Online:
MusicBrainz · Covers · AI", or "Nothing goes online" - with each place, and
why one is off, on hover. Whenever something is sent it says, for a few
seconds, where it went: covers are fetched in the background, by
themselves, and this is where that shows. A click opens What Was Sent.

Settings can be changed from more than one window (the main window's,
Detect Chapters'), so whichever saves them says so through
settings_changed(), and every line refreshes.
"""

from __future__ import annotations

from PySide6.QtCore import QObject, Qt, QTimer, Signal, Slot
from PySide6.QtWidgets import QPushButton

from mediabrowser.core import outbound, reach

# How long a request just sent is shown, in milliseconds.
FLASH_MS = 4000


class _Hub(QObject):
    """Settings saved, from any window, to every line."""

    settings_changed = Signal()


_hub: _Hub | None = None


def hub() -> _Hub:
    global _hub
    if _hub is None:
        _hub = _Hub()
    return _hub


def settings_changed() -> None:
    """Settings were saved: every network line says so now."""
    hub().settings_changed.emit()


class _Bridge(QObject):
    """Carries an entry from whichever thread sent it to the GUI thread."""

    arrived = Signal(object)


class NetworkStatus(QPushButton):
    """`open_sent` shows What Was Sent."""

    def __init__(self, open_sent, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("networkButton")
        self.setFlat(True)
        self.setCursor(Qt.PointingHandCursor)
        self.clicked.connect(open_sent)
        self._steady = ""
        self._overview = ""
        self._flash = QTimer(self)
        self._flash.setSingleShot(True)
        self._flash.setInterval(FLASH_MS)
        self._flash.timeout.connect(self._settle)
        self._bridge = _Bridge(self)
        self._bridge.arrived.connect(self._sent)
        # One callable, kept, so that the one added is the one taken off.
        self._listener = self._bridge.arrived.emit
        outbound.add_listener(self._listener)
        self.destroyed.connect(lambda _obj=None, listener=self._listener:
                               outbound.remove_listener(listener))
        hub().settings_changed.connect(self.refresh)
        self.refresh()

    def refresh(self) -> None:
        """As Settings has it now."""
        found = reach.services()
        self._steady = reach.status_text(found)
        self._overview = reach.overview(found)
        tone = "" if any(service.on for service in found.values()) else "off"
        self.setProperty("tone", tone)
        self.style().unpolish(self)
        self.style().polish(self)
        if not self._flash.isActive():
            self.setText(self._steady)
        self._update_tip()

    def _update_tip(self) -> None:
        sent = len(outbound.entries())
        count = (f"{sent} request{'s' if sent != 1 else ''} sent since the app started."
                 if sent else "Nothing sent since the app started.")
        self.setToolTip(
            "Where this app can send things, as Settings → Privacy has it now:\n"
            f"{self._overview}\n\n{reach.PRIVATE_SKIPPED}\n{count} Click to see "
            "what, to whom, and how.")

    @Slot(object)
    def _sent(self, entry: outbound.Entry) -> None:
        if entry.seq <= outbound.cleared_through():
            return
        where = entry.host or entry.service
        if entry.outcome == outbound.REFUSED:
            text = f"Refused, nothing sent: {where}"
        elif entry.outcome == outbound.FAILED:
            text = f"Couldn't reach {where}"
        elif entry.protection == outbound.CHECK:
            text = f"Attestation checked with {where}"
        else:
            text = f"Sent to {where}"
        self.setText(text)
        self._flash.start()
        self._update_tip()

    def _settle(self) -> None:
        self.setText(self._steady)
