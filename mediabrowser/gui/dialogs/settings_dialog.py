# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Settings: what may go out, and the AI it goes to.

Privacy comes first: a tick box for each place the app can send anything
(see core.privacy), all unticked until the person ticks them.

The AI tab is where the Nano-GPT key and model are set. The key is pasted
once and kept in the OS keyring; the model can be typed or picked from the
ones Nano-GPT lists with vision (the Claude ones first). Test sends the
smallest possible request so a wrong key or an empty balance is found
here, not after forty frames have been sent.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from mediabrowser.core import ai, creds, privacy, setlistfm, store
from mediabrowser.gui.worker import run_job

OTHER_ENDPOINTS = (
    "Any OpenAI-compatible service and any model that can look at pictures can be "
    "used instead - set the endpoint and the model. But web search is Nano-GPT's "
    "own, so elsewhere it's switched off (or works differently, and may fail), and "
    "another model may not read menus and video frames as well as Claude Sonnet 5, "
    "which is what this app was tested with."
)
ABOUT = (
    "Nano-GPT (nano-gpt.com) sells access to Claude and other models by the "
    "request, with no subscription. Make an account, add a little credit, and "
    "paste an API key from its settings. A concert's chapters cost a few cents "
    "to name with Sonnet; Opus is several times that and a little more careful."
)
PRIVACY_INTRO = (
    "Nothing about your videos leaves this computer unless it's ticked here. "
    "Everything else - scanning, playing, measuring the audio, reading a disc's "
    "own chapters - works without any of it."
)
PRIVATE_NOTE = (
    "A video or library marked Private (right-click it) is never sent anywhere, "
    "whatever is ticked here."
)
AI_OFF_HERE = "Sending to the AI is switched off on the Privacy tab."
KEY_NOTE = (
    "The key is kept in the system keyring (GNOME Keyring or KWallet), not in "
    f"a file. Setting {ai.API_KEY_ENV} in the environment overrides it."
)


SETLISTFM_ABOUT = (
    "setlist.fm lists what was played at a show - bootlegs, broadcasts and festival "
    "sets that were never released, so MusicBrainz doesn't have them. Its API is "
    "free for non-commercial use: sign in at setlist.fm and apply for a key at "
    f'<a href="{setlistfm.KEY_PAGE}">{setlistfm.KEY_PAGE}</a>, then paste it here. '
    "Looking a show up costs nothing, and the AI isn't involved."
)
SETLISTFM_OFF_HERE = privacy.OFF[privacy.SETLISTFM]

TAB_PRIVACY = 0
TAB_AI = 1
TAB_SETLISTFM = 2


