# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Background jobs: every callback on the GUI thread, and none once the
dialog that started the job is gone."""

import threading
import time

import pytest

from mediabrowser.gui import worker

PySide6 = pytest.importorskip("PySide6")


def settle(app, until, seconds=5.0):
    end = time.monotonic() + seconds
    while not until() and time.monotonic() < end:
        app.processEvents()
        time.sleep(0.005)
    app.processEvents()


def test_lambdas_are_called_on_the_gui_thread(app):
    """PySide calls a lambda connected straight to a signal on the emitting
    thread; a progress bar set from there paints off the GUI thread."""
    from PySide6.QtWidgets import QWidget

    owner = QWidget()
    gui = threading.current_thread()
    seen = []

    def work(progress_cb):
        for n in range(3):
            progress_cb(str(n))
        return "result"

    worker.run_job(
        owner, work, wants_progress=True,
        on_progress=lambda text: seen.append(("progress", threading.current_thread() is gui)),
        on_done=lambda result: seen.append((result, threading.current_thread() is gui)),
    )
    settle(app, lambda: len(seen) == 4)
    assert seen == [("progress", True)] * 3 + [("result", True)]


def test_failures_arrive_on_the_gui_thread(app):
    from PySide6.QtWidgets import QWidget

    owner = QWidget()
    gui = threading.current_thread()
    seen = []

    def work():
        raise ValueError("the disc couldn't be read")

    worker.run_job(
        owner, work, on_done=seen.append,
        on_failed=lambda message: seen.append((message, threading.current_thread() is gui)),
    )
    settle(app, lambda: seen)
    assert seen == [("the disc couldn't be read", True)]


def test_nothing_is_delivered_once_the_owner_is_deleted(app):
    """A dialog closed mid-job: its widgets are gone, so its callbacks
    would only fail on them. The job runs on to the end regardless."""
    from PySide6.QtWidgets import QWidget

    owner = QWidget()
    release, ran, seen = threading.Event(), threading.Event(), []

    def work():
        release.wait(5)
        ran.set()
        return 1

    worker.run_job(owner, work, on_done=seen.append)
    import shiboken6

    shiboken6.delete(owner)
    release.set()
    settle(app, lambda: ran.is_set() and not worker._live)
    assert ran.is_set() and seen == []


def test_a_job_outlives_its_handle(app):
    """Nothing need keep the returned pair for the result to arrive."""
    from PySide6.QtWidgets import QWidget

    owner = QWidget()
    seen = []
    worker.run_job(owner, lambda: time.sleep(0.05) or "late", on_done=seen.append)
    import gc

    gc.collect()
    settle(app, lambda: seen)
    assert seen == ["late"] and not worker._live
