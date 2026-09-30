# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later

import shutil
import subprocess
import threading

import pytest

from mediabrowser.core import audio_levels, stage_light

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


@pytest.fixture(scope="module")
def dark_then_bright(tmp_path_factory):
    """Ten seconds of black, then ten of white, a keyframe every second."""
    path = tmp_path_factory.mktemp("light") / "stage.mkv"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y",
         "-f", "lavfi", "-i", "color=black:size=160x90:rate=10:duration=10",
         "-f", "lavfi", "-i", "color=white:size=160x90:rate=10:duration=10",
         "-filter_complex", "[0][1]concat=n=2:v=1:a=0",
         "-g", "10", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)],
        check=True,
    )
    return path


@needs_ffmpeg
class TestSampling:
    def test_dark_and_bright_moments_read_as_such(self, dark_then_bright):
        light = stage_light.sample(dark_then_bright, [2.0, 15.0])
        assert light[2.0] < 40 < 200 < light[15.0]

    def test_progress_is_reported(self, dark_then_bright):
        seen = []
        stage_light.sample(dark_then_bright, [1.0, 5.0, 12.0], progress_cb=seen.append)
        assert seen[-1] == 100

    def test_a_file_without_a_picture_gives_nothing(self, tmp_path):
        sound = tmp_path / "sound.wav"
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
             "-i", "sine=frequency=80:sample_rate=8000:duration=5", str(sound)],
            check=True,
        )
        assert stage_light.sample(sound, [1.0, 2.0]) == {}

    def test_it_can_be_cancelled(self, dark_then_bright):
        cancel = threading.Event()
        cancel.set()
        with pytest.raises(audio_levels.Cancelled):
            stage_light.sample(dark_then_bright, [1.0, 2.0, 3.0], cancel=cancel)

    def test_a_repeat_request_is_answered_from_the_cache(self, dark_then_bright, monkeypatch):
        first = stage_light.cached_sample(dark_then_bright, [1, 2], [2.0])
        monkeypatch.setattr(stage_light, "sample", lambda *a, **k: pytest.fail("resampled"))
        assert stage_light.cached_sample(dark_then_bright, [1, 2], [2.0]) == first
        assert stage_light.peek_cached(dark_then_bright, [1, 2], [2.0]) == first
