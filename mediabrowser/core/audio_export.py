# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Songs as audio files, for listening away from the library - on a phone.

Each chosen chapter becomes one file, cut exactly at its start and end,
in a folder the person picks outside every library: FLAC to keep it
lossless, or Opus to keep it small. Each is tagged with its title, the
video's name as the album, its place in the video as the track number
and, given, the artist; and carries the video's cover when there is one
cached. Surround sound is mixed down to stereo, and a video with a
stereo track as well as a surround one gives the stereo one.

The video is only read (ffmpeg, as everywhere else). Each file is written
under a hidden name beside where it goes and only takes its own name once
it's whole, so a cancelled or failed export leaves nothing half-made.
"""

from __future__ import annotations

import base64
import json
import os
import struct
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

from . import chapter_export, frames, utils

FLAC = "flac"
OPUS = "opus"
FORMATS = (FLAC, OPUS)
LABELS = {FLAC: "FLAC (lossless)", OPUS: "Opus (small)"}
SUFFIXES = {FLAC: ".flac", OPUS: ".opus"}

# Opus at 160 kbit/s stereo is as good as anyone hears on a phone; the
# choice is there for a smaller or a larger file.
OPUS_BITRATES = (96, 128, 160, 192, 256)
DEFAULT_OPUS_BITRATE = 160

PROBE_TIMEOUT_SECONDS = 60


class ExportError(Exception):
    """Why a song couldn't be exported, in words for the person."""


class Cancelled(ExportError):
    pass


@dataclass(frozen=True)
class Song:
    index: int  # the chapter's, 0-based
    title: str
    start: float
    end: float
    path: Path  # where it goes

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


@dataclass(frozen=True)
class Tags:
    album: str
    artist: str = ""
    total: int = 0  # chapters in the video, for "3/18"


def file_name(video, index: int, fmt: str) -> str:
    """"03 Paper Lanterns.flac": the chapter's place in the video, so the
    songs sort as they play, and its name."""
    title = utils.chapter_label(index, video["chapters"][index])
    return f"{index + 1:02d} {chapter_export.safe_file_name(title)}{SUFFIXES[fmt]}"


def destination(video, folder, in_own_folder: bool = True) -> Path:
    """The folder a video's songs go in: the chosen one, or a folder in it
    named after the video, as a music player shows an album."""
    folder = Path(folder).expanduser()
    if in_own_folder:
        return folder / chapter_export.safe_file_name(video["display_name"])
    return folder


def plan(video, indices, folder, fmt: str, in_own_folder: bool = True) -> list[Song]:
    """The songs for these chapters, in the video's order."""
    where = destination(video, folder, in_own_folder)
    duration = video.get("duration") or 0.0
    songs = []
    for index in sorted(set(indices)):
        chapter = video["chapters"][index]
        start = max(0.0, float(chapter["start"]))
        end = float(chapter.get("end") or duration)
        songs.append(Song(index, utils.chapter_label(index, chapter), start,
                          max(start, end), where / file_name(video, index, fmt)))
    return songs


def refused_folder(folder, library_roots) -> str | None:
    """Why songs mustn't be saved into `folder`, or None if they may: never
    into a library, which the app never writes to."""
    root = chapter_export.inside_library(folder, library_roots)
    if root is not None:
        return f"{Path(folder).expanduser().resolve()} is inside the library {root}"
    return None


def existing(songs) -> list[Path]:
    """The songs' files that are there already."""
    return [song.path for song in songs if song.path.exists()]


def has_encoder(fmt: str) -> bool:
    """Whether this machine's ffmpeg can write `fmt`."""
    try:
        proc = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"],
                              capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return False
    name = "libopus" if fmt == OPUS else "flac"
    return any(line.split()[1:2] == [name] for line in proc.stdout.splitlines())


# --- reading the video -------------------------------------------------------------


@dataclass(frozen=True)
class Source:
    """How ffmpeg opens a video at a moment: its arguments, and for a disc
    read here and handed over, what writes it into ffmpeg's input."""

    args: list[str]
    feed: object = None  # callable(stream, cancel) writing the input, or None


def source(video, start: float) -> Source:
    """ffmpeg's input for `video` from `start`: a file seeked by time, a
    Blu-ray title by byte (frames.ffmpeg_input), a DVD title read from its
    VOBs here."""
    kind = video.get("type")
    if kind == "dvd":
        from . import dvd

        args, feed = dvd.ffmpeg_source(video, start)
        return Source(args, feed)
    position = None
    if kind == "bluray":
        position = frames.positions_for(video, [start]).get(start)
        if position is None:
            raise ExportError("couldn't find that moment on the disc")
    return Source(frames.ffmpeg_input(video, start, position))


