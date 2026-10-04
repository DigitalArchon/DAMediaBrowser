# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""What a change to the libraries does to the work in them, in words.

Libraries that overlap share their videos (see store), so adding,
forgetting, resetting or moving one can reach into another. Before any of
those the person is told plainly what is kept, what is shared and what
would be lost - counted in videos that have had work done on them, since
that's what can't be read back from the files.
"""

from __future__ import annotations

from pathlib import Path

from . import library, playlists, protection, store


def name(root: str) -> str:
    return Path(root).name or root


def owns(root: str) -> str:
    """The folder's name as a possessive: "BABYMETAL's", "MusicVids'"."""
    text = name(root)
    return f"{text}'" if text.endswith(("s", "S")) else f"{text}'s"


def has_work(video: dict) -> bool:
    """Whether anything about the video was given here rather than read
    from its file: chapter names, chapters made or edited, its own name,
    a MusicBrainz release, hidden or marked named."""
    if video.get("chapter_origin") or video.get("custom_name"):
        return True
    if any(video.get(key) for key in ("musicbrainz_release_id", "hidden", "marked_named")):
        return True
    return any(chapter.get("source") in library.KEPT_SOURCES
               for chapter in video.get("chapters") or [])


def _videos(count: int) -> str:
    return f"{count} video{'' if count == 1 else 's'}"


def _worked(videos) -> str:
    videos = list(videos)
    worked = sum(1 for video in videos if has_work(video))
    return f"{_videos(len(videos))}, {worked} with titles or chapters given here"


def adding(root) -> str | None:
    """What adding this folder as a library does with the libraries there
    are, or None when it meets none (or is one already: a rescan)."""
    root = str(Path(root).resolve())
    found = store.overlap(root)
    if found["is_library"]:
        return None
    if found["within"]:
        outer = found["within"]
        videos = store.load_library_for_root(root)["videos"].values()
        text = (
            f"“{name(root)}” is inside “{name(outer)}”, a library you already have. "
            f"It will show {owns(outer)} videos from that folder - {_worked(videos)} - "
            "and share their titles and chapters: naming a video in either names it in "
            "both, and nothing in either is lost."
        )
        outer_data = store.load_library_for_root(outer)
        for flag, word in ((protection.LOCKED, "locked"), (protection.PRIVATE, "private")):
            if protection.library_flag(outer_data, flag):
                text += f"\n\n{name(outer)} is {word}, so these videos are too."
        return text
    if found["contains"]:
        listed = "\n".join(
            f"  • {name(inner)} - {_worked(store.load_library_for_root(inner)['videos'].values())}"
            for inner in found["contains"]
        )
        which = "a library" if len(found["contains"]) == 1 else "libraries"
        return (
            f"“{name(root)}” contains {which} you already have:\n\n{listed}\n\n"
            f"Nothing is lost: their titles and chapters become {owns(root)} too, and from "
            "now on naming a video in one names it in all. They stay in the Libraries "
            "panel with their own hidden folders, playlists and Locked/Private settings - "
            f"which go on covering their videos when they're seen in {name(root)}."
        )
    return None


def forgetting(root) -> str:
    """What forgetting this library loses, and what other libraries keep."""
    root = str(Path(root).resolve())
    plan = store.forget_plan(root)
    data = store.load_library_for_root(root)
    own = []
    if data["settings"].get("hidden_folders"):
        own.append("its hidden folders")
    saved = len(playlists.all_playlists(data))
    if saved:
        own.append(f"its {saved} playlist{'' if saved == 1 else 's'}")
    for flag, word in ((protection.LOCKED, "Locked"), (protection.PRIVATE, "Private")):
        if protection.library_flag(data, flag):
            own.append(f"its {word} setting")
    own_text = (", ".join(own[:-1]) + " and " + own[-1]) if len(own) > 1 else (
        own[0] if own else "")

    lost = plan["lost"]
    lost_worked = sum(1 for video in lost.values() if has_work(video))
    kept = len(plan["videos"]) - len(lost)
    parts = []
    if plan["kept_by"] and kept:
        keepers = " and ".join(name(r) for r in plan["kept_by"])
        parts.append(
            f"{_videos(kept)} stay in {keepers}, titles, chapters and all - add "
            f"{name(root)} again any time and they're all there."
        )
    if lost:
        parts.append(
            f"{'The other ' if parts else ''}{_videos(len(lost))} - {lost_worked} with "
            "titles or chapters given here - are in no other library, and are forgotten."
            if lost_worked else
            f"{'The other ' if parts else ''}{_videos(len(lost))}, none with anything "
            "given here, are forgotten."
        )
    if own_text:
        parts.append(f"Also forgotten: {own_text}.")
    parts.append(
        "Your media files are never touched. A copy of the library as it was is kept: "
        "Restore… in the Libraries panel brings it back."
    )
    return "\n\n".join(parts)


def shared_with(root) -> list[str]:
    """The other libraries that share some of this one's videos."""
    root = str(Path(root).resolve())
    found = store.overlap(root)
    others = list(found["contains"])
    if found["within"]:
        others.insert(0, found["within"])
    return others


