# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""DVD folders: titles and chapters from the IFOs, found by a scan, played
by mpv, and read for frames and songs - to the frame - without ever
writing to the disc."""

import shutil
import subprocess

import pytest

from mediabrowser.core import (
    artwork,
    audio_export,
    chapter_export,
    config,
    dvd,
    folders,
    frames,
    library,
    playback,
    player,
    scanner,
    store,
)
from tests import dvd_builder

pytestmark = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="needs ffmpeg"
)


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "LIBRARIES_DIR", tmp_path / "data" / "libraries")
    monkeypatch.setattr(config, "ARTWORK_DIR", tmp_path / "data" / "artwork")
    monkeypatch.setattr(config, "LEGACY_LIBRARY_FILE", tmp_path / "data" / "library.json")
    dvd.clear_cache()


@pytest.fixture
def disc(tmp_path):
    """A concert DVD: one title playing three songs, a title for each of
    the last two songs on its own, and the files a scan must not list."""
    root = tmp_path / "Concerts" / "Glass Harbor" / "COPPERFIELD_HALL"
    dvd_builder.build(root, titles=[(0.0, 8.0, 16.0), (8.0,), (16.0,)], seconds=24.0)
    return root


def as_video(root, title_idx=0):
    title = next(t for t in dvd.probe_dvd_disc(root, 1) if t["title_idx"] == title_idx)
    return {"type": "dvd", "path": str(root), "title_idx": title_idx, "vts": title["vts"],
            "display_name": "Copperfield Hall", "duration": title["duration"],
            "chapters": title["chapters"]}


def test_titles_and_chapters_come_from_the_ifos(disc):
    titles = dvd.read_titles(disc)
    assert [t.index for t in titles] == [0, 1, 2]
    whole = titles[0]
    assert whole.duration == pytest.approx(24.0, abs=0.05)
    starts = [c["start"] for c in whole.chapters]
    assert len(starts) == 3 and starts[0] == 0.0
    # Cut at the VOBU at or after each chapter asked for: within a GOP.
    assert starts[1] == pytest.approx(8.0, abs=0.9) and starts[2] == pytest.approx(16.0, abs=0.9)
    assert whole.chapters[-1]["end"] == pytest.approx(24.0, abs=0.05)
    assert whole.audio == [{"channels": 2, "coding": "ac3", "language": "en", "map": "i:128"}]


def test_a_title_for_each_song_is_left_out_as_the_whole_shows_play_them(disc):
    assert [t["title_idx"] for t in dvd.probe_dvd_disc(disc, 1)] == [0]
    # And titles too short to be anything are left out whatever they play.
    assert dvd.probe_dvd_disc(disc, 25) == []


def test_found_whatever_case_its_files_are_in_or_from_its_video_ts_folder(tmp_path):
    root = dvd_builder.build(tmp_path / "RIP", lowercase=True)
    assert dvd.is_dvd_root(root) and len(dvd.read_titles(root)) == 1
    assert dvd.is_dvd_root(root / "video_ts")
    assert library.disc_name_of(root / "video_ts") == "RIP"
    assert not dvd.is_dvd_root(tmp_path)


def test_a_broken_disc_says_so(tmp_path):
    folder = tmp_path / "BAD" / "VIDEO_TS"
    folder.mkdir(parents=True)
    (folder / "VIDEO_TS.IFO").write_bytes(b"not a dvd")
    with pytest.raises(dvd.DvdError):
        dvd.read_titles(tmp_path / "BAD")


def test_a_scan_lists_the_disc_not_its_vobs(disc):
    library_root = disc.parent.parent
    (library_root / "loose.vob").write_bytes(b"")  # a VOB on its own is still a file
    units = list(scanner.find_media_units(library_root))
    assert ("dvd", disc) in units
    assert not any("VTS_01" in str(path) for _kind, path in units)


def test_a_rescan_makes_its_titles_videos_and_keeps_their_names(disc):
    library_root = disc.parent.parent
    data = library.rescan(library_root)
    (video_id, video), = data["videos"].items()
    assert video_id == store.make_video_id("dvd", disc, 0)
    assert video["type"] == "dvd" and video["display_name"] == "COPPERFIELD_HALL"
    assert library.media_label(video) == "DVD"
    assert len(video["chapters"]) == 3
    video["chapters"][1].update(title="Northbound", source="manual")
    store.save_library(data)
    again = library.rescan(library_root)
    assert again["videos"][video_id]["chapters"][1]["title"] == "Northbound"
    assert library.original_chapters(video, 20)[1]["title"] is None
    assert folders.own_folder(video) == disc


def test_a_disc_moved_within_the_library_keeps_its_names(disc):
    library_root = disc.parent.parent
    data = library.rescan(library_root)
    video_id = next(iter(data["videos"]))
    data["videos"][video_id]["chapters"][0].update(title="Paper Lanterns", source="manual")
    store.save_library(data)
    moved = library_root / "Elsewhere" / disc.name
    moved.parent.mkdir()
    disc.rename(moved)
    again = library.rescan(library_root)
    (video,) = again["videos"].values()
    assert video["path"] == str(moved) and video["chapters"][0]["title"] == "Paper Lanterns"


def test_mpv_is_given_the_title_as_it_counts_them(disc):
    segment = playback.Segment(video_id="v", kind="dvd", path=str(disc), title_idx=0,
                               audio_only=True, start=4.0, end=8.0, entries=(0,), starts=(4.0,))
    url, options = player.mpv_target(segment)
    assert url == f"dvd://0/{disc}"
    assert options["start"] == "4.000" and options["end"] == "8.000"


def test_mpv_opens_a_title_for_editing_on_the_disc(disc, monkeypatch):
    launched = {}
    p = player.Player()
    monkeypatch.setattr(p, "_launch", lambda target, *a, **k: launched.setdefault("t", target))
    p.open_for_editing(as_video(disc))
    assert launched["t"] == [f"--dvd-device={disc}", "dvd://0"]


def _frame_number(jpeg: bytes) -> int:
    """Which frame (mod 50) a grab is, from its brightness: the builder
    makes frame N's luma 16 + 4N, which a JPEG holds full range."""
    out = subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "image2pipe", "-i", "-", "-vf",
         "signalstats,metadata=mode=print:key=lavfi.signalstats.YAVG:file=-", "-f", "null", "-"],
        input=jpeg, capture_output=True, check=True).stdout.decode()
    full = float(out.split("YAVG=")[1].split()[0])
    return round(full * 219 / 255 / 4)


