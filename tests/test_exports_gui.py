# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""The window's part in exporting chapters and songs, and a DVD on the
shelf like any other video."""

import shutil

import pytest

from mediabrowser.core import chapter_export, library, store
from tests import dvd_builder


def _video(folder, name, *titles):
    path = folder / f"{name}.mkv"
    path.write_bytes(b"x")
    starts = [i * 100.0 for i in range(len(titles))]
    return {
        "type": "file", "path": str(path), "display_name": name,
        "duration": 100.0 * len(titles),
        "chapters": [{"start": s, "end": s + 100.0, "title": t, "source": "manual"}
                     for s, t in zip(starts, titles, strict=True)],
    }


@pytest.fixture
def shelf(window, tmp_path):
    media = tmp_path / "media"
    media.mkdir()
    window.data = store.default_library(str(media))
    window.data["videos"] = {
        "a": _video(media, "Glass Harbor - Copperfield Hall", "Paper Lanterns", "Northbound"),
    }
    window.refresh_library()
    return window


def _labels(menu):
    return [a.text() for a in menu.actions()]


def test_a_videos_menu_offers_both_exports(shelf):
    item = next(shelf.grid.item(i) for i in range(shelf.grid.count()))
    labels = _labels(shelf.grid.context_menu_for(item))
    assert "Export Chapters…" in labels and "Export Songs as Audio…" in labels


def test_a_chapters_menu_exports_the_selection_as_audio(shelf, monkeypatch):
    asked = []
    monkeypatch.setattr(shelf, "export_audio",
                        lambda video_id=None, indices=None: asked.append(indices))
    shelf.open_video("a")
    tree = shelf.detail.tree
    tree.topLevelItem(0).setSelected(True)
    tree.topLevelItem(1).setSelected(True)
    menu = shelf.detail.context_menu_for(tree.topLevelItem(1))
    action = next(a for a in menu.actions() if a.text() == "Export 2 Songs as Audio…")
    action.trigger()
    assert asked == [[0, 1]]


def test_export_chapters_saves_where_the_dialog_says(shelf, tmp_path, monkeypatch):
    from mediabrowser.gui import exports

    target = tmp_path / "out" / "chapters"
    target.parent.mkdir()
    cue = exports._filter(chapter_export.CUE)
    monkeypatch.setattr(exports.QFileDialog, "getSaveFileName",
                        lambda *a, **k: (str(target), cue))
    shelf.export_chapters("a")
    written = target.with_suffix(".cue")
    assert 'TITLE "Northbound"' in written.read_text(encoding="utf-8")
    assert "saved as CUE sheet" in shelf.status_label.text()
    # And the next starts in the same folder, in the same format.
    seen = {}

    def ask(parent, caption, default, filters, chosen):
        seen.update(default=default, chosen=chosen)
        return "", ""

    monkeypatch.setattr(exports.QFileDialog, "getSaveFileName", ask)
    shelf.export_chapters("a")
    assert seen["default"].startswith(str(target.parent)) and seen["chosen"] == cue


def test_export_chapters_refuses_a_library_folder(shelf, monkeypatch):
    from mediabrowser.gui import exports

    store.save_library(shelf.data)
    root = shelf.data["settings"]["library_root"]
    warned = []
    monkeypatch.setattr(exports.QFileDialog, "getSaveFileName",
                        lambda *a, **k: (f"{root}/c.xml", exports._filter("mkvmerge")))
    monkeypatch.setattr(exports.QMessageBox, "warning", lambda *a: warned.append(a[2]))
    shelf.export_chapters("a")
    assert warned and "never writes to" in warned[0]


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
def test_a_dvd_is_on_the_shelf_and_opens_like_any_video(window, tmp_path):
    from mediabrowser.gui.dialogs.chapters_dialog import MENU_TAB_DVD, TAB_MENU, ChaptersDialog

    media = tmp_path / "Concerts"
    dvd_builder.build(media / "COPPERFIELD_HALL", titles=[(0.0, 8.0, 16.0)], seconds=24.0)
    window.data = library.rescan(media)
    window.refresh_library()
    (video_id, video), = window.data["videos"].items()
    window.open_video(video_id)
    assert "DVD title" in window.detail.meta.text()
    assert window.detail.tree.topLevelItemCount() == 3
    dialog = ChaptersDialog(window, video, str(media))
    assert dialog.tabs.tabText(TAB_MENU) == MENU_TAB_DVD
    assert not dialog.tabs.isTabEnabled(TAB_MENU)
