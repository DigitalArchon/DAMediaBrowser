# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later

import shutil
import subprocess
import threading

import pytest

from mediabrowser.core import audio_levels, chaptergen

SAMPLE = """frame:0    pts:0       pts_time:0
lavfi.astats.Overall.RMS_level=-20.500000
frame:1    pts:4000    pts_time:0.5
lavfi.astats.Overall.RMS_level=-inf
frame:2    pts:8000    pts_time:1
lavfi.astats.Overall.RMS_level=-12.25
"""


class TestParsing:
    def test_levels_are_read_in_order(self):
        assert audio_levels.parse_metadata(SAMPLE) == [-20.5, audio_levels.FLOOR_DB, -12.25]

    def test_other_lines_are_ignored(self):
        assert audio_levels.parse_metadata("frame:0 pts:0\nsomething=else\n") == []


needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


@pytest.fixture(scope="module")
def two_songs(tmp_path_factory):
    """Two 70s 'songs' (a bass tone) either side of 12s of near-silence."""
    path = tmp_path_factory.mktemp("audio") / "two_songs.wav"
    tone = "sine=frequency=80:sample_rate=8000:duration=70"
    quiet = "anoisesrc=color=white:amplitude=0.001:sample_rate=8000:duration=12"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y",
         "-f", "lavfi", "-i", tone, "-f", "lavfi", "-i", quiet, "-f", "lavfi", "-i", tone,
         "-filter_complex", "[0][1][2]concat=n=3:v=0:a=1", str(path)],
        check=True,
    )
    return path


@needs_ffmpeg
class TestReadingAFile:
    def test_both_curves_cover_the_whole_file(self, two_songs):
        levels = audio_levels.read_levels(two_songs)
        assert len(levels.full) == len(levels.bass)
        assert abs(levels.duration - 152) <= 1.0

    def test_the_quiet_stretch_is_quiet(self, two_songs):
        levels = audio_levels.read_levels(two_songs)
        song = levels.bass[int(30 / levels.step)]
        gap = levels.bass[int(76 / levels.step)]
        assert gap < song - 30

    def test_the_gap_is_found_where_it_is(self, two_songs):
        levels = audio_levels.read_levels(two_songs)
        starts = chaptergen.estimate_starts(levels, levels.duration)
        assert len(starts) == 2
        assert 70 <= starts[1] <= 83

    def test_progress_is_reported(self, two_songs):
        seen = []
        audio_levels.read_levels(two_songs, duration=152, progress_cb=seen.append)
        assert seen and seen[-1] >= 90

    def test_it_can_be_cancelled(self, two_songs):
        cancel = threading.Event()
        cancel.set()
        with pytest.raises(audio_levels.Cancelled):
            audio_levels.read_levels(two_songs, cancel=cancel)

    def test_a_file_that_is_not_audio_is_an_error(self, tmp_path):
        bogus = tmp_path / "not_audio.mkv"
        bogus.write_bytes(b"nothing to see")
        with pytest.raises(audio_levels.LevelsError):
            audio_levels.read_levels(bogus)
