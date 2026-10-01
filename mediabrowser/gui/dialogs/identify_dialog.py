# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Identify Library: name everything that needs it, unattended.

The choices are what matter here - which methods may run, whether the AI
is a last resort or used first, how many AI requests the run may make,
whether its guesses go in - so they come first, with what each costs. Then
the run itself: it works in the background, one video at a time, and each
video's result is applied as it arrives (by the main window, on the GUI
thread), so the shelf fills in while it goes and stopping keeps what's
done. The dialog isn't modal: the app stays usable throughout.

The engine is core.autoname; this is its controls and its log.
"""

from __future__ import annotations

import threading

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from mediabrowser.core import ai, autoname, naming, store
from mediabrowser.gui import identified

SETTINGS_KEY = "identify_options"

METHOD_TEXT = {
    autoname.MUSICBRAINZ: (
        "MusicBrainz tracklists",
        "Free. Used only when a release's tracks add up to the video, so each song "
        "lands where it plays. Slow: MusicBrainz allows a request every second or two.",
    ),
    autoname.AUDIO: (
        "Split videos in one piece where the music stops",
        "Free, but slow: it reads the whole video - a couple of minutes a concert "
        "over a network share. The chapters it makes still need names.",
    ),
    autoname.MENU: (
        "Read Blu-ray menus (AI)",
        "The most accurate there is: each song named as the disc's own menu names "
        "it. A cent or two per disc.",
    ),
    autoname.AI_LOOK: (
        "AI looks at the video and searches the web",
        "For what nothing else could name. A few cents a video. It also puts right a "
        "MusicBrainz search the file's name got wrong, and says which release is the show.",
    ),
    autoname.TRANSLATE: (
        "Romanise titles (AI)",
        "Japanese and other titles as the songs are known in English-language "
        "releases - romanised (Iine!, not \"So Good\") or the official English title - "
        "the original kept for searching. A fraction of a cent.",
    ),
}

# What an AI request costs, very roughly, in US cents - for the estimate.
AI_CENTS = {autoname.MENU: 2, autoname.AI_LOOK: 6, autoname.TRANSLATE: 1}

# Log colours by how a video came out.
OUTCOME_COLOURS = {
    "done": identified.COLOURS[naming.NAMED],
    "better": identified.COLOURS[naming.PARTLY],
    "same": "#8a91a0",
    "error": identified.COLOURS[naming.UNNAMED],
}


def saved_options(ai_configured: bool) -> autoname.Options:
    """The choices last made here, for identifying a single video from its
    right-click menu the same way - without the AI when it isn't set up."""
    saved = store.load_app_settings().get(SETTINGS_KEY) or {}
    methods = set(saved.get("methods", autoname.FREE_METHODS))
    if not ai_configured:
        methods -= set(autoname.AI_METHODS)
    return autoname.Options(
        methods=methods,
        ai_policy=saved.get("ai_policy", autoname.LAST_RESORT),
        ai_budget=int(saved.get("ai_budget", 40)),
        only_sure=bool(saved.get("only_sure", True)),
        translate=autoname.TRANSLATE in methods,
    )


class _Runner(QObject):
    """Runs autoname.run on a worker thread, reporting over signals - the
    outcomes are objects, which run_job's text progress can't carry.

    The thread is a plain daemon thread rather than a QThread: a run can be
    in the middle of a minute-long AI request when the app quits, and a
    QThread destroyed while running takes the whole process down. The
    runner itself lives on the GUI thread, so its signals arrive there."""

    started_video = Signal(int, int, str)
    outcome = Signal(object)
    finished = Signal(object)  # the budget as left
    failed = Signal(str)

    def __init__(self, videos, options, settings, root, cancel, private=()) -> None:
        super().__init__()
        self._args = (videos, options, settings, root, cancel, frozenset(private))

    def work(self) -> None:
        videos, options, settings, root, cancel, private = self._args
        try:
            budget = autoname.run(
                videos, options, settings,
                autoname.Services(store.load_app_settings().get("musicbrainz_format", "xml")),
                library_root=root, cancel=cancel,
                on_started=lambda n, total, _vid, name: self.started_video.emit(n, total, name),
                on_outcome=self.outcome.emit,
                private=private,
            )
        except Exception as exc:
            self.failed.emit(str(exc) or repr(exc))
            return
        self.finished.emit(budget)


