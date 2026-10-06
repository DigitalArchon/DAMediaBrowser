# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Chapters saved as files for mkvmerge, ffmpeg and CUE players - read back
by the real tools where they're installed, and never written into a
library."""

import json
import shutil
import subprocess
import xml.etree.ElementTree as ET

import pytest

from mediabrowser.core import chapter_export


def video(path="/media/Concerts/Glass Harbor/Copperfield Hall.mkv", kind="file"):
    return {
        "type": kind,
        "path": path,
        "display_name": "Glass Harbor - Live at Copperfield Hall 2026",
        "duration": 754.5,
        "chapters": [
            {"start": 0.0, "end": 61.25, "title": "Paper Lanterns", "source": "manual"},
            {"start": 61.25, "end": 400.0, "title": None, "source": "auto-numbered"},
            {"start": 400.0, "end": 754.5, "title": 'Ember; "Tidewater" = #1 \\ medley',
             "source": "ai"},
        ],
    }


def test_mkvmerge_xml_has_every_chapter_its_times_and_a_name():
    text = chapter_export.mkvmerge_xml(video(), "abc")
    assert text.startswith('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE Chapters')
    root = ET.fromstring(text.split("\n", 2)[2])
    atoms = root.findall("./EditionEntry/ChapterAtom")
    assert [a.findtext("ChapterTimeStart") for a in atoms] == [
        "00:00:00.000000000", "00:01:01.250000000", "00:06:40.000000000"]
    assert atoms[2].findtext("ChapterTimeEnd") == "00:12:34.500000000"
    assert [a.findtext("ChapterDisplay/ChapterString") for a in atoms] == [
        "Paper Lanterns", "Chapter 2", 'Ember; "Tidewater" = #1 \\ medley']
    uids = [int(a.findtext("ChapterUID")) for a in atoms]
    assert len(set(uids)) == 3 and all(0 < uid < 2**64 for uid in uids)
    # The same each time, so exporting again replaces rather than adds.
    assert chapter_export.mkvmerge_xml(video(), "abc") == text


def test_ffmetadata_escapes_what_ffmpeg_would_read_as_syntax():
    text = chapter_export.ffmetadata(video())
    assert text.startswith(";FFMETADATA1\n")
    assert "START=61250\nEND=400000\ntitle=Chapter 2" in text
    assert 'title=Ember\\; "Tidewater" \\= \\#1 \\\\ medley' in text


def test_a_cue_sheet_counts_in_frames_and_names_the_file():
    text = chapter_export.cue_sheet(video())
    assert 'FILE "Copperfield Hall.mkv" WAVE' in text
    assert "  TRACK 02 AUDIO\n    TITLE \"Chapter 2\"\n    INDEX 01 01:01:19" in text
    assert "INDEX 01 06:40:00" in text
    # No way to escape a quote in a CUE sheet: it's swapped for another.
    assert "TITLE \"Ember; 'Tidewater' = #1 \\ medley\"" in text


def test_a_disc_titles_cue_sheet_names_what_a_rip_would_be_called():
    text = chapter_export.cue_sheet(video("/media/Concerts/DISC", kind="bluray"))
    assert text.startswith("REM ")
    assert 'FILE "Glass Harbor - Live at Copperfield Hall 2026.mkv" WAVE' in text


def test_never_into_a_library_nor_over_a_file_of_another_kind(tmp_path):
    library = tmp_path / "Concerts"
    (library / "Glass Harbor").mkdir(parents=True)
    clip = library / "Glass Harbor" / "Copperfield Hall.mkv"
    clip.write_bytes(b"video")
    roots = [str(library)]
    for path in (library / "chapters.xml", library / "Glass Harbor" / "x.cue"):
        with pytest.raises(chapter_export.ExportError, match="inside the library"):
            chapter_export.export(video(str(clip)), chapter_export.format_for(path), path, roots)
    notes = tmp_path / "notes.txt"
    notes.write_text("my notes", encoding="utf-8")
    with pytest.raises(chapter_export.ExportError, match="isn't a FFmpeg metadata"):
        chapter_export.export(video(), chapter_export.FFMETADATA, notes, roots)
    with pytest.raises(chapter_export.ExportError, match="saved as .cue"):
        chapter_export.export(video(), chapter_export.CUE, tmp_path / "x.xml", roots)
    with pytest.raises(chapter_export.ExportError, match="a video's name"):
        chapter_export.export(video(), chapter_export.CUE, tmp_path / "x.mkv", roots)
    assert notes.read_text(encoding="utf-8") == "my notes"
    assert clip.read_bytes() == b"video"
    assert sorted(p.name for p in library.rglob("*")) == ["Copperfield Hall.mkv", "Glass Harbor"]


def test_saved_and_saved_again_over_its_own(tmp_path):
    for fmt in chapter_export.FORMATS:
        path = tmp_path / f"chapters{chapter_export.SUFFIXES[fmt]}"
        chapter_export.export(video(), fmt, path)
        chapter_export.export(video(), fmt, path)
        assert path.read_text(encoding="utf-8") == chapter_export.render(video(), fmt)
    assert not list(tmp_path.glob(".*.part"))


def test_safe_file_names():
    assert chapter_export.safe_file_name('AC/DC: "Live"?') == "AC_DC_ _Live__"
    assert chapter_export.safe_file_name(" ... ") == "untitled"
    assert chapter_export.safe_file_name("Song 7.") == "Song 7"
    assert len(chapter_export.safe_file_name("é" * 300).encode()) <= 180


needs_ffmpeg = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="needs ffmpeg"
)


def _chapters_of(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_chapters", "-of", "json", str(path)],
                         check=True, capture_output=True, text=True).stdout
    return [(round(float(c["start_time"]), 2), c["tags"].get("title"))
            for c in json.loads(out)["chapters"]]


@needs_ffmpeg
def test_ffmpeg_puts_the_exported_chapters_into_a_copy(tmp_path):
    source = tmp_path / "in.mkv"
    subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-f", "lavfi",
                    "-i", "sine=duration=754.5:sample_rate=8000", "-c:a", "flac", str(source)],
                   check=True)
    meta = tmp_path / "chapters.txt"
    chapter_export.export(video(), chapter_export.FFMETADATA, meta)
    out = tmp_path / "out.mkv"
    subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-i", str(source), "-i", str(meta),
                    "-map", "0", "-map_chapters", "1", "-c", "copy", str(out)], check=True)
    assert _chapters_of(out) == [(0.0, "Paper Lanterns"), (61.25, "Chapter 2"),
                                 (400.0, 'Ember; "Tidewater" = #1 \\ medley')]


@pytest.mark.skipif(not shutil.which("mkvmerge"), reason="needs mkvmerge")
@needs_ffmpeg
def test_mkvmerge_reads_the_exported_xml(tmp_path):
    source = tmp_path / "in.mka"
    subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-f", "lavfi",
                    "-i", "sine=duration=754.5:sample_rate=8000", "-c:a", "flac", str(source)],
                   check=True)
    xml = tmp_path / "chapters.xml"
    chapter_export.export(video(), chapter_export.MKVMERGE, xml)
    out = tmp_path / "out.mka"
    subprocess.run(["mkvmerge", "-q", "-o", str(out), "--chapters", str(xml), str(source)],
                   check=True)
    assert [title for _start, title in _chapters_of(out)] == [
        "Paper Lanterns", "Chapter 2", 'Ember; "Tidewater" = #1 \\ medley']
