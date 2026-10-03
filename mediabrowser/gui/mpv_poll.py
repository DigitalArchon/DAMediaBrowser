# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Asking mpv how playback is going, off the GUI thread.

Each question goes over mpv's IPC socket and waits for the answer. A live
mpv answers in a millisecond; one stuck on the display doesn't answer at
all, and a poll waiting on it from the GUI thread - up to a second a
round, every 400ms - froze the window. Under XWayland that can hold
itself in place: mpv draws into the app's window, so after a resize (out
of fullscreen, say) the compositor waits for the app to repaint before
showing mpv's frame, mpv waits for its frame to be shown, and the app
waited on mpv. Asked from here the window never waits on mpv: a stuck one
can freeze only the picture.

Each answer is whatever `ask` returns, and callers put in it what lets
them recognise one asked before playback changed under it - a file loaded,
a seek - which may arrive after the change.

A plain daemon thread, as in worker.py, and the answers reach the GUI only
through a slot of this object, which lives on the GUI thread.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

import shiboken6
from PySide6.QtCore import QObject, Signal, Slot


class MpvPoll(QObject):
    """Calls `ask` every `interval_ms` on a thread of its own, and hands
    each answer to `on_answer` on the GUI thread. A round that is slow to
    come back delays the next; rounds never overlap."""

    _answered = Signal(object)

    def __init__(self, ask: Callable[[], Any], on_answer: Callable[[Any], None],
                 interval_ms: int, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._ask = ask
        self._on_answer = on_answer
        self._interval = interval_ms / 1000
        # Set to stop the running round; each start has its own, so an
        # answer from before a stop is never delivered after it.
        self._stop: threading.Event | None = None
        self._answered.connect(self._deliver)

    def start(self) -> None:
        if self._stop is not None:
            return
        stop = threading.Event()
        self._stop = stop
        threading.Thread(target=self._run, args=(stop,), daemon=True,
                         name="mediabrowser-mpv-poll").start()

    def stop(self) -> None:
        if self._stop is not None:
            self._stop.set()
            self._stop = None

    def _run(self, stop: threading.Event) -> None:
        """On the poll's thread."""
        while not stop.wait(self._interval):
            try:
                answer = self._ask()
            except Exception:  # a round that failed is a round missed
                continue
            if stop.is_set():
                return
            try:
                self._answered.emit((stop, answer))
            except RuntimeError:  # this object has gone with its window
                return

    @Slot(object)
    def _deliver(self, item) -> None:
        stop, answer = item
        if not stop.is_set() and shiboken6.isValid(self):
            self._on_answer(answer)
