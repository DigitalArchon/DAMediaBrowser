# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""mpv, driven as a single managed background process.

Starting playback of anything stops whatever this app started before, the
way a music player only ever has one thing playing.

The app talks to it over mpv's JSON IPC socket, which is how the transport
bar knows where playback has got to and how a seek reaches a process that
is already running. Playing the queue, one mpv is kept for the whole of it
(start_session) and handed each piece in turn with loadfile - drawn into a
window of the app's when given one, else in mpv's own window.
"""

import json
import os
import socket
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from . import config

# A live mpv answers in about a millisecond. This is the budget for one that
# has died without the socket noticing yet, and it is spent on whichever
# thread is polling - so keep it short.
REQUEST_TIMEOUT_SECONDS = 0.25

# What moving playback to another mpv takes along. The player's settings
# carry on for everything after; the tracks chosen only mean anything in
# the file they were chosen in, so they go with that file alone.
CARRIED_SETTINGS = ("volume", "mute", "sub-visibility")
CARRIED_TRACKS = ("aid", "sid")


def x11_environment(environ=None) -> dict:
    """mpv's environment for drawing into an X11 window of ours. On a
    Wayland desktop mpv otherwise picks its Wayland output first, which
    can't draw into another program's window: it ignores --wid and opens
    a window of its own. Without WAYLAND_DISPLAY it uses X11 (XWayland)."""
    env = dict(os.environ if environ is None else environ)
    env.pop("WAYLAND_DISPLAY", None)
    return env


class Player:
    def __init__(self):
        self._proc = None
        self._request_id = 0
        # id(self) is a memory address: unique within one run, but reused
        # after a Player is collected and repeated across concurrent
        # processes, so two instances could end up talking to each other's
        # mpv. PID plus a random suffix cannot collide, and stop() unlinks
        # it rather than leaving it behind in /tmp.
        self._socket_path = (
            Path(tempfile.gettempdir())
            / f"media-chapter-browser-mpv-{os.getpid()}-{uuid.uuid4().hex[:8]}.sock"
        )
        # Where mpv's own S key puts a screenshot, in its own window. None:
        # the folder it runs in.
        self.screenshot_dir: Path | None = None

    # --- process ---------------------------------------------------------

    def stop(self):
        """End playback on purpose.

        Clearing _proc is also what keeps take_exit() quiet about this one:
        a stop nobody should follow up on leaves nothing behind to report.
        """
        if self._proc is not None and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        self._proc = None
        self._socket_path.unlink(missing_ok=True)

    def is_active(self):
        return self._proc is not None and self._proc.poll() is None

    def take_exit(self):
        """The exit code of a process that has ended by itself since this was
        last called, or None.

        Reports once and then forgets, so a caller polling on a timer gets
        exactly one chance to act on it. A deliberate stop() is never
        reported: nothing should follow it.
        """
        if self._proc is None:
            return None
        code = self._proc.poll()
        if code is None:
            return None
        self._proc = None
        self._socket_path.unlink(missing_ok=True)
        return code

    def _launch(self, target_args, start, end, audio_only, extra=(), env=None):
        self.stop()

        args = [
            config.MPV_BINARY, *target_args,
            f"--input-ipc-server={self._socket_path}",
            *extra,
        ]
        if self.screenshot_dir is not None:
            args.append(f"--screenshot-directory={self.screenshot_dir}")
        if start is not None:
            args.append(f"--start={start:.3f}")
        if end is not None:
            args.append(f"--end={end:.3f}")
        if audio_only:
            args.append("--no-video")

        # mpv puts a screenshot (its S key, in its own window) in the folder
        # it runs in: never the one the app happened to be started from,
        # which could be a library's.
        self._proc = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            cwd=Path.home(),
        )

    def start_session(self, window_id: int | None = None) -> None:
        """An mpv that waits to be given things to play (load), and stays
        when it runs out. Drawn into `window_id` when given - with no
        controls of its own, the app's are around it - else in its own
        window, which only opens for video."""
        extra = ["--idle=yes", "--prefetch-playlist=yes", "--keep-open=no",
                 "--force-window=no"]
        env = None
        if window_id is not None:
            extra += [f"--wid={int(window_id)}", "--osc=no", "--osd-level=0",
                      "--input-default-bindings=no", "--input-vo-keyboard=no",
                      "--cursor-autohide=1000"]
            env = x11_environment()
        self._launch([], None, None, False, extra, env)

    def load(self, segment, append: bool = False, options: dict | None = None) -> int | None:
        """Give a session a playback.Segment: to play now, or (`append`) to
        play when what's playing ends. Returns mpv's id for the entry.
        `options` are mpv's, for this entry alone, over the segment's own."""
        url, segment_options = mpv_target(segment)
        options = {**segment_options, **(options or {})}
        reply = self._call({
            "name": "loadfile", "url": url, "flags": "append" if append else "replace",
            "options": ",".join(f"{key}={value}" for key, value in options.items()),
        })
        return reply.get("playlist_entry_id") if isinstance(reply, dict) else None

    def carry(self) -> dict:
        """What another mpv needs to carry on from where this one is: the
        moment, whether paused, the volume and subtitles, and the tracks
        chosen. Only what mpv could say."""
        carried = {}
        for name in ("time-pos", "pause", *CARRIED_SETTINGS, *CARRIED_TRACKS):
            value = self._request(["get_property", name])
            if value is not None:
                carried[name] = value
        return carried

    def resume(self, segment, carried: dict) -> int | None:
        """Load `segment` into a session just started, carrying on where
        another mpv left off (its carry()). Returns mpv's id for the entry."""
        for name in CARRIED_SETTINGS:
            if name in carried:
                self._call(["set_property", name, carried[name]])
        # Set before the load, it holds for the file once it has opened.
        if carried.get("pause"):
            self.set_paused(True)
        options = {}
        if isinstance(carried.get("time-pos"), int | float):
            options["start"] = f"{max(0.0, carried['time-pos']):.3f}"
        for name in CARRIED_TRACKS:
            if name in carried:
                value = carried[name]
                options[name] = "no" if value is False else str(value)
        return self.load(segment, options=options)

    def playlist_clear(self) -> None:
        """Drop everything from mpv's playlist but what's playing."""
        self._call(["playlist-clear"])

    def playlist_remove(self, index: int) -> None:
        self._call(["playlist-remove", int(index)])

    def set_end(self, end: float | None) -> None:
        """Move where what's playing stops, while it plays."""
        self._call(["set_property", "end", "none" if end is None else f"{end:.3f}"])

    def session_state(self) -> dict:
        """Everything a session's tick needs: position, duration and pause;
        the id of the entry mpv is on (the one playing, or being opened);
        and whether it has run out of things to play."""
        playlist = self._request(["get_property", "playlist"])
        current = next((item for item in playlist or []
                        if isinstance(item, dict) and item.get("current")), None)
        idle = (playlist is not None and current is None
                and bool(self._request(["get_property", "idle-active"])))
        return dict(self.state(), playing_id=current.get("id") if current else None,
                    idle=idle)

    def open_for_editing(self, video, start: float = 0.0, window_id: int | None = None,
                         paused: bool = True):
        """A whole video - file or Blu-ray title - for the chapter editor:
        drawn into `window_id` (a native window of ours) when given, else in
        mpv's own window; held open at the end rather than quitting, so the
        last seconds can be marked; and seeking exactly rather than to the
        nearest keyframe, since a chapter goes exactly where it's put."""
        # A Blu-ray's stream has no keyframe index, so an exact seek could
        # start decoding past the keyframe it needs and show a frame of grey
        # blocks; starting the demuxer a couple of seconds early avoids it.
        extra = ["--keep-open=always", "--hr-seek=yes", "--hr-seek-demuxer-offset=2",
                 "--osd-level=0", "--osc=no", "--force-window=immediate"]
        env = None
        if window_id is not None:
            # Keys belong to the editor around the picture, not to mpv.
            extra += [f"--wid={int(window_id)}", "--input-vo-keyboard=no",
                      "--input-cursor=no", "--cursor-autohide=no"]
            env = x11_environment()
        if paused:
            extra.append("--pause")
        if video.get("type") == "bluray":
            target = [f"--bluray-device={video['path']}", f"bd://{video.get('title_idx', 0)}"]
        elif video.get("type") == "dvd":
            target = [f"--dvd-device={video['path']}", f"dvd://{video.get('title_idx', 0)}"]
        else:
            target = [str(video["path"])]
        self._launch(target, start if start > 0 else None, None, False, extra, env)

    # --- IPC -------------------------------------------------------------

    def _send(self, command, retries=3):
        """Fire a command and don't wait for an answer.

        Retried because the socket file takes a moment to appear after
        launch, and the first command often arrives inside that window.
        """
        if not self.is_active():
            return False
        payload = json.dumps({"command": command}).encode("utf-8") + b"\n"
        for _attempt in range(retries):
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                    sock.settimeout(1.0)
                    sock.connect(str(self._socket_path))
                    sock.sendall(payload)
                return True
            except OSError:
                time.sleep(0.15)
        return False

    def _call(self, command, attempts: int = 20):
        """Send a command and wait for mpv to have done it, returning its
        `data`. Retried while the socket is still appearing after launch,
        so a session can be loaded the moment it starts."""
        if not self.is_active():
            return None
        self._request_id += 1
        request_id = self._request_id
        payload = json.dumps({"command": command, "request_id": request_id}).encode() + b"\n"
        for _attempt in range(attempts):
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                    sock.settimeout(2.0)
                    sock.connect(str(self._socket_path))
                    sock.sendall(payload)
                    return _reply(sock, request_id)
            except (FileNotFoundError, ConnectionRefusedError):
                if not self.is_active():
                    return None
                time.sleep(0.05)
            except OSError:
                return None
        return None

    def _request(self, command):
        """Send a command and return its `data`, or None.

        mpv interleaves unsolicited event messages with replies on the same
        socket, so the reply is found by request_id rather than by taking
        whatever line arrives first. Not retried: a caller polling on a
        timer will be back in a moment anyway.
        """
        if not self.is_active():
            return None
        self._request_id += 1
        request_id = self._request_id
        payload = json.dumps(
            {"command": command, "request_id": request_id}
        ).encode("utf-8") + b"\n"

        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(REQUEST_TIMEOUT_SECONDS)
                sock.connect(str(self._socket_path))
                sock.sendall(payload)

                buffer = b""
                while True:
                    chunk = sock.recv(4096)
                    if not chunk:
                        return None
                    buffer += chunk
                    while b"\n" in buffer:
                        line, _, buffer = buffer.partition(b"\n")
                        message = _parse(line)
                        if message is None or message.get("request_id") != request_id:
                            continue
                        if message.get("error") != "success":
                            return None
                        return message.get("data")
        except OSError:
            return None

    # --- transport -------------------------------------------------------

    def toggle_pause(self):
        return self._send(["cycle", "pause"])

    def set_paused(self, paused):
        # Waited for, unlike the other transport commands: a session sets it
        # just before loading the next file, and a command sent without
        # waiting can arrive after that load.
        self._call(["set_property", "pause", bool(paused)])

    def seek(self, seconds):
        """Jump to an absolute position, in seconds from the file's start.

        Note that is the file's start, not the chapter's: a chapter played
        with --start is still positioned within the whole file.
        """
        return self._send(["seek", float(seconds), "absolute"], retries=1)

    def seek_exact(self, seconds):
        """Jump to exactly this moment, not the keyframe before it."""
        return self._send(["seek", float(seconds), "absolute+exact"], retries=1)

    def frame_step(self, forward: bool = True):
        """One frame on or back, pausing there."""
        return self._send(["frame-step" if forward else "frame-back-step"], retries=1)

    def set_volume(self, percent):
        return self._send(["set_property", "volume", float(percent)], retries=1)

    # --- what mpv's own keys would do --------------------------------------
    #
    # Drawn into a window of the app's, mpv has no keys of its own; the app
    # takes these and asks for them here.

    def cycle(self, name: str):
        """Step a property on - "sub-visibility", "sub", "audio" - and return
        what it is now."""
        self._call(["cycle", name])
        return self._request(["get_property", name])

    def current_track(self, kind: str) -> dict | None:
        """The "sub" or "audio" track playing, as mpv describes it (id,
        lang, title...), or None for none."""
        track = self._request(["get_property", f"current-tracks/{kind}"])
        return track if isinstance(track, dict) else None

    def screenshot(self, path) -> bool:
        """The picture as it is now, subtitles and all, saved to `path`
        (its extension says the format). Whether it was."""
        path = Path(path)
        # Sent as a list, the name is taken as it is: no ${...} expanded.
        self._call(["screenshot-to-file", str(path), "subtitles"])
        return path.exists()

    def show_text(self, text: str, milliseconds: int = 2000) -> None:
        """A message over the picture. Level 0: shown even with mpv's own
        messages turned off, as they are in the app's window."""
        self._send(["show-text", text, int(milliseconds), 0], retries=1)

    # --- state -----------------------------------------------------------

    def position(self):
        """Seconds from the start of the file, or None if not playing."""
        return _as_float(self._request(["get_property", "time-pos"]))

    def duration(self):
        return _as_float(self._request(["get_property", "duration"]))

    def is_paused(self):
        value = self._request(["get_property", "pause"])
        return bool(value) if value is not None else None

    def state(self):
        """Position, duration and pause in one round trip each.

        Returned together because the transport bar wants all three at once
        and asking separately triples the poll's cost.
        """
        return {
            "position": self.position(),
            "duration": self.duration(),
            "paused": self.is_paused(),
        }


