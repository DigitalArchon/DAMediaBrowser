# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""What mpv is asked to do: the command line it is started with and the
commands it is sent. mpv itself isn't run."""

import pytest

from mediabrowser.core import player as player_module


@pytest.fixture
def launched(monkeypatch):
    runs = []

    class Proc:
        def __init__(self, args, **kwargs):
            runs.append(args)

        def poll(self):
            return None

        def terminate(self):
            pass

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(player_module.subprocess, "Popen", Proc)
    return runs


class TestEditing:
    def test_a_file_opens_embedded_paused_and_held_open(self, launched):
        player_module.Player().open_for_editing(
            {"type": "file", "path": "/v/show.mkv"}, start=12.5, window_id=4242
        )
        args = launched[0]
        assert args[0] == "mpv" and "/v/show.mkv" in args
        for flag in ("--wid=4242", "--keep-open=always", "--hr-seek=yes", "--pause",
                     "--hr-seek-demuxer-offset=2",
                     "--input-vo-keyboard=no", "--start=12.500"):
            assert flag in args
        assert "--no-video" not in args and not any(a.startswith("--end") for a in args)

    def test_a_blu_ray_title_opens_by_its_index(self, launched):
        player_module.Player().open_for_editing(
            {"type": "bluray", "path": "/disc", "title_idx": 3}, paused=False
        )
        args = launched[0]
        assert "--bluray-device=/disc" in args and "bd://3" in args
        assert "--pause" not in args and not any(a.startswith("--start") for a in args)

    def test_without_a_window_it_has_its_own(self, launched):
        player_module.Player().open_for_editing({"type": "file", "path": "/v.mkv"})
        assert not any(a.startswith("--wid") for a in launched[0])

    def test_embedded_it_draws_through_x11_on_a_wayland_desktop(self, monkeypatch):
        envs = []

        class Proc:
            def __init__(self, args, env=None, **kwargs):
                envs.append(env)

            def poll(self):
                return None

        monkeypatch.setattr(player_module.subprocess, "Popen", Proc)
        monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
        player_module.Player().open_for_editing({"type": "file", "path": "/v.mkv"}, window_id=7)
        player_module.Player().open_for_editing({"type": "file", "path": "/v.mkv"})
        assert "WAYLAND_DISPLAY" not in envs[0]
        assert envs[1] is None, "its own window: whatever suits the desktop"

    def test_exact_seeks_and_frame_steps(self, launched, monkeypatch):
        sent = []
        p = player_module.Player()
        p.open_for_editing({"type": "file", "path": "/v.mkv"})
        monkeypatch.setattr(p, "_send", lambda command, retries=3: sent.append(command) or True)
        p.seek_exact(61.25)
        p.frame_step()
        p.frame_step(forward=False)
        assert sent == [["seek", 61.25, "absolute+exact"], ["frame-step"], ["frame-back-step"]]


class TestXWayland:
    def test_a_wayland_desktop_runs_under_xwayland_unless_told_not_to(self):
        from mediabrowser.gui.app import prefer_x11

        env = {"WAYLAND_DISPLAY": "wayland-0", "DISPLAY": ":0"}
        prefer_x11(env)
        assert env["QT_QPA_PLATFORM"] == "xcb"
        chosen = {"WAYLAND_DISPLAY": "wayland-0", "DISPLAY": ":0", "QT_QPA_PLATFORM": "wayland"}
        prefer_x11(chosen)
        assert chosen["QT_QPA_PLATFORM"] == "wayland"
        no_x = {"WAYLAND_DISPLAY": "wayland-0"}
        prefer_x11(no_x)
        assert "QT_QPA_PLATFORM" not in no_x
        fallback = {"WAYLAND_DISPLAY": "wayland-0", "DISPLAY": ":0",
                    "QT_QPA_PLATFORM": "wayland;xcb"}
        prefer_x11(fallback)
        assert fallback["QT_QPA_PLATFORM"] == "xcb", "xcb was allowed"

    def test_embedded_mpv_is_kept_off_wayland(self):
        """mpv's Wayland output ignores --wid and opens its own window."""
        from mediabrowser.core.player import x11_environment

        env = x11_environment({"WAYLAND_DISPLAY": "wayland-0", "DISPLAY": ":0", "HOME": "/h"})
        assert env == {"DISPLAY": ":0", "HOME": "/h"}


class TestCarryingOn:
    """Moving playback to another mpv: what the new one is told."""

    def _calls(self, monkeypatch, p):
        calls = []
        monkeypatch.setattr(p, "_call", lambda command, attempts=20: calls.append(command))
        return calls

    def test_the_moment_and_tracks_go_with_the_file(self, launched, monkeypatch):
        from mediabrowser.core.playback import Segment

        p = player_module.Player()
        p.start_session(None)
        calls = self._calls(monkeypatch, p)
        segment = Segment("v", "file", "/v.mkv", None, False, 100.0, None, (0,), (100.0,))
        p.resume(segment, {"time-pos": 142.5, "pause": True, "volume": 60.0,
                           "sub-visibility": False, "aid": 2, "sid": False})
        assert ["set_property", "volume", 60.0] in calls
        assert ["set_property", "sub-visibility", False] in calls
        assert ["set_property", "pause", True] in calls
        load = calls[-1]
        assert load["name"] == "loadfile" and load["url"] == "/v.mkv"
        options = dict(o.split("=") for o in load["options"].split(","))
        assert options["start"] == "142.500" and options["aid"] == "2"
        assert options["sid"] == "no", "subtitles off stays off"
        assert ["set_property", "aid", 2] not in calls, "a track only means anything in its file"

    def test_screenshots_go_to_the_folder_given(self, launched):
        p = player_module.Player()
        p.screenshot_dir = "/pics/DA"
        p.start_session(None)
        assert "--screenshot-directory=/pics/DA" in launched[0]

    def test_a_dollar_in_a_screenshots_name_is_kept(self, launched, monkeypatch, tmp_path):
        p = player_module.Player()
        p.start_session(7)
        calls = self._calls(monkeypatch, p)
        p.screenshot(tmp_path / "Ca$h 1-00.png")
        assert calls == [["screenshot-to-file", str(tmp_path / "Ca$h 1-00.png"), "subtitles"]]
