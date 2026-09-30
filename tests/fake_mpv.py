# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""A stand-in for core.player.Player's session side, for tests: records
what it's told, and answers from what a test sets. Nothing is launched."""


class FakeMpv:
    """The session side of core.player.Player, answering from what a test
    sets: which entry mpv is on, and whether it has run out."""

    def __init__(self):
        self.calls = []
        self.active = False
        self.next_id = 0
        self.state = {"position": 0.0, "duration": 300.0, "paused": False,
                      "playing_id": None, "idle": False}

    def is_active(self):
        return self.active

    def start_session(self, window_id=None):
        self.calls.append(("start", window_id))
        self.active = True

    def load(self, segment, append=False):
        self.next_id += 1
        self.calls.append(("load", segment.video_id, segment.entries, append))
        if not append:
            self.state["playing_id"] = self.next_id
        return self.next_id

    def playlist_clear(self):
        self.calls.append(("clear",))

    def playlist_remove(self, index):
        self.calls.append(("remove", index))

    def set_end(self, end):
        self.calls.append(("end", end))

    def session_state(self):
        return dict(self.state)

    def stop(self):
        self.calls.append(("stop",))
        self.active = False

    # What the window's transport sends besides.
    def toggle_pause(self):
        self.calls.append(("toggle",))
        self.state["paused"] = not self.state["paused"]

    def set_paused(self, paused):
        self.state["paused"] = paused

    def seek(self, seconds):
        self.calls.append(("seek", seconds))
        self.state["position"] = seconds