class SettingsDialog(QDialog):
    def __init__(self, parent, tab: int = TAB_PRIVACY) -> None:
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setModal(True)
        self.setMinimumWidth(620)
        # Room for the wrapped notes: a form layout doesn't grow for them.
        self.setMinimumHeight(640)
        self._jobs: list = []
        self._app_settings = store.load_app_settings()
        settings = ai.settings_from(self._app_settings)
        self._stored_key = ai.stored_key()
        self._stored_setlist_key = setlistfm.stored_key()

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_privacy_tab(), "Privacy")
        self.tabs.addTab(self._build_ai_tab(settings), "AI")
        self.tabs.addTab(self._build_setlist_tab(), "setlist.fm")
        self.tabs.setCurrentIndex(tab)

        buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
        save = buttons.addButton("Save", QDialogButtonBox.AcceptRole)
        save.setObjectName("primaryButton")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)
        layout.addWidget(self.tabs, 1)
        layout.addWidget(buttons)
        self._describe_model(self.model.currentText())
        self._update_search()
        self._update_privacy()

    # --- the tabs ----------------------------------------------------------

    def _build_privacy_tab(self) -> QWidget:
        intro = QLabel(PRIVACY_INTRO)
        intro.setWordWrap(True)
        layout = QVBoxLayout()
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(4)
        layout.addWidget(intro)
        layout.addSpacing(8)
        self.privacy_checks: dict[str, QCheckBox] = {}
        for choice in privacy.CHOICES:
            label, sends = privacy.TEXT[choice]
            check = QCheckBox(label)
            check.setChecked(privacy.allowed(choice, self._app_settings))
            note = QLabel(sends)
            note.setObjectName("hintLabel")
            note.setWordWrap(True)
            # Under the box's text, not its tick.
            indent = 26 if choice != privacy.WEB_SEARCH else 52
            note.setContentsMargins(indent, 0, 0, 0)
            if choice == privacy.WEB_SEARCH:
                # Only the AI searches, so it's the AI's to allow first.
                check.setStyleSheet("margin-left: 26px;")
            self.privacy_checks[choice] = check
            layout.addWidget(check)
            layout.addWidget(note)
            layout.addSpacing(8)
        self.privacy_checks[privacy.AI].toggled.connect(lambda _on: self._update_privacy())
        private = QLabel(PRIVATE_NOTE)
        private.setObjectName("hintLabel")
        private.setWordWrap(True)
        layout.addWidget(private)
        layout.addStretch(1)
        tab = QWidget()
        tab.setLayout(layout)
        return tab

    def _update_privacy(self) -> None:
        self.privacy_checks[privacy.WEB_SEARCH].setEnabled(
            self.privacy_checks[privacy.AI].isChecked()
        )

    def _build_ai_tab(self, settings: dict) -> QWidget:
        about = QLabel(ABOUT)
        about.setObjectName("hintLabel")
        about.setWordWrap(True)
        about.setOpenExternalLinks(True)

        self.key = QLineEdit(self._stored_key)
        self.key.setEchoMode(QLineEdit.Password)
        self.key.setPlaceholderText("Paste the API key from nano-gpt.com…")
        self.show_key = QCheckBox("Show")
        self.show_key.toggled.connect(
            lambda on: self.key.setEchoMode(QLineEdit.Normal if on else QLineEdit.Password)
        )
        key_row = QHBoxLayout()
        key_row.setContentsMargins(0, 0, 0, 0)
        key_row.addWidget(self.key, 1)
        key_row.addWidget(self.show_key)
        keyring_error = creds.backend_error()
        key_note = QLabel(
            KEY_NOTE if keyring_error is None
            else f"{keyring_error} Until then, set {ai.API_KEY_ENV} in the environment."
        )
        key_note.setObjectName("hintLabel")
        key_note.setWordWrap(True)

        self.model = QComboBox()
        self.model.setEditable(True)
        self.model.setInsertPolicy(QComboBox.NoInsert)
        self.model.addItem(settings[ai.SETTING_MODEL])
        self.model.setCurrentText(settings[ai.SETTING_MODEL])
        self.model.setToolTip(
            "A model that can look at pictures. anthropic/claude-sonnet-5 is what this "
            "app was tested with; anthropic/claude-opus-5.5 is more careful and costs "
            "more. List Models shows only the endpoint's models that can see."
        )
        self.model.currentTextChanged.connect(self._describe_model)
        self.model_note = QLabel("")
        self.model_note.setObjectName("hintLabel")
        self.model_note.setWordWrap(True)
        self.model_note.setTextFormat(Qt.PlainText)
        self._models: dict[str, dict] = {}
        self.fetch_button = QPushButton("List Models")
        self.fetch_button.setToolTip("Ask Nano-GPT which models can look at images")
        self.fetch_button.clicked.connect(self.fetch_models)
        model_row = QHBoxLayout()
        model_row.setContentsMargins(0, 0, 0, 0)
        model_row.addWidget(self.model, 1)
        model_row.addWidget(self.fetch_button)

        self.frames = QSpinBox()
        self.frames.setRange(1, 4)
        self.frames.setValue(settings[ai.SETTING_FRAMES])
        self.frames.setToolTip(
            "Frames sent from just after each chapter's start. A song's caption "
            "usually shows within the first few seconds; two frames catch most, "
            "and each frame costs about as much as a paragraph of text."
        )

        self.search = QComboBox()
        for key, label in ai.SEARCH_PROVIDERS.items():
            self.search.addItem(label, key)
        self.search.setCurrentIndex(
            max(0, list(ai.SEARCH_PROVIDERS).index(settings[ai.SETTING_SEARCH]))
        )
        self.search.setToolTip(
            "Who searches the web when the model is asked to look a show up. Kagi "
            "and Perplexity find setlist.fm pages reliably; the deep options read "
            "more pages and cost more."
        )

        self.base_url = QLineEdit(settings[ai.SETTING_BASE_URL])
        self.base_url.setToolTip("Only worth changing for another OpenAI-compatible service")
        self.base_url.textChanged.connect(lambda _text: self._update_search())
        other = QLabel(OTHER_ENDPOINTS)
        other.setObjectName("hintLabel")
        other.setWordWrap(True)

        self.test_button = QPushButton("Test")
        self.test_button.setToolTip("Sends the model one tiny request - a fraction of a cent")
        self.test_button.clicked.connect(self.test)
        self.status = QLabel("")
        self.status.setObjectName("hintLabel")
        self.status.setTextFormat(Qt.PlainText)
        self.status.setWordWrap(True)
        test_row = QHBoxLayout()
        test_row.setContentsMargins(0, 0, 0, 0)
        test_row.addWidget(self.test_button)
        test_row.addWidget(self.status, 1)

        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setSpacing(8)
        form.addRow("API key:", key_row)
        form.addRow("", key_note)
        form.addRow("Model:", model_row)
        form.addRow("", self.model_note)
        form.addRow("Frames per chapter:", self.frames)
        form.addRow("Web search by:", self.search)
        form.addRow("Endpoint:", self.base_url)
        form.addRow("", other)

        layout = QVBoxLayout()
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)
        layout.addWidget(about)
        layout.addLayout(form)
        layout.addLayout(test_row)
        layout.addStretch(1)
        tab = QWidget()
        tab.setLayout(layout)
        return tab

    def _build_setlist_tab(self) -> QWidget:
        about = QLabel(SETLISTFM_ABOUT)
        about.setObjectName("hintLabel")
        about.setWordWrap(True)
        about.setOpenExternalLinks(True)
        self.setlist_key = QLineEdit(self._stored_setlist_key)
        self.setlist_key.setEchoMode(QLineEdit.Password)
        self.setlist_key.setPlaceholderText("Paste your setlist.fm API key…")
        show = QCheckBox("Show")
        show.toggled.connect(
            lambda on: self.setlist_key.setEchoMode(QLineEdit.Normal if on else QLineEdit.Password)
        )
        key_row = QHBoxLayout()
        key_row.setContentsMargins(0, 0, 0, 0)
        key_row.addWidget(self.setlist_key, 1)
        key_row.addWidget(show)
        keyring_error = creds.backend_error()
        note = QLabel(
            "The key is kept in the system keyring, not in a file. Setting "
            f"{setlistfm.API_KEY_ENV} in the environment overrides it."
            if keyring_error is None
            else f"{keyring_error} Until then, set {setlistfm.API_KEY_ENV} in the environment."
        )
        note.setObjectName("hintLabel")
        note.setWordWrap(True)
        self.setlist_test_button = QPushButton("Test")
        self.setlist_test_button.setToolTip("Asks setlist.fm one small question with this key")
        self.setlist_test_button.clicked.connect(self.test_setlist_key)
        self.setlist_status = QLabel("")
        self.setlist_status.setObjectName("hintLabel")
        self.setlist_status.setTextFormat(Qt.PlainText)
        self.setlist_status.setWordWrap(True)
        test_row = QHBoxLayout()
        test_row.setContentsMargins(0, 0, 0, 0)
        test_row.addWidget(self.setlist_test_button)
        test_row.addWidget(self.setlist_status, 1)
        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setSpacing(8)
        form.addRow("API key:", key_row)
        form.addRow("", note)
        layout = QVBoxLayout()
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)
        layout.addWidget(about)
        layout.addLayout(form)
        layout.addLayout(test_row)
        layout.addStretch(1)
        tab = QWidget()
        tab.setLayout(layout)
        return tab

    def test_setlist_key(self) -> None:
        if not self.allowed(privacy.SETLISTFM):
            self.setlist_status.setText(SETLISTFM_OFF_HERE)
            return
        key = self.setlist_key.text().strip() or setlistfm.api_key()
        if not key:
            self.setlist_status.setText("Paste an API key first.")
            return
        allowed = {choice: self.allowed(choice) for choice in privacy.CHOICES}
        self.setlist_test_button.setEnabled(False)
        self.setlist_status.setText("Asking setlist.fm…")

        def work():
            # The form's choice, not settings.json's: it may not be saved yet.
            if not privacy.allowed(privacy.SETLISTFM, allowed):
                raise setlistfm.SetlistError(SETLISTFM_OFF_HERE)
            return setlistfm.check_key(key, allowed)

        self._jobs.append(run_job(
            self, work,
            on_done=lambda _found: self._setlist_tested("setlist.fm accepted the key."),
            on_failed=lambda message: self._setlist_tested(f"That didn't work: {message}"),
        ))

    def _setlist_tested(self, text: str) -> None:
        self.setlist_test_button.setEnabled(True)
        self.setlist_status.setText(text)

    # --- what's on the form ----------------------------------------------

    def allowed(self, choice: str) -> bool:
        return self.privacy_checks[choice].isChecked()

    def settings(self) -> dict:
        """The AI settings as the form has them, in force (so with the
        environment's key if one is set)."""
        return ai.settings_from({
            **{choice: self.allowed(choice) for choice in privacy.CHOICES},
            ai.SETTING_KEY: self.key.text().strip(),
            ai.SETTING_MODEL: self.model.currentText().strip() or ai.DEFAULT_MODEL,
            ai.SETTING_BASE_URL: self.base_url.text().strip() or ai.DEFAULT_BASE_URL,
            ai.SETTING_FRAMES: self.frames.value(),
            ai.SETTING_SEARCH: self.search.currentData(),
        })

    def accept(self) -> None:
        key = self.key.text().strip()
        if key != self._stored_key:
            try:
                ai.store_key(key)
            except Exception as e:  # noqa: BLE001 - keyring locked or unavailable
                self.status.setText(f"Couldn't keep the key in the keyring: {e}")
                return
        setlist_key = self.setlist_key.text().strip()
        if setlist_key != self._stored_setlist_key:
            try:
                setlistfm.store_key(setlist_key)
            except Exception as e:  # noqa: BLE001 - keyring locked or unavailable
                self.tabs.setCurrentIndex(TAB_SETLISTFM)
                self.setlist_status.setText(f"Couldn't keep the key in the keyring: {e}")
                return
        settings = self._app_settings
        # Not in the file: a key from before the keyring is dropped here.
        settings.pop(ai.SETTING_KEY, None)
        settings[ai.SETTING_MODEL] = self.model.currentText().strip() or ai.DEFAULT_MODEL
        settings[ai.SETTING_BASE_URL] = self.base_url.text().strip() or ai.DEFAULT_BASE_URL
        settings[ai.SETTING_FRAMES] = self.frames.value()
        settings[ai.SETTING_SEARCH] = self.search.currentData()
        for choice in privacy.CHOICES:
            privacy.set_allowed(settings, choice, self.allowed(choice))
        store.save_app_settings(settings)
        super().accept()

    # --- asking Nano-GPT ---------------------------------------------------

    def fetch_models(self) -> None:
        settings = self.settings()
        if not ai.allowed(settings):
            self.status.setText(AI_OFF_HERE)
            return
        self.fetch_button.setEnabled(False)
        self.status.setText("Asking Nano-GPT for its models…")
        self._jobs.append(run_job(
            self,
            lambda: ai.list_models(settings),
            on_done=self._show_models,
            on_failed=lambda message: self._failed(f"Couldn't list models: {message}"),
        ))

    def _show_models(self, models) -> None:
        self.fetch_button.setEnabled(True)
        current = self.model.currentText()
        self._models = {m["id"]: m for m in models}
        self.model.blockSignals(True)
        self.model.clear()
        for model in models:
            self.model.addItem(model["id"])
        self.model.blockSignals(False)
        self.model.setCurrentText(current)
        self._describe_model(current)
        if ai.vision_known(models):
            self.status.setText(f"{len(models)} models can look at pictures.")
        else:
            self.status.setText(
                f"{len(models)} models - this endpoint doesn't say which can look at "
                "pictures, and this app needs one that can."
            )

    def _describe_model(self, model_id: str) -> None:
        model = self._models.get(model_id.strip())
        note = ai.RECOMMENDED.get(model_id.strip(), "")
        parts = []
        if model:
            parts.append(model["name"])
            if model.get("price"):
                parts.append(model["price"])
        if note:
            parts.append(note)
        self.model_note.setText(" · ".join(parts))

    def _update_search(self) -> None:
        on_nano = ai.searches_the_web({ai.SETTING_BASE_URL: self.base_url.text().strip()})
        self.search.setEnabled(on_nano)
        self.search.setToolTip(
            "Who searches the web when the model is asked to look a show up." if on_nano
            else "Web search is Nano-GPT's own: with another endpoint it's switched off."
        )

    def test(self) -> None:
        settings = self.settings()
        if not ai.allowed(settings):
            self.status.setText(AI_OFF_HERE)
            return
        if not ai.is_configured(settings):
            self.status.setText("Paste an API key first.")
            return
        self.test_button.setEnabled(False)
        self.status.setText(f"Asking {ai.short_model_name(settings[ai.SETTING_MODEL])}…")
        self._jobs.append(run_job(
            self,
            lambda: ai.ping(settings),
            on_done=lambda word: self._tested(settings, word),
            on_failed=lambda message: self._failed(f"That didn't work: {message}"),
        ))

    def _tested(self, settings, word: str) -> None:
        self.test_button.setEnabled(True)
        self.status.setText(
            f"{ai.short_model_name(settings[ai.SETTING_MODEL])} answered: {word[:40]}"
        )

    def _failed(self, message: str) -> None:
        self.fetch_button.setEnabled(True)
        self.test_button.setEnabled(True)
        self.status.setText(message)
