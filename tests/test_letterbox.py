# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Black bars baked into the picture: found from what ffmpeg's cropdetect
says, cropped only where every sample was black, and remembered."""

import subprocess

from mediabrowser.core import letterbox

HD = (1920, 1080)
# Legend S: a 2.4:1 picture, rows 140 to 940, with narration captions in
# the bottom bar now and then, down to row 1022.
PICTURE = (HD, (0, 1919, 140, 940))
CAPTIONED = (HD, (0, 1919, 140, 1022))


class TestCrop:
    def test_a_letterbox_comes_off_top_and_bottom(self):
        width, height, x, y = letterbox.crop_from([PICTURE] * 6)
        assert (width, x) == (1920, 0)
        assert 136 <= y <= 140 and 1080 - y - height >= 130

    def test_captions_in_a_bar_keep_as_much_of_it_as_they_use(self):
        width, height, x, y = letterbox.crop_from([PICTURE] * 5 + [CAPTIONED])
        assert y + height > 1022, "the captions are still in the picture"
        assert y >= 136, "the top bar, which never carried anything, still comes off"

    def test_a_picture_that_fills_the_frame_isnt_cropped(self):
        assert letterbox.crop_from([(HD, (0, 1919, 0, 1079))] * 6) is None

    def test_slivers_arent_worth_cropping(self):
        assert letterbox.crop_from([(HD, (2, 1917, 6, 1073))] * 6) is None

    def test_too_few_pictures_say_nothing(self):
        assert letterbox.crop_from([PICTURE, PICTURE, None, None, None]) is None

    def test_a_picture_that_changes_shape_says_nothing(self):
        assert letterbox.crop_from([PICTURE] * 5 + [((1440, 1080), (0, 1439, 0, 1079))]) is None


class TestReadingCropdetect:
    STDERR = (
        "  Stream #0:0[0x1011]: Video: h264 (High), yuv420p(top first), "
        "1920x1080 [SAR 1:1 DAR 16:9], 29.97 fps\n"
        "[Parsed_cropdetect_0 @ 0x1] x1:0 x2:1919 y1:300 y2:800 w:1920 h:500 x:0 y:300 "
        "crop=1920:500:0:300\n"
        "[Parsed_cropdetect_0 @ 0x1] x1:0 x2:1919 y1:140 y2:940 w:1920 h:800 x:0 y:140 "
        "crop=1920:800:0:140\n"
    )

    def _run(self, monkeypatch, stderr):
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
            a, 0, "", stderr))
        return letterbox.extent_at({"type": "file", "path": "/v/a.mkv"}, 60.0)

    def test_the_last_line_is_everything_seen(self, monkeypatch):
        assert self._run(monkeypatch, self.STDERR) == PICTURE

    def test_all_black_is_no_picture(self, monkeypatch):
        black = self.STDERR.split("\n")[0] + (
            "\n[Parsed_cropdetect_0 @ 0x1] x1:1919 x2:0 y1:1079 y2:0 w:-1904 h:-1072\n")
        assert self._run(monkeypatch, black) is None
        assert self._run(monkeypatch, self.STDERR.split("\n")[0]) is None


class TestRemembered:
    def test_measured_once_known_after(self):
        assert not letterbox.measured("a")
        letterbox.remember("a", (1920, 800, 0, 140))
        letterbox.remember("b", None)
        assert letterbox.crop_for("a") == (1920, 800, 0, 140)
        assert letterbox.measured("b") and letterbox.crop_for("b") is None

    def test_across_runs(self):
        letterbox.remember("a", (1920, 800, 0, 140))
        letterbox._tables.clear()
        assert letterbox.crop_for("a") == (1920, 800, 0, 140)
