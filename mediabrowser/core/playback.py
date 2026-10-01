# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Playback policy: what is played, in what order, and how one mpv is kept
playing it - independent of any UI.

The window used to own this - which of a video's two shapes to hand the
player, when an end point applies, and what counts as a next or previous
chapter. None of it is about widgets, and all of it is worth testing.
"""

from dataclasses import dataclass, replace

from . import utils

REPEAT_OFF = "off"
REPEAT_ONE = "one"
REPEAT_ALL = "all"


@dataclass(frozen=True)
class QueueEntry:
    """One thing to play. Carries its own labels so a queue survives the
    library being reloaded underneath it: the row still reads correctly even
    if the video it came from has gone.
    """

    video_id: str
    chapter_index: int
    audio_only: bool
    title: str
    video_name: str
    duration: float


class Queue:
    """What is playing and what comes after it.

    Chapters used to be reachable only in the order they sat on one disc,
    with next and previous bounded inside a single video. A queue is what
    lets a run of tracks be assembled across several.

    Insertion order is kept separately from play order, so turning shuffle
    on and off again returns the queue to how it was written down rather
    than to a third arbitrary order.
    """

    def __init__(self):
        self._entries: list[QueueEntry] = []
        self._order: list[int] = []
        self._cursor = -1
        self.repeat = REPEAT_OFF
        self.shuffle = False

    # --- contents --------------------------------------------------------

    def __len__(self) -> int:
        return len(self._entries)

    def entries(self) -> list[QueueEntry]:
        return list(self._entries)

    def is_empty(self) -> bool:
        return not self._entries

    def clear(self) -> None:
        self._entries = []
        self._order = []
        self._cursor = -1

    def set_entries(self, entries, start: int = 0) -> QueueEntry | None:
        """Replace the queue and start at `start` (an index into `entries`)."""
        self._entries = list(entries)
        if not self._entries:
            self._order = []
            self._cursor = -1
            return None
        start = max(0, min(start, len(self._entries) - 1))
        self._rebuild_order(first=start)
        return self.current()

    def append(self, entries) -> None:
        """Add to the end without disturbing what is playing."""
        first_new = len(self._entries)
        self._entries.extend(entries)
        added = list(range(first_new, len(self._entries)))
        if self.shuffle:
            import random

            random.shuffle(added)
        self._order.extend(added)
        if self._cursor < 0 and self._order:
            self._cursor = 0

    def remove(self, entry_index: int) -> None:
        """Drop one entry by its position in insertion order."""
        if not 0 <= entry_index < len(self._entries):
            return
        playing = self.current_index()
        del self._entries[entry_index]

        self._order = [i - 1 if i > entry_index else i
                       for i in self._order if i != entry_index]
        if not self._order:
            self._cursor = -1
            return
        # Removing something keeps whatever was playing playing, unless it
        # was the thing removed - then the next one takes its place.
        if playing == entry_index:
            self._cursor = min(self._cursor, len(self._order) - 1)
        elif playing is not None:
            target = playing - 1 if playing > entry_index else playing
            self._cursor = self._order.index(target)

    def move(self, from_index: int, to_index: int) -> None:
        """Reorder the queue itself, in insertion order."""
        if not 0 <= from_index < len(self._entries):
            return
        to_index = max(0, min(to_index, len(self._entries) - 1))
        if from_index == to_index:
            return
        playing = self.current_index()
        entry = self._entries.pop(from_index)
        self._entries.insert(to_index, entry)
        self._reindex_after_move(from_index, to_index, playing)

    def _reindex_after_move(self, from_index, to_index, playing):
        def shifted(i):
            if i == from_index:
                return to_index
            if from_index < i <= to_index:
                return i - 1
            if to_index <= i < from_index:
                return i + 1
            return i

        self._order = [shifted(i) for i in self._order]
        if playing is not None:
            self._cursor = self._order.index(shifted(playing))

    def set_audio_only(self, entry_index: int, audio_only: bool) -> list[int]:
        """Play an entry as audio or as video, and the rest of its video's
        run after it in the play order with it - the way Play lines up the
        rest of a video. Returns the entry indices changed."""
        if entry_index not in self._order:
            return []
        video_id = self._entries[entry_index].video_id
        changed = []
        for index in self._order[self._order.index(entry_index):]:
            entry = self._entries[index]
            if entry.video_id != video_id:
                break
            if entry.audio_only != audio_only:
                self._entries[index] = replace(entry, audio_only=audio_only)
                changed.append(index)
        return changed

    def remove_many(self, entry_indices) -> None:
        """Drop several entries at once, by their positions in insertion order."""
        for index in sorted(set(entry_indices), reverse=True):
            self.remove(index)

    def reorder(self, new_order) -> None:
        """Rewrite the queue's own order: `new_order` lists every entry's
        current index, in the order they should now be written down. What
        is playing stays playing."""
        new_order = list(new_order)
        if sorted(new_order) != list(range(len(self._entries))):
            return
        playing = self.current_index()
        where = {old: new for new, old in enumerate(new_order)}
        self._entries = [self._entries[old] for old in new_order]
        if self.shuffle:
            self._order = [where[i] for i in self._order]
            if playing is not None:
                self._cursor = self._order.index(where[playing])
        else:
            self._order = list(range(len(self._entries)))
            self._cursor = where[playing] if playing is not None else -1
            if self._cursor < 0 and self._order:
                self._cursor = 0

    def remap_video(self, video_id: str, video, index_map) -> None:
        """Re-point one video's entries after its chapters were split,
        merged or replaced, keeping them on the same music.

        `index_map` takes a chapter's old index to its new one. Titles and
        lengths are re-read too, since a split or merge changes both. The
        entries stay where they are in the queue, so the play order and
        what is playing are untouched.
        """
        chapters = video["chapters"]
        if not chapters:
            return
        for i, entry in enumerate(self._entries):
            if entry.video_id != video_id:
                continue
            index = index_map(entry.chapter_index)
            index = max(0, min(index, len(chapters) - 1))
            chapter = chapters[index]
            self._entries[i] = replace(
                entry,
                chapter_index=index,
                title=utils.chapter_label(index, chapter),
                duration=chapter["end"] - chapter["start"],
            )

    # --- position --------------------------------------------------------

    def current(self) -> QueueEntry | None:
        index = self.current_index()
        return self._entries[index] if index is not None else None

    def order(self) -> list[int]:
        """Entry indices (insertion order) in the order they play."""
        return list(self._order)

    def position(self) -> int | None:
        """Where the cursor is in the play order."""
        return self._cursor if 0 <= self._cursor < len(self._order) else None

    def current_index(self) -> int | None:
        """Where the cursor is, in insertion order."""
        if not 0 <= self._cursor < len(self._order):
            return None
        return self._order[self._cursor]

    def jump_to(self, entry_index: int) -> QueueEntry | None:
        if entry_index not in self._order:
            return None
        self._cursor = self._order.index(entry_index)
        return self.current()

    def advance(self) -> QueueEntry | None:
        """The next thing to play, or None when the queue is done.

        Repeat-one is deliberately only honoured here, not in next(): asking
        for the next track should move on even with repeat-one set, or the
        button would appear broken.
        """
        if self.repeat == REPEAT_ONE:
            return self.current()
        return self.next()

    def next(self) -> QueueEntry | None:
        if not self._order:
            return None
        if self._cursor + 1 < len(self._order):
            self._cursor += 1
            return self.current()
        if self.repeat == REPEAT_ALL:
            # Reshuffle on the way round, so a repeating shuffled queue is
            # not the same order over and over.
            if self.shuffle:
                self._rebuild_order()
            self._cursor = 0
            return self.current()
        return None

    def previous(self) -> QueueEntry | None:
        if not self._order:
            return None
        if self._cursor > 0:
            self._cursor -= 1
            return self.current()
        if self.repeat == REPEAT_ALL:
            self._cursor = len(self._order) - 1
            return self.current()
        return None

    # --- modes -----------------------------------------------------------

    def set_shuffle(self, on: bool) -> None:
        """Turn shuffle on or off, keeping whatever is playing playing."""
        if on == self.shuffle:
            return
        self.shuffle = on
        playing = self.current_index()
        self._rebuild_order(first=playing)

    def _rebuild_order(self, first: int | None = None) -> None:
        """Recompute the play order, leaving `first` playing.

        Shuffled, that means lifting `first` to the front of a fresh random
        order. Unshuffled it must NOT reorder anything - starting an album
        at track 3 plays 3, 4, 5, and previous goes back to 2 - so there the
        cursor moves instead.
        """
        indices = list(range(len(self._entries)))
        if not indices:
            self._order = []
            self._cursor = -1
            return

        if self.shuffle:
            import random

            random.shuffle(indices)
            if first is not None and first in indices:
                indices.remove(first)
                indices.insert(0, first)
            self._cursor = 0
        else:
            self._cursor = indices.index(first) if first in indices else 0

        self._order = indices


def queue_entries_for(video_id: str, video, audio_only: bool) -> list[QueueEntry]:
    """Every chapter of one video, as queue entries."""
    return [
        QueueEntry(
            video_id=video_id,
            chapter_index=i,
            audio_only=audio_only,
            title=utils.chapter_label(i, chapter),
            video_name=video["display_name"],
            duration=chapter["end"] - chapter["start"],
        )
        for i, chapter in enumerate(video["chapters"])
    ]


# --- playing the queue through one mpv ---------------------------------------------
#
# Each chapter used to be its own mpv: it played to its end and exited, the
# window noticed on its next poll and started another. Every step cost a
# process start and a file open, and for video the window closed and opened
# again in between. Now one mpv plays the whole queue, and is always holding
# the piece playing and the piece after it, so it moves on by itself -
# having opened the next file early - and the app only follows.
#
# A piece is a run of the queue that one file plays straight through: the
# chapters of one video one after another, joined, so moving between them
# is no step at all.


@dataclass(frozen=True)
class Segment:
    """One piece for mpv: a stretch of one video covering one or more
    queue entries in a row."""

    video_id: str
    kind: str  # "file" or "bluray"
    path: str
    title_idx: int | None
    audio_only: bool
    start: float
    end: float | None  # None: on to the end of the file
    entries: tuple[int, ...]  # the queue entries it plays (insertion order), in order
    starts: tuple[float, ...]  # where each of those entries starts in the file

    def entry_at(self, position: float | None) -> int:
        """The entry playing at `position` seconds into the file."""
        chosen = self.entries[0]
        if position is None:
            return chosen
        for entry, start in zip(self.entries, self.starts, strict=True):
            # A seek lands a hair before where it was aimed.
            if position >= start - 0.25:
                chosen = entry
        return chosen


def _chapter_of(video_of, entry):
    video = video_of(entry.video_id)
    if video is None or not 0 <= entry.chapter_index < len(video["chapters"]):
        return None, None
    return video, video["chapters"][entry.chapter_index]


def plan_segment(queue: "Queue", video_of, position: int) -> Segment | None:
    """The piece that starts at play-order `position`: that entry and the
    ones straight after it that carry on from it in the same video. None
    if there's nothing playable there (its video gone, say).

    Video carries on past the last chapter to the end of the file when
    nothing is queued after it - a chapter of a concert film is a place to
    start, not a clip. Audio stops where its track does.
    """
    order, entries = queue.order(), queue.entries()
    if not 0 <= position < len(order):
        return None
    first = entries[order[position]]
    video, chapter = _chapter_of(video_of, first)
    if video is None:
        return None
    chapters = video["chapters"]
    chosen = [position]
    if queue.repeat != REPEAT_ONE:
        while chosen[-1] + 1 < len(order):
            before = entries[order[chosen[-1]]]
            after = entries[order[chosen[-1] + 1]]
            if (after.video_id != before.video_id or after.audio_only != before.audio_only
                    or after.chapter_index != before.chapter_index + 1
                    or after.chapter_index >= len(chapters)):
                break
            chosen.append(chosen[-1] + 1)
    indices = [entries[order[p]].chapter_index for p in chosen]
    followed = chosen[-1] + 1 < len(order) or queue.repeat in (REPEAT_ALL, REPEAT_ONE)
    end = chapters[indices[-1]]["end"]
    if not first.audio_only and not followed:
        end = None
    return Segment(
        video_id=first.video_id,
        kind=video["type"],
        path=video["path"],
        title_idx=video.get("title_idx"),
        audio_only=first.audio_only,
        start=chapter["start"],
        end=end,
        entries=tuple(order[p] for p in chosen),
        starts=tuple(chapters[i]["start"] for i in indices),
    )


def following(queue: "Queue", position: int) -> int | None:
    """The play-order position after `position`, as the queue's repeat
    would have it: the same one again under repeat-one, the first again
    after the last under repeat-all."""
    if queue.repeat == REPEAT_ONE:
        return position
    if position + 1 < len(queue.order()):
        return position + 1
    if queue.repeat == REPEAT_ALL and queue.order():
        return 0
    return None


@dataclass
class Tick:
    """What one look at the playing mpv found."""

    position: float | None = None  # seconds into the file
    duration: float | None = None  # of the file
    paused: bool | None = None
    moved: bool = False  # the queue has moved on to another entry
    finished: bool = False  # the queue has played out
    closed: bool = False  # mpv went away - its window was closed


class Session:
    """The queue, played through one mpv.

    `player` is a core.player.Player (or a stand-in with the same session
    methods); `video_of` looks a video up by id. Where the video is drawn is
    fixed per session: into `window_id`, a window of ours, or mpv's own
    window when that's None. Asking to play somewhere else starts a new mpv.
    """

    def __init__(self, player, queue: Queue, video_of) -> None:
        self.player = player
        self.queue = queue
        self._video_of = video_of
        self._window_id: int | None = None
        # What mpv's playlist holds: the piece playing, and the next.
        self._loaded: list[tuple[Segment, int | None]] = []

    def running(self) -> bool:
        return bool(self._loaded) and self.player.is_active()

    def current(self) -> Segment | None:
        return self._loaded[0][0] if self._loaded else None

    def window_id(self) -> int | None:
        return self._window_id

    # --- starting and stopping ---------------------------------------------

    def play(self, window_id: int | None = None) -> Segment | None:
        """Play from the queue's current entry. None if nothing there can
        be played."""
        segment = self._first_playable(self.queue.position())
        if segment is None:
            self.stop()
            return None
        if not self.player.is_active() or window_id != self._window_id:
            self.player.start_session(window_id)
            self._window_id = window_id
        else:
            # Paused stays paused across files in one mpv; choosing
            # something to play means play it. Before the load: told while
            # mpv is opening the file, it's paused again once it has.
            self.player.set_paused(False)
        self._loaded = [(segment, self.player.load(segment))]
        self._load_next()
        return segment

    def stop(self) -> None:
        self._loaded = []
        self.player.stop()

    def _first_playable(self, position: int | None) -> Segment | None:
        """The piece at `position`, or the first after it that can play,
        moving the queue there."""
        tried = 0
        while position is not None and tried < len(self.queue):
            segment = plan_segment(self.queue, self._video_of, position)
            if segment is not None:
                self.queue.jump_to(segment.entries[0])
                return segment
            position = following(self.queue, position)
            tried += 1
        return None

    def _position_of(self, entry_index: int) -> int | None:
        order = self.queue.order()
        return order.index(entry_index) if entry_index in order else None

    def _load_next(self) -> None:
        """Hand mpv the piece after the one playing, so it can open it early
        and go straight on."""
        current = self.current()
        last = self._position_of(current.entries[-1]) if current else None
        position = following(self.queue, last) if last is not None else None
        # Past anything that can't be played now - a video whose file is away.
        for _tried in range(len(self.queue)):
            if position is None:
                return
            segment = plan_segment(self.queue, self._video_of, position)
            if segment is not None:
                self._loaded.append((segment, self.player.load(segment, append=True)))
                return
            position = following(self.queue, position)

    def queue_changed(self) -> None:
        """The queue was edited while playing: re-plan what's playing (it
        may now end sooner, or later) and what comes after it."""
        if not self._loaded:
            return
        playing = self.current()
        position = self.queue.position()
        replanned = (plan_segment(self.queue, self._video_of, position)
                     if position is not None else None)
        if (replanned is None or replanned.video_id != playing.video_id
                or replanned.audio_only != playing.audio_only):
            # What's playing isn't in the queue any more; it plays on alone.
            replanned = replace(playing, entries=playing.entries[:1],
                                starts=playing.starts[:1])
        elif replanned.end != playing.end:
            self.player.set_end(replanned.end)
        # Keep where the playing piece started; entries before the playing
        # one have been played.
        self._loaded[0] = (replace(replanned, start=playing.start), self._loaded[0][1])
        self.player.playlist_clear()
        del self._loaded[1:]
        self._load_next()

    # --- following it ----------------------------------------------------

    def tick(self) -> Tick:
        if not self._loaded:
            return Tick()
        if not self.player.is_active():
            self._loaded = []
            return Tick(closed=True)
        state = self.player.session_state()
        tick = Tick(state.get("position"), state.get("duration"), state.get("paused"))
        playing_id = state.get("playing_id")
        if state.get("idle"):
            self._loaded = []
            tick.finished = True
            return tick
        if len(self._loaded) > 1 and playing_id is not None and playing_id == self._loaded[1][1]:
            # mpv has gone on to the next piece by itself.
            self.player.playlist_remove(0)
            del self._loaded[0]
            self.queue.jump_to(self.current().entries[0])
            tick.moved = True
            self._load_next()
            return tick
        entry = self.current().entry_at(tick.position)
        if entry != self.queue.current_index():
            self.queue.jump_to(entry)
            tick.moved = True
        return tick