def _probe_input(video) -> list[str] | None:
    """ffprobe's arguments for the video's streams; None for a DVD, whose
    streams are only known from ffmpeg reading it."""
    kind = video.get("type")
    if kind == "bluray":
        args = []
        if video.get("playlist") is not None:
            args += ["-playlist", str(video["playlist"])]
        return args + [f"bluray:{video['path']}"]
    if kind == "dvd":
        return None
    return [str(video["path"])]


def audio_streams(video) -> list[dict]:
    """The video's audio streams, in order: [{"channels", "sample_fmt"}],
    and for a DVD's, the "map" ffmpeg knows each by in its VOBs."""
    if video.get("type") == "dvd":
        from . import dvd

        return dvd.audio_streams(video)
    args = _probe_input(video)
    if args is None:
        return []
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=channels,sample_fmt", "-of", "json", *args],
            capture_output=True, text=True, timeout=PROBE_TIMEOUT_SECONDS,
        )
        streams = json.loads(proc.stdout or "{}").get("streams") or []
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return []
    return [{"channels": int(s.get("channels") or 0), "sample_fmt": s.get("sample_fmt") or ""}
            for s in streams]


def choose_stream(streams: list[dict]) -> tuple[int, dict]:
    """Which audio stream to take, and what it is: the first in stereo -
    uncompressed (a DVD's LPCM) before any other - else the first, mixed
    down."""
    stereo = [(stream.get("coding") != "lpcm", number) for number, stream in enumerate(streams)
              if stream["channels"] == 2]
    if stereo:
        number = min(stereo)[1]
        return number, streams[number]
    return 0, (streams[0] if streams else {})


# --- tags and cover ------------------------------------------------------------------


def _ffmeta(text: str) -> str:
    return chapter_export._ffmeta_escape(text)


def picture_block(jpeg: bytes) -> str:
    """A FLAC picture block for a front cover, base64'd: how an Ogg file -
    Opus - carries its cover, as the METADATA_BLOCK_PICTURE comment."""
    mime = b"image/jpeg"
    block = (struct.pack(">II", 3, len(mime)) + mime + struct.pack(">I", 0)
             + struct.pack(">IIIII", 0, 0, 0, 0, len(jpeg)) + jpeg)
    return base64.b64encode(block).decode("ascii")


def metadata_text(song: Song, tags: Tags, fmt: str, cover: bytes | None) -> str:
    lines = [";FFMETADATA1", f"title={_ffmeta(song.title)}"]
    if tags.album:
        lines.append(f"album={_ffmeta(tags.album)}")
    if tags.artist:
        lines += [f"artist={_ffmeta(tags.artist)}", f"album_artist={_ffmeta(tags.artist)}"]
    track = f"{song.index + 1}/{tags.total}" if tags.total else str(song.index + 1)
    lines.append(f"track={track}")
    if fmt == OPUS and cover:
        lines.append(f"METADATA_BLOCK_PICTURE={picture_block(cover)}")
    return "\n".join(lines) + "\n"


# --- writing -------------------------------------------------------------------------


def command(src: Source, song: Song, fmt: str, meta_file: str, partial: Path, *,
            stream: tuple[int, dict] = (0, {}), cover_file: str | None = None,
            bitrate: int = DEFAULT_OPUS_BITRATE) -> list[str]:
    number, info = stream
    # The video goes last: a Blu-ray's input ends with where to start
    # decoding from, which only means that as an option of the output.
    args = ["ffmpeg", "-nostdin", "-v", "error", "-nostats", "-progress", "pipe:1",
            "-i", meta_file]
    if fmt == FLAC and cover_file:
        args += ["-i", cover_file]
    media = 2 if fmt == FLAC and cover_file else 1
    which = f"{media}:{info['map']}" if info.get("map") else f"{media}:a:{number}"
    args += [*src.args, "-t", f"{song.duration:.3f}", "-map", which,
             "-map_metadata", "0", "-map_chapters", "-1"]
    if info.get("channels", 2) != 2:
        args += ["-ac", "2"]
    if fmt == FLAC:
        if str(info.get("sample_fmt", "")).startswith(("flt", "dbl")):
            # Decoded from a lossy track: 16 bits holds all there is.
            args += ["-sample_fmt", "s16"]
        args += ["-c:a", "flac"]
        if cover_file:
            args += ["-map", "1:v", "-c:v", "copy", "-disposition:v", "attached_pic",
                     "-metadata:s:v", "comment=Cover (front)"]
        args += ["-f", "flac"]
    else:
        args += ["-vn", "-c:a", "libopus", "-b:a", f"{int(bitrate)}k", "-f", "opus"]
    return args + ["-n", str(partial)]