@pytest.mark.parametrize("seconds", [1.0, 4.4, 6.72, 10.0, 17.36, 23.0])
def test_a_frame_grab_lands_on_the_frame_asked_for(disc, seconds):
    jpeg = frames.grab(as_video(disc), seconds, width=96)
    assert jpeg and jpeg.startswith(b"\xff\xd8")
    assert abs(_frame_number(jpeg) - round(seconds * 25) % 50) <= 1


def test_a_disc_with_blank_navigation_packs_is_still_read(tmp_path):
    root = dvd_builder.build(tmp_path / "PLAIN")
    vob = root / "VIDEO_TS" / "VTS_01_1.VOB"
    data = bytearray(vob.read_bytes())
    for sector in range(len(data) // dvd.SECTOR):
        at = sector * dvd.SECTOR
        if data[at + 0x26:at + 0x2A] == b"\x00\x00\x01\xbf":
            data[at + 0x45:at + 0x49] = bytes(4)
    vob.write_bytes(bytes(data))
    where = dvd.position(as_video(root), 6.0)
    assert where.ranges[0][0] > 0 and where.seconds_before >= 0
    jpeg = frames.grab(as_video(root), 6.0, width=96)
    assert jpeg and abs(_frame_number(jpeg) - 150 % 50) <= 13


def test_several_frames_at_once(disc):
    grabbed = frames.grab_many(as_video(disc), [1.0, 9.0, 20.0], width=96)
    assert sorted(grabbed) == [1.0, 9.0, 20.0]


def test_songs_export_at_their_chapters_lengths(disc, tmp_path):
    video = as_video(disc)
    songs = audio_export.plan(video, [0, 1, 2], tmp_path / "Phone", audio_export.FLAC)
    written = audio_export.export_songs(video, songs, audio_export.FLAC,
                                        audio_export.Tags("Copperfield Hall"))
    for song, path in zip(songs, written, strict=True):
        length = float(subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
             str(path)], check=True, capture_output=True, text=True).stdout)
        assert length == pytest.approx(song.duration, abs=0.05)


def test_the_stereo_lpcm_track_is_taken_from_a_dvd():
    streams = [{"channels": 6, "coding": "ac3", "map": "i:128"},
               {"channels": 2, "coding": "ac3", "map": "i:129"},
               {"channels": 2, "coding": "lpcm", "map": "i:160"}]
    number, info = audio_export.choose_stream(streams)
    assert info["map"] == "i:160"


def test_a_cover_is_a_frame_of_it(disc):
    video = as_video(disc)
    assert artwork.find("dvd-cover", video) is not None


def test_its_chapters_export_like_any_others(disc, tmp_path):
    video = as_video(disc)
    text = chapter_export.cue_sheet(video)
    assert 'FILE "Copperfield Hall.mkv" WAVE' in text and text.count("TRACK") == 3


def test_nothing_on_the_disc_changes(disc, tmp_path):
    def snapshot():
        return {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in disc.rglob("*") if p.is_file()}

    before = snapshot()
    for path in [disc, *disc.rglob("*")]:
        path.chmod(0o555 if path.is_dir() else 0o444)
    try:
        library.rescan(disc.parent.parent)
        video = as_video(disc)
        frames.grab(video, 2.0)
        songs = audio_export.plan(video, [0], tmp_path / "Phone", audio_export.OPUS)
        audio_export.export_songs(video, songs, audio_export.OPUS, audio_export.Tags("A"))
    finally:
        for path in [disc, *disc.rglob("*")]:
            path.chmod(0o755 if path.is_dir() else 0o644)
    assert snapshot() == before
