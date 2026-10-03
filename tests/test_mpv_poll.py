# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Asking mpv off the GUI thread: answers arrive on the GUI thread, a stuck
mpv never holds the window up, and nothing asked before a stop arrives
after it."""

import threading
import time

import pytest

PySide6 = pytest.importorskip("PySide6")

from PySide6.QtCore import QElapsedTimer, QEventLoop, QTimer  # noqa: E402

from mediabrowser.gui.mpv_poll import MpvPoll  # noqa: E402


def run(ms):
    """The event loop for `ms`, as app.exec() runs it - letting other
    threads run, which QTest.qWait doesn't."""
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def wait_for(condition, ms=2000):
    timer = QElapsedTimer()
    timer.start()
    while not condition() and timer.elapsed() < ms:
        run(10)
    return condition()


def timer_ticks(ms):
    """How many times a 20ms timer fires in `ms` of the event loop."""
    ticks, timer = [], QTimer()
    timer.setInterval(20)
    timer.timeout.connect(lambda: ticks.append(1))
    timer.start()
    run(ms)
    timer.stop()
    return len(ticks)


def test_answers_arrive_on_the_gui_thread(app):
    gui = threading.current_thread()
    asked_on, answered_on = [], []

    def ask():
        asked_on.append(threading.current_thread())
        return len(asked_on)

    poll = MpvPoll(ask, lambda answer: answered_on.append(threading.current_thread()), 10)
    poll.start()
    assert wait_for(lambda: len(answered_on) >= 3)
    poll.stop()
    assert gui not in asked_on, "mpv is asked off the GUI thread"
    assert set(answered_on) == {gui}


def test_a_stuck_mpv_never_holds_the_window_up(app):
    asked = []

    def ask():
        asked.append(1)
        time.sleep(1.0)  # an mpv that doesn't answer: the socket times out
        return None

    poll = MpvPoll(ask, lambda answer: None, 10)
    poll.start()
    ticks = timer_ticks(1200)
    poll.stop()
    assert asked, "mpv was asked"
    assert ticks >= 45, f"the event loop stalled: {ticks} of ~60"


def test_nothing_asked_before_a_stop_arrives_after_it(app):
    release, answered = threading.Event(), []

    def ask():
        release.wait(2)
        return "stale"

    poll = MpvPoll(ask, answered.append, 10)
    poll.start()
    run(50)
    poll.stop()
    release.set()
    run(100)
    assert answered == []


def test_a_round_that_fails_is_only_missed(app):
    rounds, answered = [], []

    def ask():
        rounds.append(1)
        if len(rounds) == 1:
            raise OSError("mpv went away mid-question")
        return "fine"

    poll = MpvPoll(ask, answered.append, 10)
    poll.start()
    assert wait_for(lambda: answered)
    poll.stop()
    assert answered[0] == "fine"
