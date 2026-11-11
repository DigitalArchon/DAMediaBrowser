# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Settings: what may go out, and the AI it goes to.

Privacy comes first: a tick box for each place the app can send anything
(see core.privacy), all unticked until the person ticks them. Under the
AI's, Privacy Mode: the AI's requests only to a private model, attested
before anything is sent to it (core.private_ai), with a line that says, as
the form stands, where they will go and who can read them.

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
    QScrollArea,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from mediabrowser.core import (
    ai,
    creds,
    netguard,
    outbound,
    privacy,
    private_ai,
    sealed,
    setlistfm,
    store,
)
from mediabrowser.gui import links, network_status
from mediabrowser.gui.worker import run_job

OTHER_ENDPOINTS = (
    "Any OpenAI-compatible service and any model that can look at pictures can be "
    "used instead - set the endpoint (https, unless it runs on this computer or its "
    "own network) and the model. But web search is Nano-GPT's "
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
PRIVACY_MODE_LABEL = "Privacy Mode: only to a private model, attested first"
PRIVACY_MODE_NOTE = (
    "Everything for the AI goes only to the private model below, and only after its "
    "enclave has proved what it is, with the hardware maker's signatures. A private/ model is "
    "end-to-end encrypted: Nano-GPT relays only ciphertext, and sees your account, the "
    "model, when and how much. A TEE/ model is attested too - most as RedPill's TEE "
    "gateway, which passes each request on to the machine that runs the model, so that "
    "machine is the gateway's word - and Nano-GPT's gateway can read what is sent. "
    "Nothing falls back to the ordinary model, and there's no web search."
)
STRICT_LABEL = "Strict: refuse an enclave that proves less than it could"
STRICT_NOTE = (
    "Otherwise one that passed every check it took, but couldn't take them all - an "
    "older release, Intel's or NVIDIA's services out of reach - is used, and shown in "
    "amber."
)
WEB_SEARCH_OFF_IN_PRIVACY_MODE = (
    "Not in Privacy Mode: Nano-GPT would have to read the request to search for it."
)
ATTEST_E2EE = (
    "Asks nano-gpt's relay for the enclave's report, and checks it against AMD's or "
    "Intel's certificates and Tinfoil's release record on Sigstore. Nothing about your "
    "videos is sent, and it costs nothing."
)
ATTEST_TEE = (
    "Asks nano-gpt for the model's report and RedPill's gateway's, checks them with "
    "Intel's and NVIDIA's attestation services, and says what was attested: RedPill's "
    "gateway, the provider's instances, or one enclave. Nothing about your videos is "
    "sent, and it costs nothing."
)
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
        # Whether Tinfoil's client is installed takes a moment to find out the
        # first time; the notes say so once it's known.
        if sealed.known() is None:
            self._jobs.append(run_job(self, sealed.available,
                                      on_done=lambda _answer: self._update_privacy()))

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
                self.web_search_note = note
            self.privacy_checks[choice] = check
            layout.addWidget(check)
            layout.addWidget(note)
            if choice == privacy.AI:
                # Where it goes, as the form has it now.
                self.where_label = QLabel()
                self.where_label.setObjectName("toneLabel")
                self.where_label.setWordWrap(True)
                self.where_label.setTextFormat(Qt.PlainText)
                self.where_label.setContentsMargins(26, 2, 0, 0)
                layout.addWidget(self.where_label)
                layout.addSpacing(8)
                layout.addLayout(self._build_privacy_mode())
            layout.addSpacing(8)
        self.privacy_checks[privacy.AI].toggled.connect(lambda _on: self._update_privacy())
        self.privacy_checks[privacy.WEB_SEARCH].toggled.connect(
            lambda _on: self._update_privacy())
        private = QLabel(PRIVATE_NOTE)
        private.setObjectName("hintLabel")
        private.setWordWrap(True)
        layout.addWidget(private)
        layout.addStretch(1)
        inner = QWidget()
        inner.setLayout(layout)
        scroll = QScrollArea()
        scroll.setWidget(inner)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        return scroll

    def _build_privacy_mode(self) -> QVBoxLayout:
        """Privacy Mode's box, its model, Strict and Check Attestation, indented
        under the AI's box: the AI's to allow first."""
        settings = ai.settings_from(self._app_settings)
        self.privacy_mode_check = QCheckBox(PRIVACY_MODE_LABEL)
        self.privacy_mode_check.setChecked(settings[ai.SETTING_PRIVACY_MODE])
        self.privacy_mode_check.toggled.connect(lambda _on: self._update_privacy())
        note = QLabel(PRIVACY_MODE_NOTE)
        note.setObjectName("hintLabel")
        note.setWordWrap(True)
        note.setContentsMargins(26, 0, 0, 0)
        self.privacy_mode_note = note

        self.private_model = QComboBox()
        self.private_model.setEditable(True)
        self.private_model.setInsertPolicy(QComboBox.NoInsert)
        current = settings[ai.SETTING_PRIVATE_MODEL] or ai.DEFAULT_PRIVATE_MODEL
        for model in dict.fromkeys([current, *ai.PRIVATE_RECOMMENDED]):
            self.private_model.addItem(model)
        self.private_model.setCurrentText(current)
        self.private_model.setToolTip(
            "A private/ model is end-to-end encrypted; a TEE/ model is attested, but "
            "Nano-GPT's gateway reads what is sent. It must be able to see the frames. "
            "List Private Models shows the ones that can."
        )
        self.private_model.currentTextChanged.connect(lambda _text: self._update_privacy())
        self._private_models: dict[str, dict] = {}
        self.private_list_button = QPushButton("List Private Models")
        self.private_list_button.setToolTip(
            "Ask Nano-GPT which private and TEE models can look at pictures. Its lists "
            "are public: no key is sent.")
        self.private_list_button.clicked.connect(self.fetch_private_models)
        model_row = QHBoxLayout()
        model_row.setContentsMargins(0, 0, 0, 0)
        model_row.addWidget(QLabel("Private model:"))
        model_row.addWidget(self.private_model, 1)
        model_row.addWidget(self.private_list_button)
        self.private_model_note = QLabel()
        self.private_model_note.setObjectName("toneLabel")
        self.private_model_note.setWordWrap(True)
        self.private_model_note.setTextFormat(Qt.PlainText)

        self.strict_check = QCheckBox(STRICT_LABEL)
        self.strict_check.setChecked(settings[ai.SETTING_STRICT])
        self.strict_check.setToolTip(STRICT_NOTE)
        strict_note = QLabel(STRICT_NOTE)
        strict_note.setObjectName("hintLabel")
        strict_note.setWordWrap(True)
        strict_note.setContentsMargins(26, 0, 0, 0)
        self.strict_note = strict_note

        self.attest_button = QPushButton("Check Attestation")
        self.attest_button.clicked.connect(self.check_attestation)
        self.attest_status = QLabel("")
        self.attest_status.setObjectName("toneLabel")
        self.attest_status.setTextFormat(Qt.PlainText)
        self.attest_status.setWordWrap(True)
        self.attest_status.setTextInteractionFlags(Qt.TextSelectableByMouse)
        attest_row = QHBoxLayout()
        attest_row.setContentsMargins(0, 0, 0, 0)
        attest_row.addWidget(self.attest_button, 0, Qt.AlignTop)
        attest_row.addWidget(self.attest_status, 1)

        inner = QVBoxLayout()
        inner.setContentsMargins(26, 0, 0, 0)
        inner.setSpacing(6)
        inner.addLayout(model_row)
        inner.addWidget(self.private_model_note)
        inner.addWidget(self.strict_check)
        inner.addWidget(strict_note)
        inner.addLayout(attest_row)

        box = QVBoxLayout()
        box.setContentsMargins(26, 0, 0, 0)
        box.setSpacing(4)
        box.addWidget(self.privacy_mode_check)
        box.addWidget(note)
        box.addLayout(inner)
        return box

    def _update_privacy(self) -> None:
        ai_on = self.privacy_checks[privacy.AI].isChecked()
        private = self.privacy_mode_check.isChecked()
        for widget in (self.privacy_mode_check, self.privacy_mode_note):
            widget.setEnabled(ai_on)
        for widget in (self.private_model, self.private_list_button, self.strict_check,
                       self.strict_note, self.attest_button, self.private_model_note):
            widget.setEnabled(ai_on and private)
        search = self.privacy_checks[privacy.WEB_SEARCH]
        search.setEnabled(ai_on and not private)
        provider = (self.search.currentText() if hasattr(self, "search")
                    else ai.SEARCH_PROVIDERS[ai.DEFAULT_SEARCH])
        self.web_search_note.setText(
            WEB_SEARCH_OFF_IN_PRIVACY_MODE if private else
            f"Nano-GPT has {provider} (chosen on the AI tab) search the web about the show "
            "for the model - the artist, tour and venue. Both see the search.")
        if hasattr(self, "base_url"):
            self._update_where()

    def _update_where(self) -> None:
        """Where the AI's requests go, and who can read them, as the form has it;
        and what stands in Privacy Mode's way, if anything does."""
        settings = self.settings()
        model = self.private_model.currentText().strip()
        if not self.allowed(privacy.AI):
            self._tone(self.where_label, "Nothing is sent to the AI.", "")
        else:
            where, protection = ai.destination(settings, online=self.allowed(
                privacy.WEB_SEARCH))
            tone = {"sealed": "ok", "tee": "warning"}.get(protection, "")
            self._tone(self.where_label, f"Sends it {where}.", tone)
        problems = private_ai.problems(settings[ai.SETTING_BASE_URL], model, wait=False)
        if problems:
            self._tone(self.private_model_note, " ".join(problems), "error")
        else:
            listed = self._private_models.get(model) or {}
            parts = [private_ai.describe(model)[0] or "", listed.get("price", ""),
                     listed.get("note", "") or ai.PRIVATE_RECOMMENDED.get(model, "")]
            self._tone(self.private_model_note, " · ".join(p for p in parts if p),
                       "ok" if private_ai.kind_of(model) == private_ai.E2EE else "warning")
        self.attest_button.setToolTip(
            ATTEST_E2EE if private_ai.kind_of(model) == private_ai.E2EE else ATTEST_TEE)
        where, _protection = ai.destination(settings)
        self.test_button.setToolTip(
            f"Sends the model one tiny request {where}: a fraction of a cent.")
        self.ai_privacy_note.setVisible(self.privacy_mode_check.isChecked())
        self.ai_privacy_note.setText(
            f"Privacy Mode is on (Privacy tab): the AI's requests go only to {model}. "
            "The model, web search and endpoint here are what's used when it's off.")

    @staticmethod
    def _tone(label: QLabel, text: str, tone: str) -> None:
        label.setText(text)
        label.setProperty("tone", tone)
        label.style().unpolish(label)
        label.style().polish(label)

    def _build_ai_tab(self, settings: dict) -> QWidget:
        about = QLabel(ABOUT)
        about.setObjectName("hintLabel")
        about.setWordWrap(True)
        links.guard(about)
        self.ai_privacy_note = QLabel()
        self.ai_privacy_note.setObjectName("warningLabel")
        self.ai_privacy_note.setWordWrap(True)
        self.ai_privacy_note.setTextFormat(Qt.PlainText)
        self.ai_privacy_note.hide()

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
        self.model.currentTextChanged.connect(lambda _text: self._update_privacy())
        self.model_note = QLabel("")
        self.model_note.setObjectName("hintLabel")
        self.model_note.setWordWrap(True)
        self.model_note.setTextFormat(Qt.PlainText)
        self._models: dict[str, dict] = {}
        self.fetch_button = QPushButton("List Models")
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
        self.search.currentIndexChanged.connect(lambda _index: self._update_privacy())

        self.base_url = QLineEdit(settings[ai.SETTING_BASE_URL])
        self.base_url.setToolTip("Only worth changing for another OpenAI-compatible service")
        self.base_url.textChanged.connect(lambda _text: self._update_search())
        self.base_url.textChanged.connect(lambda _text: self._update_privacy())
        other = QLabel(OTHER_ENDPOINTS)
        other.setObjectName("hintLabel")
        other.setWordWrap(True)

        self.test_button = QPushButton("Test")
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
        layout.addWidget(self.ai_privacy_note)
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
        links.guard(about)
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
        environment's key if one is set). In Privacy Mode, with a session of
        their own: a test or a check here is attested as any request is."""
        return ai.settings_from({
            **{choice: self.allowed(choice) for choice in privacy.CHOICES},
            ai.SETTING_KEY: self.key.text().strip(),
            ai.SETTING_MODEL: self.model.currentText().strip() or ai.DEFAULT_MODEL,
            ai.SETTING_BASE_URL: self.base_url.text().strip() or ai.DEFAULT_BASE_URL,
            ai.SETTING_FRAMES: self.frames.value(),
            ai.SETTING_SEARCH: self.search.currentData(),
            ai.SETTING_PRIVACY_MODE: self.privacy_mode_check.isChecked(),
            ai.SETTING_PRIVATE_MODEL: self.private_model.currentText().strip(),
            ai.SETTING_STRICT: self.strict_check.isChecked(),
        }, form=True)

    def accept(self) -> None:
        problem = ai.endpoint_problem(self.base_url.text().strip() or ai.DEFAULT_BASE_URL)
        if problem:
            self.tabs.setCurrentIndex(TAB_AI)
            self.status.setText(f"Not saved: {problem}")
            return
        private_model = self.private_model.currentText().strip() or ai.DEFAULT_PRIVATE_MODEL
        if self.privacy_mode_check.isChecked() and private_ai.kind_of(private_model) is None:
            self.tabs.setCurrentIndex(TAB_PRIVACY)
            self._tone(self.attest_status, f"Not saved: {private_model} runs in no attested "
                       "enclave. Choose a private/ or TEE/ model.", "error")
            return
        key = self.key.text().strip()
        unfit = netguard.key_problem(key, "Nano-GPT")
        if unfit:
            self.tabs.setCurrentIndex(TAB_AI)
            self.status.setText(f"Not saved: {unfit}")
            return
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
        settings[ai.SETTING_PRIVACY_MODE] = self.privacy_mode_check.isChecked()
        settings[ai.SETTING_PRIVATE_MODEL] = private_model
        settings[ai.SETTING_STRICT] = self.strict_check.isChecked()
        for choice in privacy.CHOICES:
            privacy.set_allowed(settings, choice, self.allowed(choice))
        store.save_app_settings(settings)
        if settings[ai.SETTING_PRIVACY_MODE] and private_ai.is_private_mode(private_model):
            sealed.check()      # known before the first request needs it
        network_status.settings_changed()
        super().accept()

    # --- asking Nano-GPT ---------------------------------------------------

    def fetch_models(self) -> None:
        settings = self.settings()
        if not ai.allowed(settings):
            self.status.setText(AI_OFF_HERE)
            return
        self.fetch_button.setEnabled(False)
        self.status.setText(f"Asking {self._endpoint_host()} for its models…")
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

    def _endpoint_host(self) -> str:
        url = self.base_url.text().strip() or ai.DEFAULT_BASE_URL
        return outbound.host_of(url) or url

    def _update_search(self) -> None:
        on_nano = ai.searches_the_web({ai.SETTING_BASE_URL: self.base_url.text().strip()})
        self.search.setEnabled(on_nano)
        self.search.setToolTip(
            "Who searches the web when the model is asked to look a show up." if on_nano
            else "Web search is Nano-GPT's own: with another endpoint it's switched off."
        )
        self.fetch_button.setToolTip(
            f"Asks {self._endpoint_host()} which models can look at pictures. The API key "
            "goes with it, if there is one, so that the prices are yours.")

    def test(self) -> None:
        settings = self.settings()
        if not ai.allowed(settings):
            self.status.setText(AI_OFF_HERE)
            return
        if not settings.get(ai.SETTING_KEY):
            self.status.setText("Paste an API key first.")
            return
        if not ai.is_configured(settings):
            self.status.setText(ai.not_ready(settings))
            return
        model = ai.active_model(settings)
        self.test_button.setEnabled(False)
        self.status.setText(f"Attesting {model}'s enclave, then asking it…"
                            if ai.in_privacy_mode(settings) else
                            f"Asking {ai.short_model_name(model)}…")
        self._jobs.append(run_job(
            self,
            lambda: ai.ping(settings),
            on_done=lambda word: self._tested(settings, word),
            on_failed=lambda message: self._failed(f"That didn't work: {message}"),
            purpose="Settings: Test",
        ))

    def _tested(self, settings, word: str) -> None:
        self.test_button.setEnabled(True)
        text = f"{ai.short_model_name(ai.active_model(settings))} answered: {word[:40]}"
        session = ai.session_of(settings)
        tip = ""
        if session is not None:
            proved = private_ai.attestation_label(session.attestation)[0]
            reply = private_ai.receipt_label(session.last_receipt())[0]
            text += f" — {proved}; the reply {reply.lstrip('✓⚠ ')}"
            # What was attested, and what the reply's mark means (receipt_tip).
            tip = " ".join(part for part in (
                private_ai.what_was_attested(session.attestation, clear=False),
                private_ai.receipt_tip(session.last_receipt())) if part)
        self.status.setText(text)
        self.status.setToolTip(tip)

    def check_attestation(self) -> None:
        settings = self.settings()
        model = settings[ai.SETTING_PRIVATE_MODEL]
        self.attest_button.setEnabled(False)
        self._tone(self.attest_status, f"Attesting {model}'s enclave…", "")
        self._jobs.append(run_job(
            self,
            lambda: ai.check_attestation(settings),
            on_done=self._attested,
            on_failed=self._attestation_failed,
            purpose="Settings: Check Attestation",
        ))

    def _attested(self, session) -> None:
        self._update_privacy()      # the button, as the form has it now
        text, tone = private_ai.attestation_label(session.attestation)
        # What was attested first, and what it leaves to someone's word.
        what = private_ai.what_was_attested(session.attestation)
        self._tone(self.attest_status,
                   " ".join(part for part in (f"{text}.", what, session.attestation.detail)
                            if part), tone)

    def _attestation_failed(self, message: str) -> None:
        self._update_privacy()
        self._tone(self.attest_status, f"Refused: {message}", "error")

    def fetch_private_models(self) -> None:
        settings = self.settings()
        self.private_list_button.setEnabled(False)
        self._tone(self.private_model_note, "Asking Nano-GPT for its private models…", "")
        self._jobs.append(run_job(
            self,
            lambda: ai.list_private_models(settings),
            on_done=self._show_private_models,
            on_failed=lambda message: self._private_list_failed(message),
        ))

    def _show_private_models(self, models) -> None:
        self.private_list_button.setEnabled(True)
        current = self.private_model.currentText()
        self._private_models = {m["id"]: m for m in models}
        self.private_model.blockSignals(True)
        self.private_model.clear()
        for model in models:
            self.private_model.addItem(model["id"])
        self.private_model.blockSignals(False)
        self.private_model.setCurrentText(current)
        self._update_privacy()

    def _private_list_failed(self, message: str) -> None:
        self.private_list_button.setEnabled(True)
        self._tone(self.private_model_note, f"Couldn't list the private models: {message}",
                   "error")

    def _failed(self, message: str) -> None:
        self.fetch_button.setEnabled(True)
        self.test_button.setEnabled(True)
        self.status.setText(message)