class IdentifyDialog(QDialog):
    """`window` applies each outcome (window.apply_identified) and says
    which videos are hidden or missing (window.identify_skips)."""

    def __init__(self, window) -> None:
        super().__init__(window)
        self.setWindowTitle("Identify Library")
        self.setModal(False)
        self.resize(760, 760)
        self.main = window
        self._thread: threading.Thread | None = None
        self._runner: _Runner | None = None
        self._cancel = threading.Event()
        self._counts = {"done": 0, "better": 0, "same": 0, "error": 0}
        self._ai_used = 0
        self._ai_settings = ai.settings_from(store.load_app_settings())
        self._build()
        self._load_options()
        self._update_estimate()

    # --- construction ----------------------------------------------------

    def _build(self) -> None:
        intro = QLabel(
            "Names every video that still needs it, one at a time, while you get on "
            "with something else. It only fills in what's missing - a chapter with a "
            "real name keeps it - and the whole run can be undone afterwards."
        )
        intro.setObjectName("hintLabel")
        intro.setWordWrap(True)

        methods_caption = QLabel("Methods")
        methods_caption.setObjectName("sectionCaption")
        self.method_checks: dict[str, QCheckBox] = {}
        methods = QGridLayout()
        methods.setContentsMargins(0, 0, 0, 0)
        methods.setHorizontalSpacing(12)
        methods.setVerticalSpacing(2)
        for row, method in enumerate(autoname.METHODS):
            label, hint = METHOD_TEXT[method]
            check = QCheckBox(label)
            check.setToolTip(hint)
            check.toggled.connect(lambda _on: self._update_estimate())
            detail = QLabel(hint)
            detail.setObjectName("hintLabel")
            detail.setWordWrap(True)
            self.method_checks[method] = check
            methods.addWidget(check, 2 * row, 0)
            methods.addWidget(detail, 2 * row + 1, 0)

        ai_caption = QLabel("The AI")
        ai_caption.setObjectName("sectionCaption")
        self.last_resort = QRadioButton("Only as a last resort: free methods first")
        self.accurate_first = QRadioButton(
            "Most accurate first: read Blu-ray menus before trying MusicBrainz"
        )
        policy = QButtonGroup(self)
        policy.addButton(self.last_resort)
        policy.addButton(self.accurate_first)
        self.budget = QSpinBox()
        self.budget.setRange(1, 1000)
        self.budget.setSuffix(" AI requests")
        self.budget.valueChanged.connect(lambda _v: self._update_estimate())
        budget_row = QHBoxLayout()
        budget_row.setContentsMargins(0, 0, 0, 0)
        budget_row.addWidget(QLabel("Stop using the AI after"))
        budget_row.addWidget(self.budget)
        budget_row.addStretch(1)
        self.only_sure = QCheckBox("Leave out names the AI marks as guesses")
        self.only_sure.setToolTip(
            "A chapter it could only guess at stays unnamed, for you to name - rather "
            "than getting a name that may be wrong."
        )

        scope_caption = QLabel("Which videos")
        scope_caption.setObjectName("sectionCaption")
        self.include_partly = QCheckBox("Include videos with some chapters already named")
        self.include_partly.toggled.connect(lambda _on: self._update_estimate())
        self.include_unverified = QCheckBox(
            "Check videos named only after their file (single songs, clips)"
        )
        self.include_unverified.setToolTip(
            "A short video in one piece is named after its file to begin with. This "
            "checks the name - a free MusicBrainz look-up first, then the AI if allowed."
        )
        self.include_unverified.toggled.connect(lambda _on: self._update_estimate())
        self.include_hidden = QCheckBox("Include hidden videos")
        self.include_hidden.toggled.connect(lambda _on: self._update_estimate())

        self.estimate = QLabel()
        self.estimate.setObjectName("hintLabel")
        self.estimate.setWordWrap(True)
        self.estimate.setTextFormat(Qt.PlainText)

        self.options_panel = QWidget()
        panel = QVBoxLayout(self.options_panel)
        panel.setContentsMargins(0, 0, 0, 0)
        panel.setSpacing(6)
        for widget in (methods_caption,):
            panel.addWidget(widget)
        panel.addLayout(methods)
        panel.addWidget(ai_caption)
        panel.addWidget(self.last_resort)
        panel.addWidget(self.accurate_first)
        panel.addLayout(budget_row)
        panel.addWidget(self.only_sure)
        panel.addWidget(scope_caption)
        panel.addWidget(self.include_partly)
        panel.addWidget(self.include_unverified)
        panel.addWidget(self.include_hidden)

        # --- the run
        self.progress = QProgressBar()
        self.progress.setTextVisible(True)
        self.progress.hide()
        self.current = QLabel("")
        self.current.setObjectName("hintLabel")
        self.current.setTextFormat(Qt.PlainText)
        self.log = QListWidget()
        self.log.setWordWrap(True)
        self.log.hide()
        self.summary = QLabel("")
        self.summary.setTextFormat(Qt.PlainText)
        self.summary.setWordWrap(True)

        self.start_button = QPushButton("Start")
        self.start_button.setObjectName("primaryButton")
        self.start_button.clicked.connect(self.start)
        self.stop_button = QPushButton("Stop")
        self.stop_button.clicked.connect(self.stop)
        self.stop_button.hide()
        self.undo_button = QPushButton("Undo This Run")
        self.undo_button.setToolTip("Put back every video this run changed, as it was")
        self.undo_button.clicked.connect(self.undo)
        self.undo_button.hide()
        close = QPushButton("Close")
        close.clicked.connect(self.close)
        buttons = QHBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.addWidget(self.undo_button)
        buttons.addStretch(1)
        buttons.addWidget(self.start_button)
        buttons.addWidget(self.stop_button)
        buttons.addWidget(close)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)
        layout.addWidget(intro)
        layout.addWidget(self.options_panel)
        layout.addWidget(self.estimate)
        layout.addWidget(self.progress)
        layout.addWidget(self.current)
        layout.addWidget(self.log, 1)
        layout.addWidget(self.summary)
        layout.addLayout(buttons)

    # --- options -----------------------------------------------------------

    def options(self) -> autoname.Options:
        return autoname.Options(
            methods={m for m, check in self.method_checks.items()
                     if check.isChecked() and check.isEnabled()},
            ai_policy=(autoname.ACCURATE_FIRST if self.accurate_first.isChecked()
                       else autoname.LAST_RESORT),
            ai_budget=self.budget.value(),
            only_sure=self.only_sure.isChecked(),
            translate=self.method_checks[autoname.TRANSLATE].isChecked(),
            include_partly=self.include_partly.isChecked(),
            include_unverified=self.include_unverified.isChecked(),
        )

    def _load_options(self) -> None:
        saved = store.load_app_settings().get(SETTINGS_KEY) or {}
        chosen = set(saved.get("methods", autoname.FREE_METHODS))
        configured = ai.is_configured(self._ai_settings)
        for method, check in self.method_checks.items():
            check.setChecked(method in chosen)
            if method in autoname.AI_METHODS and not configured:
                check.setEnabled(False)
                check.setToolTip("Set a Nano-GPT API key in AI Settings first")
        (self.accurate_first if saved.get("ai_policy") == autoname.ACCURATE_FIRST
         else self.last_resort).setChecked(True)
        self.budget.setValue(int(saved.get("ai_budget", 40)))
        self.only_sure.setChecked(bool(saved.get("only_sure", True)))
        self.include_partly.setChecked(bool(saved.get("include_partly", True)))
        self.include_unverified.setChecked(bool(saved.get("include_unverified", True)))
        self.include_hidden.setChecked(bool(saved.get("include_hidden", False)))

    def _save_options(self) -> None:
        options = self.options()
        settings = store.load_app_settings()
        settings[SETTINGS_KEY] = {
            "methods": sorted(options.methods),
            "ai_policy": options.ai_policy,
            "ai_budget": options.ai_budget,
            "only_sure": options.only_sure,
            "include_partly": options.include_partly,
            "include_unverified": options.include_unverified,
            "include_hidden": self.include_hidden.isChecked(),
        }
        store.save_app_settings(settings)

    def videos(self) -> list[tuple[str, dict]]:
        skip = self.main.identify_skips(include_hidden=self.include_hidden.isChecked())
        return autoname.candidates(self.main.shelf_videos(), self.options(), skip)

    def _update_estimate(self) -> None:
        options = self.options()
        videos = self.videos()
        discs = sum(1 for _, v in videos if v["type"] == "bluray")
        text = (f"{len(videos)} video(s) to identify"
                + (f", {discs} of them Blu-ray titles." if discs else "."))
        ai_methods = options.methods & set(autoname.AI_METHODS)
        if not options.methods:
            text += " Choose at least one method."
        elif ai_methods:
            most = min(options.ai_budget,
                       len(videos) * sum(autoname.AI_REQUESTS[m] for m in ai_methods))
            worst = max(AI_CENTS[m] for m in ai_methods)
            model = self._ai_settings[ai.SETTING_MODEL]
            priced = (f"with {ai.short_model_name(model)}" if model == ai.DEFAULT_MODEL else
                      f"at Claude Sonnet's prices ({ai.short_model_name(model)}, set in AI "
                      "Settings, may cost more or less)")
            text += (f" At most {most} AI request(s), paid for from your Nano-GPT credit - "
                     f"under ${most * worst / 100:.2f} {priced}, usually much less.")
        else:
            text += " No AI: this run costs nothing."
        self.estimate.setText(text)
        self.start_button.setEnabled(bool(videos) and bool(options.methods)
                                     and self._thread is None)

    # --- the run -----------------------------------------------------------

    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self._thread is not None:
            return
        if self.options_panel.isHidden():
            # After a run: back to the choices, for another.
            self._show_options(True)
            return
        videos = self.videos()
        options = self.options()
        if not videos or not options.methods:
            return
        self._save_options()
        self.main.begin_identify_run()
        self._cancel = threading.Event()
        self._counts = dict.fromkeys(self._counts, 0)
        self._ai_used = 0
        # The log needs the room while a run goes; the choices fold away.
        self._show_options(False)
        self.start_button.hide()
        self.stop_button.show()
        self.undo_button.hide()
        self.log.clear()
        self.log.show()
        self.summary.setText("")
        self.progress.setRange(0, len(videos))
        self.progress.setValue(0)
        self.progress.setFormat("%v of %m")
        self.progress.show()

        self._runner = _Runner(videos, options, self._ai_settings,
                               self.main._root_of, self._cancel,
                               private=self.main.private_ids())
        self._runner.started_video.connect(self._on_started)
        self._runner.outcome.connect(self._on_outcome)
        self._runner.finished.connect(self._on_finished)
        self._runner.failed.connect(self._on_failed)
        self._thread = threading.Thread(target=self._runner.work, daemon=True,
                                        name="identify-library")
        self._thread.start()

    def stop(self) -> None:
        self._cancel.set()
        self.stop_button.setEnabled(False)
        self.current.setText("Stopping after this step…")

    def _on_started(self, n: int, total: int, name: str) -> None:
        self.progress.setValue(n)
        self.current.setText(f"Identifying {name}…")
        self.main.show_identify_progress(n, total, name)

    def _on_outcome(self, outcome: autoname.Outcome) -> None:
        self._ai_used += outcome.ai_used
        if outcome.error:
            kind = "error"
        elif outcome.after.state == naming.NAMED and outcome.change is not None:
            kind = "done"
        elif outcome.change is not None:
            kind = "better"
        else:
            kind = "same"
        self._counts[kind] += 1
        if outcome.change is not None:
            self.main.apply_identified(outcome.video_id, outcome.change)
        head = {
            "done": f"✓ {outcome.name} — {outcome.after.describe().lower()}",
            "better": f"◐ {outcome.name} — {outcome.before.describe().lower()} → "
                      f"{outcome.after.describe().lower()}",
            "same": f"· {outcome.name} — unchanged",
            "error": f"✕ {outcome.name} — {outcome.error}",
        }[kind]
        item = QListWidgetItem("\n".join([head, *[f"    {line}" for line in outcome.log]]))
        item.setForeground(QColor(OUTCOME_COLOURS[kind]))
        self.log.addItem(item)
        self.log.scrollToBottom()
        self.progress.setValue(self.progress.value() + 1)

    def _finish(self, text: str) -> None:
        self._thread = None
        self._runner = None
        self.progress.hide()
        self.current.setText("")
        self.stop_button.hide()
        self.stop_button.setEnabled(True)
        self.start_button.setText("Start Another Run")
        self.start_button.setEnabled(True)
        self.start_button.show()
        self.undo_button.setVisible(self.main.can_undo_identify())
        self.summary.setText(text)
        self.main.end_identify_run(text)

    def _on_finished(self, budget) -> None:
        c = self._counts
        text = (
            f"{'Stopped' if self._cancel.is_set() else 'Done'}: {c['done']} identified, "
            f"{c['better']} improved, {c['same']} unchanged"
            + (f", {c['error']} failed" if c["error"] else "")
            + (f". {self._ai_used} AI request(s) used." if self._ai_used else ". No AI used.")
        )
        self._finish(text)

    def _on_failed(self, message: str) -> None:
        self._finish(f"The run stopped: {message}")

    def _show_options(self, show: bool) -> None:
        self.options_panel.setVisible(show)
        self.estimate.setVisible(show)
        self.log.setVisible(not show)
        if show:
            self.summary.setText("")
            self.undo_button.hide()
            self.start_button.setText("Start")
            self._update_estimate()

    def undo(self) -> None:
        count = self.main.undo_identify()
        self.undo_button.hide()
        self.summary.setText(f"Undone: {count} video(s) put back as they were.")

    def closeEvent(self, event) -> None:
        # Closing hides it; a run carries on and the status bar follows it.
        # Reopening Identify Library brings it back.
        event.accept()
