# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Background jobs on a worker thread.

Scanning a library runs ffprobe once per file and libbluray once per disc;
MusicBrainz lookups wait on the network and on a deliberate 2s throttle.
Either would freeze the window if run on the GUI thread.

All of it reaches the GUI only as signals; no widget is ever touched off the
GUI thread. Two things make that hold, and both have crashed the app when
missing:

- The callbacks are called by the Job's own slots, never connected to its
  signals directly. PySide calls a plain function or lambda connected to a
  signal on whichever thread emits it - the worker's - so a progress lambda
  setting a progress bar would paint from the worker thread. A slot of a
  QObject on the GUI thread is queued across to it.
- The thread is a plain daemon thread, not a QThread. A QThread destroyed
  while it runs - its dialog closed, or the app quit, in the middle of a
  menu read or a minute-long AI request - aborts the whole process.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

import shiboken6
from PySide6.QtCore import QObject, Signal, Slot

# Every job still running or with results still to deliver, so none is
# freed while a signal is on its way to it.
_live: set[Job] = set()


class Job(QObject):
    """Runs one callable off the GUI thread and reports what happened.

    Made on the GUI thread, so it lives there: its signals, emitted from the
    worker, are queued across, and its slots hand each one to its callback
    on the GUI thread.
    """

    progress = Signal(str)
    done = Signal(object)
    failed = Signal(str)
    finished = Signal()

    def __init__(self, work: Callable[..., Any], *, wants_progress: bool = False,
                 owner: QObject | None = None, on_done=None, on_failed=None,
                 on_progress=None) -> None:
        super().__init__()
        self._work = work
        self._wants_progress = wants_progress
        self._owner = owner
        self._on_done, self._on_failed, self._on_progress = on_done, on_failed, on_progress
        self.progress.connect(self._deliver_progress)
        self.done.connect(self._deliver_done)
        self.failed.connect(self._deliver_failed)
        self.finished.connect(self._deliver_finished)

    def run(self) -> None:
        """On the worker thread."""
        try:
            if self._wants_progress:
                result = self._work(progress_cb=self.progress.emit)
            else:
                result = self._work()
        except Exception as exc:  # surfaced in the UI rather than killing the thread
            self.failed.emit(str(exc) or repr(exc))
        else:
            self.done.emit(result)
        finally:
            self.finished.emit()

    def _wanted(self) -> bool:
        """Whether whoever started this is still there to hear about it:
        a dialog closed mid-job has had its widgets deleted."""
        return self._owner is None or shiboken6.isValid(self._owner)

    @Slot(str)
    def _deliver_progress(self, text: str) -> None:
        if self._on_progress is not None and self._wanted():
            self._on_progress(text)

    @Slot(object)
    def _deliver_done(self, result) -> None:
        if self._wanted():
            self._on_done(result)

    @Slot(str)
    def _deliver_failed(self, message: str) -> None:
        if self._on_failed is not None and self._wanted():
            self._on_failed(message)

    @Slot()
    def _deliver_finished(self) -> None:
        _live.discard(self)


def run_job(
    parent: QObject,
    work: Callable[..., Any],
    *,
    on_done: Callable[[Any], None],
    on_failed: Callable[[str], None] | None = None,
    on_progress: Callable[[str], None] | None = None,
    wants_progress: bool = False,
) -> tuple[threading.Thread, Job]:
    """Start `work` on its own thread; each callback is called on the GUI
    thread, and not at all once `parent` has been deleted.

    Callers keep the returned pair to know a job is under way; the job
    keeps itself alive until it has delivered everything.
    """
    job = Job(work, wants_progress=wants_progress, owner=parent, on_done=on_done,
              on_failed=on_failed, on_progress=on_progress)
    _live.add(job)
    thread = threading.Thread(target=job.run, daemon=True, name="mediabrowser-job")
    thread.start()
    return thread, job
