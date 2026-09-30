# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Where the Nano-GPT key and model are set.

The key is pasted once and kept in the app's settings; the model can be
typed or picked from the ones Nano-GPT lists with vision (the Claude ones
first). Test sends the smallest possible request so a wrong key or an
empty balance is found here, not after forty frames have been sent.
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
    QVBoxLayout,
)

from mediabrowser.core import ai, store
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
KEY_NOTE = (
    "The key is stored in this app's settings.json, in plain text. Setting "
    f"{ai.API_KEY_ENV} in the environment overrides it."
)


class AISettingsDialog(QDialog):
    def __init__(self, parent) -> None:
        super().__init__(parent)
        self.setWindowTitle("AI Settings")
        self.setModal(True)
        self.setMinimumWidth(620)
        # Room for the wrapped notes: a form layout doesn't grow for them.
        self.setMinimumHeight(640)
        self._jobs: list = []
        self._app_settings = store.load_app_settings()
        settings = ai.settings_from(self._app_settings)

        about = QLabel(ABOUT)
        about.setObjectName("hintLabel")
        about.setWordWrap(True)
        about.setOpenExternalLinks(True)

        self.key = QLineEdit(self._app_settings.get(ai.SETTING_KEY) or "")
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
        key_note = QLabel(KEY_NOTE)
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

        buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
        save = buttons.addButton("Save", QDialogButtonBox.AcceptRole)
        save.setObjectName("primaryButton")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)
        layout.addWidget(about)
        layout.addLayout(form)
        layout.addLayout(test_row)
        layout.addWidget(buttons)
        self._describe_model(self.model.currentText())
        self._update_search()

    # --- what's on the form ----------------------------------------------

    def settings(self) -> dict:
        """The AI settings as the form has them, in force (so with the
        environment's key if one is set)."""
        return ai.settings_from({
            ai.SETTING_KEY: self.key.text().strip(),
            ai.SETTING_MODEL: self.model.currentText().strip() or ai.DEFAULT_MODEL,
            ai.SETTING_BASE_URL: self.base_url.text().strip() or ai.DEFAULT_BASE_URL,
            ai.SETTING_FRAMES: self.frames.value(),
            ai.SETTING_SEARCH: self.search.currentData(),
        })

    def accept(self) -> None:
        settings = self._app_settings
        settings[ai.SETTING_KEY] = self.key.text().strip()
        settings[ai.SETTING_MODEL] = self.model.currentText().strip() or ai.DEFAULT_MODEL
        settings[ai.SETTING_BASE_URL] = self.base_url.text().strip() or ai.DEFAULT_BASE_URL
        settings[ai.SETTING_FRAMES] = self.frames.value()
        settings[ai.SETTING_SEARCH] = self.search.currentData()
        store.save_app_settings(settings)
        super().accept()

    # --- asking Nano-GPT ---------------------------------------------------

    def fetch_models(self) -> None:
        settings = self.settings()
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