def _partial(path: Path) -> Path:
    return path.with_name(f".{path.name}.part")


def export_song(video, song: Song, fmt: str, tags: Tags, *, cover: Path | None = None,
                stream: tuple[int, dict] = (0, {}), bitrate: int = DEFAULT_OPUS_BITRATE,
                cancel: threading.Event | None = None, progress_cb=None) -> Path:
    """Write one song. `progress_cb` gets how far through it is, 0 to 1.
    Raises Cancelled, or ExportError saying what went wrong."""
    song.path.parent.mkdir(parents=True, exist_ok=True)
    partial = _partial(song.path)
    if partial.exists():
        partial.unlink()  # left by an export that was stopped
    cover_bytes = None
    if cover is not None:
        try:
            cover_bytes = Path(cover).read_bytes()
        except OSError:
            cover = None
    src = source(video, song.start)
    work = Path(tempfile.mkdtemp(prefix="mcb-export-"))
    meta = work / "tags.txt"
    try:
        with open(meta, "w", encoding="utf-8") as handle:
            handle.write(metadata_text(song, tags, fmt, cover_bytes))
        args = command(src, song, fmt, str(meta), partial, stream=stream,
                       cover_file=str(cover) if cover and fmt == FLAC else None,
                       bitrate=bitrate)
        _run(args, src, song.duration, cancel, progress_cb)
        os.replace(partial, song.path)
    except BaseException:
        if partial.exists():
            partial.unlink()
        raise
    finally:
        meta.unlink(missing_ok=True)
        work.rmdir()
    return song.path


def _run(args, src: Source, duration: float, cancel, progress_cb) -> None:
    try:
        proc = subprocess.Popen(
            args, stdin=subprocess.PIPE if src.feed else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
    except OSError as exc:
        raise ExportError(f"could not run ffmpeg: {exc}") from exc
    errors: list[bytes] = []
    drain = threading.Thread(target=lambda: errors.extend(proc.stderr), daemon=True)
    drain.start()
    feeder = None
    stop_feeding = threading.Event()
    if src.feed:
        def feed():
            try:
                src.feed(proc.stdin, stop_feeding)
            except (BrokenPipeError, OSError, ValueError):
                pass  # ffmpeg had all it wanted
            finally:
                try:
                    proc.stdin.close()
                except OSError:
                    pass
        feeder = threading.Thread(target=feed, daemon=True)
        feeder.start()
    cancelled = False
    for raw in proc.stdout:
        line = raw.decode("utf-8", "replace")
        if cancel is not None and cancel.is_set():
            cancelled = True
            proc.kill()
            break
        if progress_cb and duration and line.startswith("out_time_us="):
            try:
                done = int(line.split("=", 1)[1]) / 1_000_000
            except ValueError:
                continue
            progress_cb(max(0.0, min(1.0, done / duration)))
    proc.wait()
    stop_feeding.set()
    if feeder is not None:
        feeder.join(timeout=5)
    drain.join(timeout=5)
    if cancelled:
        raise Cancelled("export cancelled")
    if proc.returncode != 0:
        message = b"".join(errors).decode("utf-8", "replace").strip()
        raise ExportError(message or f"ffmpeg exited with {proc.returncode}")


def export_songs(video, songs, fmt: str, tags: Tags, *, cover: Path | None = None,
                 bitrate: int = DEFAULT_OPUS_BITRATE, skip_existing: bool = False,
                 cancel: threading.Event | None = None, progress_cb=None) -> list[Path]:
    """Write each song in turn. `progress_cb` gets (songs done, of how
    many, overall fraction). Returns the files written; one already there
    is replaced unless `skip_existing`."""
    stream = choose_stream(audio_streams(video))
    written = []
    total = len(songs)
    for done, song in enumerate(songs):
        if cancel is not None and cancel.is_set():
            raise Cancelled("export cancelled")
        if skip_existing and song.path.exists():
            continue

        def each(fraction, done=done):
            if progress_cb:
                progress_cb(done, total, (done + fraction) / max(1, total))

        each(0.0)
        written.append(export_song(video, song, fmt, tags, cover=cover, stream=stream,
                                   bitrate=bitrate, cancel=cancel, progress_cb=each))
    if progress_cb:
        progress_cb(total, total, 1.0)
    return written
