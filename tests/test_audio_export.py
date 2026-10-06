# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Songs exported as FLAC or Opus files for a phone: cut where their
chapters are, tagged, with the cover, stereo - and never into a library."""

import base64
import json
import shutil
import struct
import subprocess
import threading

import pytest

from mediabrowser.core import audio_export

needs_ffmpeg = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="needs ffmpeg"
)


def video(path="/media/Concerts/Glass Harbor/Copperfield Hall.mkv"):
    return {
        "type": "file", "path": str(path),
        "display_name": "Glass Harbor: Live at Copperfield Hall", "duration": 12.0,
        "chapters": [
            {"start": 0.0, "end": 4.5, "title": "Paper Lanterns", "source": "manual"},
            {"start": 4.5, "end": 9.0, "title": None, "source": "auto-numbered"},
            {"start": 9.0, "end": 12.0, "title": "Ember / Tidewater (medley)", "source": "ai"},
        ],
    }


def test_each_song_is_named_by_its_place_and_title_in_a_folder_of_the_video(tmp_path):
    songs = audio_export.plan(video(), [2, 0, 2], tmp_path, audio_export.OPUS)
    folder = tmp_path / "Glass Harbor_ Live at Copperfield Hall"
    assert [s.path for s in songs] == [folder / "01 Paper Lanterns.opus",
                                       folder / "03 Ember _ Tidewater (medley).opus"]
    assert [(s.start, s.end, s.title) for s in songs] == [
        (0.0, 4.5, "Paper Lanterns"), (9.0, 12.0, "Ember / Tidewater (medley)")]
    loose = audio_export.plan(video(), [1], tmp_path, audio_export.FLAC, in_own_folder=False)
    assert loose[0].path == tmp_path / "02 Chapter 2.flac"


def test_never_into_a_library(tmp_path):
    library = tmp_path / "Concerts"
    (library / "Glass Harbor").mkdir(parents=True)
    assert "inside the library" in audio_export.refused_folder(
        library / "Glass Harbor", [str(library)])
    assert audio_export.refused_folder(library, [str(library)])
    assert audio_export.refused_folder(tmp_path / "Phone", [str(library)]) is None


def test_the_stereo_track_is_taken_over_a_surround_one():
    assert audio_export.choose_stream(
        [{"channels": 6, "sample_fmt": "fltp"}, {"channels": 2, "sample_fmt": "s32"}]
    ) == (1, {"channels": 2, "sample_fmt": "s32"})
    assert audio_export.choose_stream([{"channels": 6, "sample_fmt": ""}])[0] == 0
    assert audio_export.choose_stream([]) == (0, {})


def test_an_opus_cover_is_a_flac_picture_block():
    block = base64.b64decode(audio_export.picture_block(b"\xff\xd8jpeg"))
    kind, mime_length = struct.unpack(">II", block[:8])
    assert kind == 3 and block[8:8 + mime_length] == b"image/jpeg"
    assert block.endswith(struct.pack(">I", 6) + b"\xff\xd8jpeg")


def test_surround_is_mixed_down_and_lossy_audio_kept_at_16_bits(tmp_path):
    song = audio_export.plan(video(), [0], tmp_path, audio_export.FLAC)[0]
    src = audio_export.Source(["-ss", "0.00", "-i", "in.mkv"])
    args = audio_export.command(src, song, audio_export.FLAC, "tags.txt", tmp_path / "p",
                                stream=(1, {"channels": 6, "sample_fmt": "fltp"}))
    assert args[args.index("-ac") + 1] == "2" and "s16" in args
    assert args[args.index("-map") + 1] == "1:a:1"
    # The video is the last input, so a Blu-ray's trailing -ss is the output's.
    assert args.index("in.mkv") > args.index("tags.txt")
    assert args[-2:] == ["-n", str(tmp_path / "p")]


def _make_video(path, seconds=12):
    subprocess.run(
        ["ffmpeg", "-v", "error", "-nostdin",
         "-f", "lavfi", "-i", f"testsrc=size=96x54:rate=10:duration={seconds}",
         "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
         "-c:v", "mpeg4", "-c:a", "aac", "-ac", "6", str(path)],
        check=True, capture_output=True,
    )


def _probe(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "format=duration:format_tags:stream=codec_name,channels:stream_tags", "-of", "json",
         str(path)], check=True, capture_output=True, text=True).stdout
    return json.loads(out)


@pytest.fixture
def cover(tmp_path):
    path = tmp_path / "cover.jpg"
    subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-f", "lavfi",
                    "-i", "color=c=red:s=64x64:d=1", "-frames:v", "1", str(path)], check=True)
    return path


@needs_ffmpeg
@pytest.mark.parametrize("fmt", audio_export.FORMATS)
def test_songs_come_out_cut_tagged_stereo_and_with_the_cover(tmp_path, cover, fmt):
    if not audio_export.has_encoder(fmt):
        pytest.skip(f"this ffmpeg can't write {fmt}")
    source = tmp_path / "in.mkv"
    _make_video(source)
    v = video(source)
    songs = audio_export.plan(v, [0, 2], tmp_path / "Phone", fmt)
    seen = []
    written = audio_export.export_songs(
        v, songs, fmt, audio_export.Tags("Copperfield Hall", "Glass Harbor", 3), cover=cover,
        progress_cb=lambda done, total, fraction: seen.append((done, total, fraction)))
    assert written == [s.path for s in songs]
    assert seen[-1] == (2, 2, 1.0)
    for song in songs:
        info = _probe(song.path)
        codecs = [(s["codec_name"], s.get("channels")) for s in info["streams"]]
        assert (fmt, 2) in codecs and ("mjpeg", None) in codecs
        assert float(info["format"]["duration"]) == pytest.approx(song.duration, abs=0.05)
        tags = {k.lower(): v for s in info["streams"] for k, v in s.get("tags", {}).items()}
        tags.update({k.lower(): v for k, v in info["format"].get("tags", {}).items()})
        assert tags["title"] == song.title and tags["album"] == "Copperfield Hall"
        assert tags["artist"] == "Glass Harbor"
        assert tags["track"] == f"{song.index + 1}/3"
    assert not list((tmp_path / "Phone").rglob(".*.part"))


@needs_ffmpeg
def test_files_there_already_can_be_kept(tmp_path):
    source = tmp_path / "in.mkv"
    _make_video(source)
    v = video(source)
    songs = audio_export.plan(v, [0, 1], tmp_path / "Phone", audio_export.FLAC)
    songs[0].path.parent.mkdir(parents=True)
    songs[0].path.write_bytes(b"mine")
    assert audio_export.existing(songs) == [songs[0].path]
    written = audio_export.export_songs(v, songs, audio_export.FLAC, audio_export.Tags("A"),
                                        skip_existing=True)
    assert written == [songs[1].path] and songs[0].path.read_bytes() == b"mine"
    audio_export.export_songs(v, songs, audio_export.FLAC, audio_export.Tags("A"))
    assert songs[0].path.read_bytes() != b"mine"


@needs_ffmpeg
def test_a_cancelled_export_leaves_nothing_half_made(tmp_path):
    source = tmp_path / "in.mkv"
    _make_video(source, seconds=60)
    v = dict(video(source), duration=60.0,
             chapters=[{"start": 0.0, "end": 60.0, "title": "Northbound", "source": "manual"}])
    songs = audio_export.plan(v, [0], tmp_path / "Phone", audio_export.FLAC)
    cancel = threading.Event()

    def progress(done, total, fraction):
        cancel.set()

    with pytest.raises(audio_export.Cancelled):
        audio_export.export_songs(v, songs, audio_export.FLAC, audio_export.Tags("A"),
                                  cancel=cancel, progress_cb=progress)
    assert list((tmp_path / "Phone").rglob("*")) in ([], [songs[0].path.parent])


# --- the dialog ---------------------------------------------------------------------


@needs_ffmpeg
def test_the_dialog_exports_the_chosen_songs(app, tmp_path, monkeypatch):
    from mediabrowser.gui.dialogs import audio_export_dialog
    from mediabrowser.gui.dialogs.audio_export_dialog import AudioExportDialog

    source = tmp_path / "Concerts" / "Glass Harbor" / "in.mkv"
    source.parent.mkdir(parents=True)
    _make_video(source)
    monkeypatch.setattr(audio_export_dialog.exports, "library_roots", lambda: [])
    dialog = AudioExportDialog(None, "vid", video(source), [1, 0],
                               library_root=str(tmp_path / "Concerts"))
    assert dialog.songs.count() == 2
    assert dialog.artist.text() == "Glass Harbor"
    dialog.flac_radio.setChecked(True)
    dialog.folder.setText(str(tmp_path / "Phone"))
    dialog.own_folder.setChecked(False)
    finished = []
    dialog.finished.connect(finished.append)
    dialog.start()
    for _ in range(400):
        if finished:
            break
        app.processEvents()
        threading.Event().wait(0.02)
    assert finished == [1]
    assert sorted(p.name for p in dialog.written) == ["01 Paper Lanterns.flac",
                                                      "02 Chapter 2.flac"]


def test_the_dialog_refuses_a_library_folder(app, tmp_path, monkeypatch):
    from mediabrowser.gui.dialogs import audio_export_dialog
    from mediabrowser.gui.dialogs.audio_export_dialog import AudioExportDialog

    library = tmp_path / "Concerts"
    library.mkdir()
    monkeypatch.setattr(audio_export_dialog.exports, "library_roots", lambda: [str(library)])
    warned = []
    monkeypatch.setattr(audio_export_dialog.QMessageBox, "warning",
                        lambda *args: warned.append(args[2]))
    dialog = AudioExportDialog(None, "vid", video(), [0])
    dialog.folder.setText(str(library))
    dialog.start()
    assert warned and "never writes to" in warned[0]
    assert not dialog._running
