# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Frames for the model: real ffmpeg on a tiny generated video."""

import shutil
import subprocess
import threading

import pytest

from mediabrowser.core import audio_levels, frames

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


@pytest.fixture(scope="module")
def clip(tmp_path_factory):
    """Six seconds of colour bars, 320 wide."""
    path = tmp_path_factory.mktemp("frames") / "clip.mkv"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y",
         "-f", "lavfi", "-i", "smptebars=size=320x180:rate=10:duration=6",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)],
        check=True,
    )
    return {"type": "file", "path": str(path), "duration": 6.0}


class TestInputs:
    def test_a_file_is_seeked_by_time(self):
        assert frames.ffmpeg_input({"type": "file", "path": "/a/b.mkv"}, 12.5) == [
            "-ss", "12.50", "-i", "/a/b.mkv",
        ]

    def test_a_bluray_title_is_seeked_by_byte_then_decoded_the_rest(self):
        video = {"type": "bluray", "path": "/disc", "playlist": 7}
        assert frames.ffmpeg_input(video, 600.0, (3110680320, 0.24)) == [
            "-skip_initial_bytes", "3110680320", "-playlist", "7", "-i", "bluray:/disc",
            "-ss", "0.24",
        ]
        assert frames.ffmpeg_input(video, 600.0, (100, 0.0)) == [
            "-skip_initial_bytes", "100", "-playlist", "7", "-i", "bluray:/disc",
        ]

    def test_a_bluray_without_positions_gives_nothing(self, monkeypatch):
        video = {"type": "bluray", "path": "/nowhere", "playlist": 1, "title_idx": 0}
        assert frames.grab(video, 5.0) is None
        assert frames.grab_many(video, [5.0, 6.0]) == {}


@needs_ffmpeg
class TestGrabbing:
    def test_a_frame_is_a_jpeg_of_the_asked_width(self, clip):
        jpeg = frames.grab(clip, 2.0, width=160)
        assert jpeg is not None and jpeg.startswith(b"\xff\xd8")
        # The JPEG SOF0 marker carries height then width, big-endian.
        sof = jpeg.find(b"\xff\xc0")
        assert sof > 0
        width = int.from_bytes(jpeg[sof + 7:sof + 9], "big")
        assert width == 160

    def test_past_the_end_there_is_nothing(self, clip):
        assert frames.grab(clip, 60.0) is None

    def test_many_at_once_with_progress(self, clip):
        seen = []
        got = frames.grab_many(clip, [1.0, 2.0, 60.0], progress_cb=seen.append, width=160)
        assert sorted(got) == [1.0, 2.0]
        assert seen[-1] == 100

    def test_it_can_be_cancelled(self, clip):
        cancel = threading.Event()
        cancel.set()
        with pytest.raises(audio_levels.Cancelled):
            frames.grab_many(clip, [1.0, 2.0], cancel=cancel)

    def test_a_repeat_is_answered_from_the_cache(self, clip, monkeypatch):
        first = frames.cached_grab_many(clip, [1.0, 60.0], width=160)
        assert sorted(first) == [1.0]
        monkeypatch.setattr(frames, "grab_many", lambda *a, **k: pytest.fail("regrabbed"))
        assert frames.cached_grab_many(clip, [1.0, 60.0], width=160) == first


class TestTheCacheCeiling:
    def test_the_oldest_frames_go_once_it_is_full(self, monkeypatch):
        frames.clear_cache()
        monkeypatch.setattr(frames, "MAX_CACHE_BYTES", 25)
        monkeypatch.setattr(frames, "grab_many",
                            lambda video, times, **k: {t: b"x" * 10 for t in times})
        video = {"type": "file", "path": "/v.mkv"}
        frames.cached_grab_many(video, [1.0, 2.0])
        assert len(frames._cache) == 2 and frames._cache_bytes == 20
        frames.cached_grab_many(video, [3.0])
        assert frames._cache_bytes <= 25
        assert [k[2] for k in frames._cache] == [2.0, 3.0], "the oldest went"
        frames.clear_cache()
        assert frames._cache_bytes == 0 and not frames._cache
