# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""The libraries on show at once.

The shelf used to be one library. Now it can be any number of them: their
videos side by side, searched, sorted, queued and identified together, each
video still belonging to - and saved in - its own library, with that
library's locked, private and hidden settings.

The window works on `view()`. With one library on show that is the library
itself, exactly as before. With several it's a stand-in: its "videos" are
all of theirs (MergedVideos reads and writes each in its own library) and
its "settings" are the first library's, which is where anything about the
shelf as a whole - an Identify run's history - is kept. Saving goes
through the shelf, library by library.
"""

from __future__ import annotations

from collections.abc import Iterator, MutableMapping
from pathlib import Path

from . import store


class MergedVideos(MutableMapping):
    """Several libraries' videos as one mapping of id to video. A video
    stays in its own library whatever is done through this; a new one goes
    into the first."""

    def __init__(self, libraries: list[dict]) -> None:
        self._libraries = libraries

    def _owner(self, video_id) -> dict | None:
        for data in self._libraries:
            if video_id in data["videos"]:
                return data
        return None

    def __getitem__(self, video_id):
        owner = self._owner(video_id)
        if owner is None:
            raise KeyError(video_id)
        return owner["videos"][video_id]

    def __setitem__(self, video_id, video) -> None:
        owner = self._owner(video_id) or self._libraries[0]
        owner["videos"][video_id] = video

    def __delitem__(self, video_id) -> None:
        owner = self._owner(video_id)
        if owner is None:
            raise KeyError(video_id)
        del owner["videos"][video_id]

    def __iter__(self) -> Iterator:
        for data in self._libraries:
            yield from data["videos"]

    def __len__(self) -> int:
        return sum(len(data["videos"]) for data in self._libraries)


class Shelf:
    def __init__(self, libraries: list[dict]) -> None:
        if not libraries:
            raise ValueError("a shelf needs a library")
        self.libraries = list(libraries)
        self._view: dict | None = None

    @property
    def primary(self) -> dict:
        return self.libraries[0]

    def several(self) -> bool:
        return len(self.libraries) > 1

    def view(self) -> dict:
        """What the window works on as its library."""
        if not self.several():
            return self.primary
        if self._view is None:
            self._view = {
                "settings": self.primary["settings"],
                "videos": MergedVideos(self.libraries),
            }
        return self._view

    def roots(self) -> list[str]:
        return [data["settings"].get("library_root") for data in self.libraries
                if data["settings"].get("library_root")]

    def library_of(self, video_id) -> dict:
        """The library a video is in - the first, for one in none."""
        for data in self.libraries:
            if video_id in data["videos"]:
                return data
        return self.primary

    def library_at(self, root) -> dict | None:
        for data in self.libraries:
            if data["settings"].get("library_root") == root:
                return data
        return None

    def library_holding(self, path) -> dict:
        """The library whose folder `path` is in."""
        path = Path(path)
        for data in self.libraries:
            root = data["settings"].get("library_root")
            if root and (path == Path(root) or path.is_relative_to(root)):
                return data
        return self.primary

    def replace(self, data: dict) -> None:
        """Put a library as rescanned (or reset) in place of its old self."""
        root = data["settings"].get("library_root")
        for i, existing in enumerate(self.libraries):
            if existing["settings"].get("library_root") == root:
                self.libraries[i] = data
                self._view = None
                return
        self.libraries.append(data)
        self._view = None

    def save(self, video_id=None) -> None:
        """Save the library a video is in, or - with none given - them all."""
        targets = [self.library_of(video_id)] if video_id is not None else self.libraries
        for data in targets:
            if data["settings"].get("library_root"):
                store.save_library(data)
