# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Where a video sits on disk, for showing it in a file manager and for
hiding whole folders of it from the shelf.

A video file lives in a folder. A Blu-ray title is different: its "file" is
the disc folder itself, so that folder is the thing to hide (hiding the
folder it sits in would take every other disc next to it along too), and
it is what a file manager should highlight.

Hidden folders are stored per library, as absolute paths, in the library's
settings. Single videos can be hidden too - one Blu-ray title out of a
disc's several extras, say, where hiding the disc's folder would take the
concert with it - by a flag on the video itself. Hiding is only about what
the shelf shows: nothing is deleted, the names stay stored, and rescans
still read everything.
"""

from pathlib import PurePath

HIDDEN_SETTING = "hidden_folders"
# Set on a video hidden by itself rather than by its folder.
VIDEO_HIDDEN_KEY = "hidden"


def own_folder(video) -> PurePath:
    """The folder that "hide this folder" means for this video."""
    path = PurePath(video["path"])
    return path if video["type"] == "bluray" else path.parent


def reveal_target(video) -> tuple[PurePath, PurePath]:
    """(folder to open, item in it to highlight) for a file manager."""
    path = PurePath(video["path"])
    return path.parent, path


def folder_chain(video, root) -> list[PurePath]:
    """The folders a video could be hidden by, nearest first, stopping short
    of the library's own root - hiding that would hide everything.
    """
    root = PurePath(root) if root else None
    chain = []
    folder = own_folder(video)
    while root is not None and folder != root and folder.is_relative_to(root):
        chain.append(folder)
        folder = folder.parent
    return chain


def hidden_folders(data) -> list[str]:
    return list(data["settings"].get(HIDDEN_SETTING, []))


def hiding(video, hidden) -> list[str]:
    """The hidden folders that contain this video, if any."""
    folder = own_folder(video)
    return [h for h in hidden if folder == PurePath(h) or folder.is_relative_to(h)]


def is_hidden(video, hidden) -> bool:
    """Hidden by itself, or by any hidden folder it is in."""
    return bool(video.get(VIDEO_HIDDEN_KEY)) or bool(hiding(video, hidden))


def hide_video(video) -> None:
    video[VIDEO_HIDDEN_KEY] = True


def unhide_video(video) -> None:
    video.pop(VIDEO_HIDDEN_KEY, None)


def hide(data, folder) -> None:
    hidden = hidden_folders(data)
    folder = str(folder)
    if folder not in hidden:
        hidden.append(folder)
        hidden.sort()
    data["settings"][HIDDEN_SETTING] = hidden


def unhide(data, folder) -> None:
    folder = str(folder)
    data["settings"][HIDDEN_SETTING] = [h for h in hidden_folders(data) if h != folder]


def relative_name(folder, root) -> str:
    """A folder as it reads under the library root: "Extras/Trailers"."""
    folder, root = PurePath(folder), PurePath(root) if root else None
    if root is not None and folder.is_relative_to(root) and folder != root:
        return str(folder.relative_to(root))
    return folder.name or str(folder)
