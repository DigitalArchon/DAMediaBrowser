# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Saving a video's chapters as a file for another tool.

The format follows the file type picked in the save dialog; where the
last one was saved is remembered, so a run of videos goes to one folder.
Nothing is ever saved into a library (core.chapter_export says why).
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import QFileDialog, QMessageBox

from mediabrowser.core import chapter_export, store

# Where exports went last, so the next starts there. An app setting.
EXPORT_FOLDER_SETTING = "export_folder"
# Which chapter format was picked last.
CHAPTER_FORMAT_SETTING = "chapter_export_format"


def last_folder() -> Path:
    saved = store.load_app_settings().get(EXPORT_FOLDER_SETTING)
    if saved and Path(saved).is_dir():
        return Path(saved)
    return Path.home()


def remember_folder(folder) -> None:
    settings = store.load_app_settings()
    settings[EXPORT_FOLDER_SETTING] = str(folder)
    store.save_app_settings(settings)


def _filter(fmt: str) -> str:
    suffix = chapter_export.SUFFIXES[fmt]
    return f"{chapter_export.LABELS[fmt]} (*{suffix})"


def library_roots() -> list[str]:
    return [known["root"] for known in store.list_libraries()]


def export_chapters(parent, video_id: str, video: dict) -> str | None:
    """Ask where, and save the video's chapters there. Returns what to say
    in the status bar, or None if nothing was saved."""
    settings = store.load_app_settings()
    fmt = settings.get(CHAPTER_FORMAT_SETTING)
    if fmt not in chapter_export.FORMATS:
        fmt = chapter_export.MKVMERGE
    filters = [_filter(f) for f in chapter_export.FORMATS]
    default = last_folder() / chapter_export.default_name(video, fmt)
    path, chosen = QFileDialog.getSaveFileName(
        parent, "Export Chapters", str(default), ";;".join(filters), _filter(fmt)
    )
    if not path:
        return None
    fmt = chapter_export.FORMATS[filters.index(chosen)] if chosen in filters else (
        chapter_export.format_for(path) or fmt
    )
    if chapter_export.format_for(path) != fmt:
        path += chapter_export.SUFFIXES[fmt]
    try:
        saved = chapter_export.export(video, fmt, path, library_roots(), video_id)
    except chapter_export.ExportError as exc:
        QMessageBox.warning(parent, "Export Chapters", str(exc))
        return None
    except OSError as exc:
        QMessageBox.critical(parent, "Export failed", f"Couldn't write the chapters: {exc}")
        return None
    settings = store.load_app_settings()
    settings[CHAPTER_FORMAT_SETTING] = fmt
    settings[EXPORT_FOLDER_SETTING] = str(saved.parent)
    store.save_app_settings(settings)
    return (f"{len(video['chapters'])} chapter(s) saved as "
            f"{chapter_export.LABELS[fmt]} to {saved}.")