def mpv_target(segment) -> tuple[str, dict]:
    """What to hand mpv's loadfile for a playback.Segment: the URL, and
    options for that entry alone. A Blu-ray or DVD title carries its disc in
    the URL - bluray-device and dvd-device can't be given per entry. Both
    count titles from 0, as the app stores them."""
    if segment.kind == "bluray":
        url = f"bd://{segment.title_idx or 0}/{segment.path}"
    elif segment.kind == "dvd":
        url = f"dvd://{segment.title_idx or 0}/{segment.path}"
    else:
        url = str(segment.path)
    options = {
        "start": f"{segment.start:.3f}",
        "end": "none" if segment.end is None else f"{segment.end:.3f}",
        "vid": "no" if segment.audio_only else "auto",
    }
    return url, options


def _reply(sock, request_id):
    buffer = b""
    while True:
        chunk = sock.recv(65536)
        if not chunk:
            return None
        buffer += chunk
        while b"\n" in buffer:
            line, _, buffer = buffer.partition(b"\n")
            message = _parse(line)
            if message is None or message.get("request_id") != request_id:
                continue
            return message.get("data") if message.get("error") == "success" else None


def _parse(line):
    try:
        message = json.loads(line)
    except (ValueError, TypeError):
        return None
    return message if isinstance(message, dict) else None


def _as_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