def locating(old_root, new_root) -> str:
    """What Locate Moved Folder is about to do, and how sure it is."""
    old_root = str(Path(old_root).resolve())
    new_root = str(Path(new_root).resolve())
    source, target = library.relocation(old_root, new_root)
    data = store.load_library_for_root(source)
    found, looked = library.located(data["videos"].values(), source, target)
    text = f"Move “{name(old_root)}” from\n  {old_root}\nto\n  {new_root}"
    if source != old_root:
        text += (
            f"\n\nIt's inside “{name(source)}”, whose other videos are at the new place "
            f"too - so the whole share moved, and {name(source)} moves with it, to\n  {target}"
        )
    text += (f"\n\n{_worked(data['videos'].values())} - all keep their titles and "
             "chapters.")
    if looked and found < looked:
        text += (
            f"\n\nOf {looked} videos checked, {found} are there. The rest show as "
            "missing until they're found: a rescan picks up files that were renamed or "
            "moved within the folder."
        )
    return text


def backup_title(backup: dict) -> str:
    """One line for a backup in the list: what it holds, and when."""
    import time

    nested = [r for r in backup["libraries"] if r != backup["root"]]
    text = name(backup["root"])
    if nested:
        text += f" (with {', '.join(name(r) for r in nested)})"
    when = time.strftime("%-d %b %Y, %H:%M", time.localtime(backup["when"]))
    return f"{text} - {when}"


def backup_detail(backup: dict) -> str:
    """What a backup holds, why it was made, and what restoring it does."""
    roots = backup["libraries"]
    present = [r for r in roots if store.has_library(r)]
    text = (f"Saved {backup['reason']}.\n\n" if backup["reason"] else "")
    text += (f"{'Library' if len(roots) == 1 else 'Libraries'}: "
             + ", ".join(roots) + f"\n{_worked(backup['videos'].values())}.")
    text += (
        "\n\nRestoring brings back its libraries and videos, and nothing here now is "
        "lost: a video that's still here keeps whichever copy is further along, and the "
        "library it goes into is backed up first."
    )
    if present:
        text += (f"\n\n{', '.join(name(r) for r in present)} "
                 f"{'is' if len(present) == 1 else 'are'} here already, and keep "
                 f"{'its' if len(present) == 1 else 'their'} current settings.")
    return text


def erasing(root) -> str:
    """What Delete Library Data deletes, counted - and what it doesn't."""
    root = str(Path(root).resolve())
    plan = store.erase_plan(root)
    videos = plan["videos"].values()
    data = store.load_library_for_root(root)
    saved = len(playlists.all_playlists(data))
    lines = [
        f"  • {_worked(videos)}",
        "  • every title, chapter and name given to them, their MusicBrainz matches, "
        "hidden and Locked/Private settings",
        f"  • {name(root)}'s own settings, hidden folders and Identify history"
        + (f", and its {saved} playlist{'' if saved == 1 else 's'}" if saved else ""),
    ]
    inside = [r for r in plan["libraries"] if r != root]
    if inside:
        lines.append(f"  • the {'library' if len(inside) == 1 else 'libraries'} inside it: "
                     + ", ".join(name(r) for r in inside))
    if plan["backups"]:
        lines.append(f"  • its part of {plan['backups']} backup"
                     f"{'' if plan['backups'] == 1 else 's'} - so Restore can't bring it back")
    lines.append("  • the cached cover art for its videos")
    text = ("This permanently deletes, from this app's database:\n\n" + "\n".join(lines)
            + "\n\nIt can't be undone. Adding the folder again starts it from scratch: every "
            "title and chapter has to be found or named again.")
    if plan["shared_with"]:
        outer = plan["shared_with"]
        text += (
            f"\n\n{name(root)} is inside {name(outer)}, which shares these videos - so "
            f"they're deleted from {name(outer)} too. {name(outer)} shows them again, "
            "untitled, when it's next rescanned. The rest of "
            f"{owns(outer)} videos aren't touched."
        )
    text += ("\n\nOnly this app's records go: the folder and every video in it are "
             "never touched.")
    return text


def everything() -> str:
    """What Delete All Library Data deletes, counted."""
    libraries = store.list_libraries()
    videos: dict = {}
    saved = 0
    for lib in libraries:
        data = store.load_library_for_root(lib["root"])
        videos.update(data["videos"])
        saved += len(playlists.all_playlists(data))
    backups = store.list_backups()
    worked = sum(1 for video in videos.values() if has_work(video))
    lines = [
        f"  • {len(libraries)} librar{'y' if len(libraries) == 1 else 'ies'} and "
        f"{_videos(len(videos))}",
        f"  • every title, chapter and name given here - {worked} video"
        f"{'' if worked == 1 else 's'} have some",
        "  • hidden folders, Locked and Private settings, MusicBrainz matches and "
        "Identify history",
    ]
    if saved:
        lines.append(f"  • {saved} playlist{'' if saved == 1 else 's'}")
    lines.append(f"  • all {len(backups)} backup{'' if len(backups) == 1 else 's'} - "
                 "so nothing can be restored afterwards"
                 if backups else "  • (there are no backups to restore from)")
    lines.append("  • the cached cover art")
    return (
        "This permanently deletes, from this app's database:\n\n" + "\n".join(lines)
        + "\n\nIt can't be undone. Adding a folder again starts it from scratch: every "
        "title and chapter has to be found or named again.\n\n"
        "Your video files are never touched, and your Settings (privacy, the AI and its "
        "key) are kept."
    )
