# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Export Audio: chosen songs of a video as FLAC or Opus files, in a
folder outside the library - for a phone.

Says which songs, asks where and in what, then writes them one at a time
on a worker thread with a progress bar and a Cancel. Files already there
are asked about first: replaced, or left and skipped. Nothing is ever
written inside a library (core.audio_export).
"""

from __future__ import annotations

import threading
from pathlib import Path

from PySide6.QtCore import QStandardPaths, Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
)

from mediabrowser.core import artwork, audio_export, store, utils
from mediabrowser.gui import exports
from mediabrowser.gui.worker import run_job

# What was chosen last time: the folder, the format, Opus's bitrate and
# whether each video gets a folder of its own.
FOLDER_SETTING = "audio_export_folder"
FORMAT_SETTING = "audio_export_format"
BITRATE_SETTING = "audio_export_opus_bitrate"
OWN_FOLDER_SETTING = "audio_export_own_folder"

NOTE = (
    "Each song is cut exactly where its chapter starts and ends, tagged with its "
    "title, the video's name as the album and its number, and given the video's "
    "cover. Surround sound is mixed down to stereo. Nothing is ever written inside "
    "a library."
)


def default_folder() -> Path:
    saved = store.load_app_settings().get(FOLDER_SETTING)
    if saved and Path(saved).is_dir():
        return Path(saved)
    music = QStandardPaths.writableLocation(QStandardPaths.MusicLocation)
    if music and Path(music).is_dir():
        return Path(music)
    return Path.home()


class AudioExportDialog(QDialog):
    def __init__(self, parent, video_id: str, video: dict, indices, library_root=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Export Audio")
        self.setModal(True)
        self.resize(600, 520)
        self.video_id = video_id
        self.video = video
        self.indices = sorted(set(indices))
        self._jobs: list = []
        self._cancel = threading.Event()
        self._running = False
        self.written: list[Path] = []
        settings = store.load_app_settings()

        count = len(self.indices)
        heading = QLabel(
            f"{count} song{'s' if count != 1 else ''} from {video['display_name']}"
        )
        heading.setObjectName("sectionCaption")
        heading.setTextFormat(Qt.PlainText)
        heading.setWordWrap(True)
        self.songs = QListWidget()
        self.songs.setSelectionMode(QAbstractItemView.NoSelection)
        self.songs.setMaximumHeight(160)
        for index in self.indices:
            chapter = video["chapters"][index]
            length = utils.format_seconds(chapter["end"] - chapter["start"])
            self.songs.addItem(f"{index + 1}. {utils.chapter_label(index, chapter)}  ({length})")

        self.flac_radio = QRadioButton(audio_export.LABELS[audio_export.FLAC])
        self.opus_radio = QRadioButton(audio_export.LABELS[audio_export.OPUS])
        formats = QButtonGroup(self)
        formats.addButton(self.flac_radio)
        formats.addButton(self.opus_radio)
        chosen = settings.get(FORMAT_SETTING, audio_export.OPUS)
        (self.flac_radio if chosen == audio_export.FLAC else self.opus_radio).setChecked(True)
        self.bitrate = QComboBox()
        for rate in audio_export.OPUS_BITRATES:
            self.bitrate.addItem(f"{rate} kbit/s", rate)
        saved_rate = settings.get(BITRATE_SETTING, audio_export.DEFAULT_OPUS_BITRATE)
        self.bitrate.setCurrentIndex(max(0, self.bitrate.findData(saved_rate)))
        self.opus_radio.toggled.connect(self.bitrate.setEnabled)
        self.bitrate.setEnabled(self.opus_radio.isChecked())
        format_row = QHBoxLayout()
        format_row.setContentsMargins(0, 0, 0, 0)
        format_row.addWidget(self.flac_radio)
        format_row.addSpacing(12)
        format_row.addWidget(self.opus_radio)
        format_row.addWidget(self.bitrate)
        format_row.addStretch(1)

        self.folder = QLineEdit(str(default_folder()))
        browse = QPushButton("Choose…")
        browse.clicked.connect(self._choose_folder)
        folder_row = QHBoxLayout()
        folder_row.setContentsMargins(0, 0, 0, 0)
        folder_row.addWidget(self.folder, 1)
        folder_row.addWidget(browse)
        self.own_folder = QCheckBox("In a folder named after the video")
        self.own_folder.setChecked(bool(settings.get(OWN_FOLDER_SETTING, True)))
        self.artist = QLineEdit(utils.guess_artist(video, library_root))
        self.artist.setPlaceholderText("Optional")
        self.artist.setToolTip("Tagged as the artist and album artist of each song")

        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setSpacing(8)
        form.addRow("Format:", format_row)
        form.addRow("Save to:", folder_row)
        form.addRow("", self.own_folder)
        form.addRow("Artist:", self.artist)

        note = QLabel(NOTE)
        note.setObjectName("hintLabel")
        note.setWordWrap(True)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.hide()
        self.status = QLabel("")
        self.status.setObjectName("hintLabel")
        self.status.setTextFormat(Qt.PlainText)
        self.status.setWordWrap(True)

        self.buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
        self.export_button = self.buttons.addButton("Export", QDialogButtonBox.AcceptRole)
        self.export_button.setObjectName("primaryButton")
        self.buttons.accepted.connect(self.start)
        self.buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)
        layout.addWidget(heading)
        layout.addWidget(self.songs)
        layout.addLayout(form)
        layout.addWidget(note)
        layout.addStretch(1)
        layout.addWidget(self.progress)
        layout.addWidget(self.status)
        layout.addWidget(self.buttons)

    def fmt(self) -> str:
        return audio_export.FLAC if self.flac_radio.isChecked() else audio_export.OPUS

    def _choose_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Save the Songs To", self.folder.text())
        if folder:
            self.folder.setText(folder)

    def plan(self) -> list[audio_export.Song]:
        return audio_export.plan(self.video, self.indices, self.folder.text().strip(),
                                 self.fmt(), self.own_folder.isChecked())

    def start(self) -> None:
        if self._running:
            return
        folder = self.folder.text().strip()
        if not folder:
            self.status.setText("Choose a folder to save the songs to.")
            return
        why = audio_export.refused_folder(folder, exports.library_roots())
        if why:
            QMessageBox.warning(
                self, "Export Audio",
                f"The songs can't be saved there: {why}, which the app never writes to. "
                "Choose a folder outside your libraries.",
            )
            return
        fmt = self.fmt()
        if not audio_export.has_encoder(fmt):
            what = "Opus (libopus)" if fmt == audio_export.OPUS else "FLAC"
            self.status.setText(f"This computer's ffmpeg can't write {what}. Try the other format.")
            return
        songs = self.plan()
        skip = False
        there = audio_export.existing(songs)
        if there:
            box = QMessageBox(self)
            box.setWindowTitle("Export Audio")
            box.setIcon(QMessageBox.Question)
            names = "\n".join(path.name for path in there[:8])
            more = f"\n…and {len(there) - 8} more" if len(there) > 8 else ""
            box.setText(f"{len(there)} of these songs are there already:\n\n{names}{more}")
            replace = box.addButton("Replace Them", QMessageBox.AcceptRole)
            keep = box.addButton("Keep Them, Export the Rest", QMessageBox.ActionRole)
            box.addButton(QMessageBox.Cancel)
            box.exec()
            if box.clickedButton() not in (replace, keep):
                return
            skip = box.clickedButton() is keep
        self._remember(fmt)
        self._run(songs, fmt, skip)

    def _remember(self, fmt: str) -> None:
        settings = store.load_app_settings()
        settings[FOLDER_SETTING] = self.folder.text().strip()
        settings[FORMAT_SETTING] = fmt
        settings[BITRATE_SETTING] = self.bitrate.currentData()
        settings[OWN_FOLDER_SETTING] = self.own_folder.isChecked()
        store.save_app_settings(settings)

    def _run(self, songs, fmt: str, skip: bool) -> None:
        self._running = True
        self._cancel.clear()
        for widget in (self.flac_radio, self.opus_radio, self.bitrate, self.folder,
                       self.own_folder, self.artist, self.export_button):
            widget.setEnabled(False)
        self.progress.setValue(0)
        self.progress.show()
        self.status.setText("Exporting…")
        video = self.video
        tags = audio_export.Tags(album=video["display_name"],
                                 artist=self.artist.text().strip(),
                                 total=len(video["chapters"]))
        cover = artwork.lookup(self.video_id)
        bitrate = self.bitrate.currentData()
        cancel = self._cancel

        def work(progress_cb):
            def report(done, total, fraction):
                progress_cb(f"{int(fraction * 100)}|{min(done + 1, total)}|{total}")

            return audio_export.export_songs(
                video, songs, fmt, tags, cover=cover, bitrate=bitrate,
                skip_existing=skip, cancel=cancel, progress_cb=report,
            )

        self._jobs.append(run_job(
            self, work, wants_progress=True,
            on_progress=self._on_progress, on_done=self._on_done, on_failed=self._on_failed,
        ))

    def _on_progress(self, text: str) -> None:
        percent, song, total = (int(part) for part in text.split("|"))
        self.progress.setValue(percent)
        self.status.setText(f"Exporting song {song} of {total}…")

    def _on_done(self, written) -> None:
        self._running = False
        self.written = list(written)
        self.accept()

    def _on_failed(self, message: str) -> None:
        self._running = False
        self.progress.hide()
        if self._cancel.is_set():
            self.reject()
            return
        self.status.setText(f"The export stopped: {message}")
        for widget in (self.flac_radio, self.opus_radio, self.folder, self.own_folder,
                       self.artist, self.export_button):
            widget.setEnabled(True)
        self.bitrate.setEnabled(self.opus_radio.isChecked())

    def reject(self) -> None:
        if self._running:
            # Stops ffmpeg; the half-written song is removed, the finished
            # ones are kept.
            self._cancel.set()
            self.status.setText("Stopping…")
            return
        super().reject()

    def done(self, result: int) -> None:
        self._cancel.set()
        super().done(result)
