# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Settings: what may go out, and the AI it goes to.

Privacy comes first: a tick box for each place the app can send anything
(see core.privacy), all unticked until the person ticks them. Under the
AI's, Privacy Mode: the AI's requests only to a private model, attested
before anything is sent to it (core.private_ai), with a line that says, as
the form stands, where they will go and who can read them.

The AI tab is where the provider, its key and the model are set. One
endpoint, chosen from NanoGPT, Chutes, Tinfoil, Anthropic or another
OpenAI-compatible service: choosing one fills in its address, and the
protocol it speaks follows from the address (ai.provider_of), however the
address got there - so a Tinfoil address typed by hand is Tinfoil's, sealed
end to end. Each provider's key is pasted once and kept in the OS keyring,
one per provider, so switching and back loses none; the model can be typed
or picked from the ones the provider lists with vision, each with how
private it is. Test sends the smallest possible request so a wrong key or an
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
    QMessageBox,
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
    levels,
    netguard,
    outbound,
    privacy,
    private_ai,
    sealed,
    setlistfm,
    store,
)
from mediabrowser.gui import links, network_status, plain
from mediabrowser.gui.plain import tooltip
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
# What each provider is, in a few sentences, where it is chosen.
ABOUTS = {
    ai.NANOGPT: ABOUT,
    ai.CHUTES: (
        "Chutes (chutes.ai) runs open models on machines third parties rent out, paid by "
        "the request or by subscription. Its -TEE models run in Intel TDX enclaves and are "
        "sent end-to-end encrypted, each request sealed here to one attested instance "
        "(partial privacy, preliminary: the program holding the key, chutes-aegis.so, is "
        "Chutes' own closed binary, admitted by its signature, not measured). Its other "
        "models are not private: Chutes reads their text."),
    ai.TINFOIL: (
        "Tinfoil (tinfoil.sh) runs open models in attested enclaves, every request sealed "
        "here to its router's attested key, over a connection pinned to the enclave's TLS "
        "key: Tinfoil sees only ciphertext (highest privacy, preliminary). That the router "
        "hands a request only to attested model enclaves is its published code, not "
        "something this app checks."),
    ai.ANTHROPIC: (
        "Anthropic's own API (api.anthropic.com), spoken natively, the key sent as "
        "Anthropic takes it. Not private: Anthropic reads what is sent. Claude Sonnet 5 is "
        "what this app was tested with."),
    ai.OTHER: (
        "Another service that speaks the OpenAI chat API: set its address and a model "
        "that can look at pictures. Not private: the service reads what is sent."),
}
# Where each provider's key comes from, for the field's placeholder.
KEY_FROM = {
    ai.NANOGPT: "Paste the API key from nano-gpt.com…",
    ai.CHUTES: "Paste your Chutes API key (chutes.ai)…",
    ai.TINFOIL: "Paste your Tinfoil API key (tinfoil.sh)…",
    ai.ANTHROPIC: "Paste your Anthropic API key (console.anthropic.com)…",
    ai.OTHER: "Paste the service's API key, if it needs one…",
}
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
    "With Tinfoil as the provider (AI tab), every model is end-to-end encrypted to its "
    "attested router (highest privacy); with Chutes, its -TEE models to one attested "
    "instance (partial privacy, never under Strict). "
    "Nothing falls back to the ordinary model, and there's no web search."
)
STRICT_LABEL = "Strict: refuse an enclave that proves less than it could"
# Off by default, and said plainly beside it (SealedLore's author, Nov 11 2026): the one
# request that goes to RedPill directly, not through Nano-GPT.
UPSTREAMS_LABEL = "Name the provider behind each TEE reply"
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
ATTEST_TINFOIL = (
    "Asks Tinfoil's atc.tinfoil.sh for its router's attestation bundle, and checks it "
    "against AMD's or Intel's certificates and Tinfoil's release record on Sigstore. No "
    "key is sent, nothing about your videos, and it costs nothing."
)
ATTEST_CHUTES = (
    "Asks Chutes for the model's instances and their evidence for a fresh nonce, and "
    "checks each with Intel's and NVIDIA's attestation services and Chutes' published "
    "measurements. With a key, that each one's quote binds the key a request would be "
    "sealed to; without one, Chutes' public evidence only (the key binding unchecked). "
    "Nothing about your videos is sent, and it costs nothing."
)
ATTEST_TEE = (
    "Asks nano-gpt for the model's report and RedPill's gateway's, checks them with "
    "Intel's and NVIDIA's attestation services, and says what was attested: RedPill's "
    "gateway, the provider's instances, or one enclave. Nothing about your videos is "
    "sent, and it costs nothing."
)
KEY_NOTE = (
    "The key is kept in the system keyring (GNOME Keyring or KWallet), not in "
    "a file, and goes only to the server it was entered for: changing the endpoint to "
    f"another asks first. Setting {ai.API_KEY_ENV} in the environment overrides it, for "
    "Nano-GPT's own server only."
)
# Where each provider's key goes, under its field: one key kept per provider.
KEY_NOTES = {
    ai.NANOGPT: KEY_NOTE,
    ai.CHUTES: (
        "Kept in the system keyring, one key per provider, not in a file. A Chutes key "
        "goes only to Chutes' own three addresses, over https: llm.chutes.ai, "
        "claude.chutes.ai and api.chutes.ai, where its end-to-end encryption goes. The "
        "checks of Intel's and NVIDIA's carry none."),
    ai.TINFOIL: (
        "Kept in the system keyring, one key per provider, not in a file. A Tinfoil key "
        "goes only to inference.tinfoil.sh, over a connection pinned to the attested "
        "enclave; its attestation and its list of models are fetched with none."),
    ai.ANTHROPIC: (
        "Kept in the system keyring, one key per provider, not in a file, and sent only "
        "to the server it was entered for, as Anthropic's x-api-key."),
    ai.OTHER: (
        "Kept in the system keyring, one key per provider, not in a file, and sent only "
        "to the server it was entered for: changing the endpoint to another asks first."),
}
# Said by the question before Test, List Models or Check Attestation sends the key.
NOTHING_SENT = "No sends nothing."


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
        # Each provider's saved key, the keyring entry it is in and the server it was
        # entered for (ai.key_slot, ai.key_origin): one per provider, so that switching
        # and back loses none. _stored_key and _key_origin are the shown provider's. And
        # the servers it was said here a key may go to, or not (_ask_about_the_key): an
        # address changed to another server never takes the saved key along unasked.
        self._saved = self._saved_keys(self._app_settings.get(ai.SETTING_BASE_URL)
                                       or ai.DEFAULT_BASE_URL)
        # What the key field holds for each provider, as typed here.
        self._typed = {provider: saved[0] for provider, saved in self._saved.items()}
        self._shown_provider = ai.provider_of(self._app_settings.get(ai.SETTING_BASE_URL)
                                              or ai.DEFAULT_BASE_URL)
        self._key_may_go: set[str] = set()
        self._key_may_not_go: set[str] = set()
        # The server a No was said to at an earlier Save (ai.SETTING_KEY_DECLINED): Save
        # asks no more about it; an action that sends the key now still does.
        self._key_declined = str(self._app_settings.get(ai.SETTING_KEY_DECLINED) or "")
        # The key set to go over plain http to another machine as Settings opened: sent
        # if it was confirmed for it (ai.SETTING_KEY_IN_CLEAR), else held back until it is
        # (nothing is taken as confirmed: the author, Nov 14 2026), shown with Confirm….
        # Save asks about neither again; Test, List Models and Check Attestation, which
        # send it at once, ask about one held back. And those said Yes to since
        # (_may_send_plain), which Save records.
        opened = ai.settings_from(dict(self._app_settings, **{
            ai.SETTING_KEY: self._stored_key, ai.KEY_ORIGIN: self._key_origin}))
        self._plain_at_open = self._in_the_clear(opened, self._stored_key)
        agreed = str(self._app_settings.get(ai.SETTING_KEY_IN_CLEAR) or "")
        self._plain_confirmed = [entry for entry in self._plain_at_open if entry[1] == agreed]
        self._plain_allowed: list[tuple[str, str]] = []
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

    @staticmethod
    def _saved_keys(address: str) -> dict:
        """{provider: (its saved key, the server it was entered for, its keyring entry)}
        as the keyring has them: the saved address's provider's as load_settings reads
        it, each other's at its own preset address."""
        found = {}
        at = ai.provider_of(address)
        for provider in ai.PROVIDERS:
            there = address if provider == at else ai.PROVIDER_ADDRESSES.get(provider, "")
            if there:
                slot = ai.key_slot(there)
            else:
                slot = ai.OTHER if ai.stored_key(ai.OTHER) or not ai.stored_key(ai.NANOGPT) \
                    else ai.NANOGPT
            key = ai.stored_key(slot)
            origin = ai.key_origin(there or address, slot) if key else None
            found[provider] = (key, origin, slot)
        return found

    @property
    def _stored_key(self) -> str:
        """The shown provider's saved key."""
        return self._saved[self._shown_provider][0]

    @property
    def _key_origin(self):
        """The server the shown provider's saved key was entered for; None for none."""
        return self._saved[self._shown_provider][1]

    @property
    def _key_slot(self) -> str:
        return self._saved[self._shown_provider][2]

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
            "Ask the provider which of its private models can look at pictures - Nano-GPT's "
            "private/ and TEE/ ones, Tinfoil's, Chutes' -TEE ones. Their lists are public: "
            "no key is sent.")
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

        self.upstreams_check = QCheckBox(UPSTREAMS_LABEL)
        self.upstreams_check.setChecked(settings[ai.SETTING_NAME_UPSTREAMS])
        self.upstreams_check.setToolTip(private_ai.UPSTREAMS_NOTE)
        self.upstreams_check.toggled.connect(lambda _on: self._update_privacy())
        upstreams_note = QLabel(private_ai.UPSTREAMS_NOTE)
        upstreams_note.setObjectName("hintLabel")
        upstreams_note.setWordWrap(True)
        upstreams_note.setContentsMargins(26, 0, 0, 0)
        self.upstreams_note = upstreams_note

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
        inner.addWidget(self.upstreams_check)
        inner.addWidget(upstreams_note)
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
                       self.strict_note, self.upstreams_check, self.upstreams_note,
                       self.attest_button, self.private_model_note):
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
            level = ai.privacy_of(settings)
            tone = {levels.HIGHEST: "ok", levels.PARTIAL: "warning"}.get(level.level, "")
            self._tone(self.where_label, f"Sends it {where}. {level.words}.", tone)
        problems = ai.privacy_problems(settings, model, wait=False)
        found = levels.privacy_at(model, settings[ai.SETTING_BASE_URL])
        if problems:
            self._tone(self.private_model_note, " ".join(problems), "error")
        else:
            listed = self._private_models.get(model) or {}
            label, meaning = ai.model_privacy(settings, model, private=True)
            note = listed.get("note", "") or ai.PRIVATE_RECOMMENDED.get(model, "")
            parts = [label, listed.get("price", ""), "" if note == label else note]
            self._tone(self.private_model_note, " · ".join(p for p in parts if p),
                       "ok" if found.level == levels.HIGHEST else "warning")
            self.private_model_note.setToolTip(tooltip(meaning))
        self.attest_button.setToolTip(
            ATTEST_TINFOIL if found.service == levels.TINFOIL else
            ATTEST_CHUTES if found.service == levels.CHUTES else
            ATTEST_E2EE if found.sealed else ATTEST_TEE)
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
        provider = self._shown_provider
        self.provider = QComboBox()
        for key, label in ai.PROVIDERS.items():
            self.provider.addItem(label, key)
        self.provider.setCurrentIndex(list(ai.PROVIDERS).index(provider))
        self.provider.setToolTip(
            "Fills in the provider's address and the protocol it speaks. Whatever way an "
            "address is set - chosen here, typed, an older settings file - the provider is "
            "read from the address itself, and with it how requests go and where its key "
            "may go.")
        # A choice made here: the provider's address, model and key (not one the address
        # typed below brings, which is followed without changing them).
        self.provider.activated.connect(lambda _index: self._provider_chosen())
        about = QLabel(ABOUTS[provider])
        about.setObjectName("hintLabel")
        about.setWordWrap(True)
        links.guard(about)
        self.about = about
        self.ai_privacy_note = QLabel()
        self.ai_privacy_note.setObjectName("warningLabel")
        self.ai_privacy_note.setWordWrap(True)
        self.ai_privacy_note.setTextFormat(Qt.PlainText)
        self.ai_privacy_note.hide()

        self.key = QLineEdit(self._stored_key)
        self.key.setEchoMode(QLineEdit.Password)
        self.key.setPlaceholderText(KEY_FROM[provider])
        self.key_label = QLabel(self._key_words(provider))
        self.show_key = QCheckBox("Show")
        self.show_key.toggled.connect(
            lambda on: self.key.setEchoMode(QLineEdit.Normal if on else QLineEdit.Password)
        )
        key_row = QHBoxLayout()
        key_row.setContentsMargins(0, 0, 0, 0)
        key_row.addWidget(self.key, 1)
        key_row.addWidget(self.show_key)
        keyring_error = creds.backend_error()
        self._keyring_error = keyring_error
        key_note = QLabel(self._key_note(provider))
        key_note.setObjectName("hintLabel")
        key_note.setWordWrap(True)
        self.key_note = key_note

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
        self.base_url.setToolTip(
            "The provider's API address. Tinfoil's, Chutes' or Anthropic's typed here is "
            "theirs, with everything that follows from it.")
        self.base_url.textChanged.connect(lambda _text: self._address_changed())
        self.base_url.textChanged.connect(lambda _text: self._update_search())
        self.base_url.textChanged.connect(lambda _text: self._update_privacy())
        self.protocol = QLabel()
        self.protocol.setObjectName("hintLabel")
        self.protocol.setWordWrap(True)
        self.protocol.setTextFormat(Qt.PlainText)
        self._show_protocol()
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
        form.addRow("Provider:", self.provider)
        form.addRow(self.key_label, key_row)
        form.addRow("", key_note)
        held = self._held_back_row()
        if held is not None:
            form.addRow("", held)
        form.addRow("Model:", model_row)
        form.addRow("", self.model_note)
        form.addRow("Frames per chapter:", self.frames)
        form.addRow("Web search by:", self.search)
        form.addRow("Endpoint:", self.base_url)
        form.addRow("Speaks:", self.protocol)
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

    # --- the provider ----------------------------------------------------

    @staticmethod
    def _key_words(provider: str) -> str:
        return "API key:" if provider == ai.OTHER else f"{ai.PROVIDERS[provider]} API key:"

    def _key_note(self, provider: str) -> str:
        if self._keyring_error is not None:
            return f"{self._keyring_error} Until then, set {ai.API_KEY_ENV} in the environment."
        return KEY_NOTES[provider]

    def _provider_chosen(self) -> None:
        """A provider picked from the list: its address and its model fill in (another
        service's are left to be typed), and its own key shows."""
        provider = self.provider.currentData()
        address = ai.PROVIDER_ADDRESSES.get(provider)
        if address is None and ai.provider_of(self._address()) != ai.OTHER:
            address = ""            # another service's: to be typed
        if address is not None and address != self.base_url.text().strip():
            self.base_url.setText(address)
        self._show_provider(provider)
        model = ai.PROVIDER_MODELS.get(provider)
        if model:
            self.model.setCurrentText(model)
        if provider == ai.OTHER:
            self.base_url.setFocus()

    def _address_changed(self) -> None:
        """The provider follows the address, however it is set: typed, pasted or chosen."""
        found = ai.provider_of(self._address()) if self.base_url.text().strip() else \
            self.provider.currentData()
        if found != self.provider.currentData():
            self.provider.blockSignals(True)
            self.provider.setCurrentIndex(list(ai.PROVIDERS).index(found))
            self.provider.blockSignals(False)
        self._show_provider(found)
        self._show_protocol()

    def _show_provider(self, provider: str) -> None:
        """The key field, its words and the notes for `provider`: what was typed for the
        one shown before is kept for it, and its own saved key (or what was typed for it
        here) shows."""
        if provider == self._shown_provider:
            return
        self._typed[self._shown_provider] = self.key.text()
        self._shown_provider = provider
        self.key.blockSignals(True)
        self.key.setText(self._typed.get(provider, ""))
        self.key.blockSignals(False)
        self.key.setPlaceholderText(KEY_FROM[provider])
        self.key_label.setText(self._key_words(provider))
        self.key_note.setText(self._key_note(provider))
        self.about.setText(ABOUTS[provider])

    def _show_protocol(self) -> None:
        protocol = ai.protocol_of(self._address())
        self.protocol.setText(ai.PROTOCOL_WORDS.get(protocol, protocol))

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
            ai.KEY_ORIGIN: self._form_key_origin(),
            ai.KEY_SLOT: self._key_slot,
            ai.SETTING_MODEL: self.model.currentText().strip() or ai.DEFAULT_MODEL,
            ai.SETTING_BASE_URL: self.base_url.text().strip() or ai.DEFAULT_BASE_URL,
            ai.SETTING_FRAMES: self.frames.value(),
            ai.SETTING_SEARCH: self.search.currentData(),
            ai.SETTING_PRIVACY_MODE: self.privacy_mode_check.isChecked(),
            ai.SETTING_PRIVATE_MODEL: self.private_model.currentText().strip(),
            ai.SETTING_STRICT: self.strict_check.isChecked(),
            ai.SETTING_NAME_UPSTREAMS: self.upstreams_check.isChecked(),
            ai.SETTING_KEY_IN_CLEAR: self._form_in_clear(),
        }, form=True)

    def _form_in_clear(self) -> str:
        """The server the key on the form may go to over plain http (ai.
        SETTING_KEY_IN_CLEAR): confirmed before or said Yes to here, else ""."""
        key, there = self.key.text().strip(), ai.origin_of(self._address())
        if (key, there) in self._plain_confirmed + self._plain_allowed:
            return there
        return ""

    def _address(self) -> str:
        return self.base_url.text().strip() or ai.DEFAULT_BASE_URL

    def _keeps_the_saved_key(self) -> bool:
        """Whether the key field holds the saved key as it was: not one typed here, which
        is for the address as the form has it. (The environment's key stands in for it
        at Nano-GPT's own server alone: ai.settings_from.)"""
        key = self.key.text().strip()
        return bool(key) and key == self._stored_key

    def _form_key_origin(self):
        """The server the key on the form may go to (ai.KEY_ORIGIN): the saved key's own,
        or the form's address's where it was said here that it may go there. None for a
        key typed here, which goes with the address it was typed beside."""
        if not self._keeps_the_saved_key():
            return None
        address = ai.origin_of(self._address())
        return address if address in self._key_may_go else self._key_origin

    def _ask_about_the_key(self, saving: bool = False) -> None:
        """Ask once a dialog, for an address on another server than the saved key was
        entered for, whether the key may go there, No the default: before anything would
        send it - Test, List Models and Check Attestation, which send it at once, ask
        even about a server said No to at an earlier Save; Save itself (`saving`) does
        not ask about that one again (ai.SETTING_KEY_DECLINED): a question asked at
        every Save is soon answered Yes without reading. With No the key isn't sent
        there, and says why where it would have been."""
        if not self._keeps_the_saved_key():
            return
        there = ai.origin_of(self._address())
        if not there or there == self._key_origin or there in self._key_may_go \
                or there in self._key_may_not_go:
            return
        if saving and there == self._key_declined:
            return
        self._ask_whether_the_key_may_go(there)

    def _ask_whether_the_key_may_go(self, there: str) -> bool:
        """The question about the saved key and the server `there`, No the default; the
        answer kept for the dialog. True on Yes."""
        shown, entered = ai.shown_origin(there), ai.shown_origin(self._key_origin or "")
        answer = plain.question(
            self, "Use the saved key?",
            f"Use the saved key with {shown}?\n\n"
            f"The {ai.KEY_NAMES[self._key_slot]} API key kept in the keyring was entered for "
            f"{entered or 'a server the keyring does not name'}. Yes sends it to {shown} "
            f"from now on, with everything sent to the AI. No keeps it for "
            f"{entered or 'that server'}: nothing is sent to {shown} with it, and you can "
            f"paste the key for {shown} in its place.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer == QMessageBox.Yes:
            self._key_may_go.add(there)
            self._key_may_not_go.discard(there)
            return True
        self._key_may_not_go.add(there)
        self._key_may_go.discard(there)
        return False

    def _held_back_row(self):
        """Under the key, while the saved key is held back from the server the saved
        endpoint names (as Settings opened): where it is for, where it isn't sent,
        whether you said No, and Ask again… (Save keeps the answer). None when it isn't
        held back."""
        if not self._stored_key or not self._key_origin:
            return None
        there = ai.origin_of(self._app_settings.get(ai.SETTING_BASE_URL)
                             or ai.DEFAULT_BASE_URL)
        held = [entry for entry in self._plain_at_open if entry not in self._plain_confirmed]
        if there and there == self._key_origin and held:
            return self._in_clear_row(held[0])
        if not there or there == self._key_origin:
            return None
        self.held_back = QLabel(self._held_back_words(there))
        self.held_back.setObjectName("warningLabel")
        self.held_back.setWordWrap(True)
        self.held_back.setTextFormat(Qt.PlainText)
        self.ask_again = QPushButton("Ask again…")
        self.ask_again.clicked.connect(lambda: self._ask_again(there))
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self.held_back, 1)
        row.addWidget(self.ask_again, 0, Qt.AlignTop)
        return row

    def _in_clear_row(self, entry: tuple[str, str]):
        """Under the key, while it is held back from plain http to another machine as
        Settings opened: where, and Confirm…, which asks (No the default; Save keeps
        the answer)."""
        there = entry[1]
        words = (f"The saved key would go over plain http, unencrypted, to "
                 f"{ai.shown_origin(there)}, another machine on your network, so it isn't "
                 f"sent until you confirm it (or use the server's https:// address).")
        self.held_back = QLabel(words)
        self.held_back.setObjectName("warningLabel")
        self.held_back.setWordWrap(True)
        self.held_back.setTextFormat(Qt.PlainText)
        self.confirm_in_clear = QPushButton("Confirm…")

        def confirm() -> None:
            before = list(self._plain_allowed)
            if self._may_send_plain(self.settings(), "Save keeps your answer.", held_too=True) \
                    and self._plain_allowed != before:
                self.held_back.setText(f"With Save, the saved key is sent over plain http to "
                                       f"{ai.shown_origin(there)}.")

        self.confirm_in_clear.clicked.connect(confirm)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self.held_back, 1)
        row.addWidget(self.confirm_in_clear, 0, Qt.AlignTop)
        return row

    def _held_back_words(self, there: str) -> str:
        said_no = (" You said No to sending it there." if there == self._key_declined
                   else "")
        return (f"The saved key is for {ai.shown_origin(self._key_origin)}, not sent to "
                f"{ai.shown_origin(there)}.{said_no} To use a key there, paste it above, "
                f"or ask again.")

    def _ask_again(self, there: str) -> None:
        if self._ask_whether_the_key_may_go(there):
            self.held_back.setText(f"With Save, the saved key goes to "
                                   f"{ai.shown_origin(there)}.")
        else:
            self.held_back.setText(self._held_back_words(there))

    @staticmethod
    def _in_the_clear(settings: dict, key: str) -> list[tuple[str, str]]:
        """(the key, its server) where `settings` would send `key`, the AI key, over plain
        http to another machine on the local network (netguard.in_the_clear), confirmed
        for it or not, else [] (one held back from a moved address goes nowhere)."""
        address = settings.get(ai.SETTING_BASE_URL) or ""
        if settings.get(ai.KEY_HELD) and not settings.get(ai.KEY_IN_CLEAR_HELD):
            return []
        if key and (settings.get(ai.SETTING_KEY) or settings.get(ai.KEY_IN_CLEAR_HELD)) \
                and netguard.in_the_clear(address):
            return [(key, ai.origin_of(address))]
        return []

    def _may_send_plain(self, settings: dict, after_no: str, held_too: bool = False) -> bool:
        """Before the AI key goes over plain http to another machine on the local
        network, where anyone on it can read it (a café's or a hotel's Wi-Fi too):
        asked first, No the default, as every DA app asks (SealedLore's
        keys_in_the_clear, the DA apps' security standard, Nov 2026). Not once confirmed
        for it, nor once said Yes to in this dialog, nor (unless `held_too`) when it was
        set to go there as Settings opened: then it is held back, shown with Confirm…,
        and Save doesn't ask again. To this computer itself it never leaves it. True
        unless No."""
        known = self._plain_confirmed + self._plain_allowed
        if not held_too:
            known = known + self._plain_at_open
        new = [entry for entry in self._in_the_clear(settings, self.key.text().strip())
               if entry not in known]
        if not new:
            return True
        shown = ai.shown_origin(new[0][1])
        answer = plain.question(
            self, "Send the key unencrypted?",
            f"The {ai.KEY_NAMES[self._shown_provider]} API key goes over plain http, which "
            f"isn't encrypted, to "
            f"another machine on your network: {shown}.\n\nAnyone on that network can "
            f"read it on the way, a café's or a hotel's Wi-Fi included. (A key sent to "
            f"this computer itself, localhost or 127.0.0.1, never leaves it.)\n\n"
            f"Send it unencrypted? {after_no}",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer != QMessageBox.Yes:
            return False
        self._plain_allowed += new
        return True

    def accept(self) -> None:
        if self._shown_provider == ai.OTHER and not self.base_url.text().strip():
            self.tabs.setCurrentIndex(TAB_AI)
            self.status.setText("Not saved: set the service's address under Endpoint.")
            return
        problem = ai.endpoint_problem(self.base_url.text().strip() or ai.DEFAULT_BASE_URL)
        if problem:
            self.tabs.setCurrentIndex(TAB_AI)
            self.status.setText(f"Not saved: {problem}")
            return
        private_model = self.private_model.currentText().strip() or ai.DEFAULT_PRIVATE_MODEL
        if self.privacy_mode_check.isChecked() and \
                not levels.privacy_at(private_model, self._address()).private:
            self.tabs.setCurrentIndex(TAB_PRIVACY)
            self._tone(self.attest_status, f"Not saved: {private_model} runs in no attested "
                       f"enclave at {ai.service_name(self._address())}. Choose a private/ or "
                       f"TEE/ model on Nano-GPT, any of Tinfoil's, or a -TEE one on Chutes.",
                       "error")
            return
        key = self.key.text().strip()
        self._typed[self._shown_provider] = self.key.text()
        for provider, typed in self._typed.items():
            unfit = netguard.key_problem(typed.strip(), ai.PROVIDERS[provider])
            if unfit:
                self.tabs.setCurrentIndex(TAB_AI)
                self.status.setText(f"Not saved: {unfit}")
                return
        self._ask_about_the_key(saving=True)
        if not self._may_send_plain(self.settings(), "No goes back to Settings, to change "
                                                     "the endpoint or the key."):
            self.tabs.setCurrentIndex(TAB_AI)
            self.status.setText("Not saved: the key would have gone unencrypted. Change "
                                "the endpoint or the key, or Save again and choose Yes.")
            return
        there = ai.origin_of(self._address())
        declined = self._key_declined
        try:
            self._store_other_providers_keys()
        except Exception as e:  # noqa: BLE001 - keyring locked or unavailable
            self.status.setText(f"Couldn't keep the key in the keyring: {e}")
            return
        if key != self._stored_key:
            try:
                # Kept for the server it was entered beside, and sent only there: in its
                # provider's own entry, or that entry forgotten when emptied.
                ai.store_key(key, self._address(),
                             None if key else self._key_slot)
            except Exception as e:  # noqa: BLE001 - keyring locked or unavailable
                self.status.setText(f"Couldn't keep the key in the keyring: {e}")
                return
            declined = ""                   # a key entered for that server lets the No go
        else:
            if self._keeps_the_saved_key() and there in self._key_may_go \
                    and there != self._key_origin:
                try:
                    ai.let_key_go_to(self._address(), self._key_slot)
                except Exception as e:  # noqa: BLE001 - keyring locked or unavailable
                    self.status.setText(f"Couldn't keep in the keyring where the key may "
                                        f"go: {e}")
                    return
                declined = ""
            elif self._keeps_the_saved_key() and there in self._key_may_not_go:
                declined = there            # not asked again at the next Save
            elif there == self._key_origin or not self._stored_key:
                declined = ""               # the address back, or no key: nothing held
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
        if declined:
            settings[ai.SETTING_KEY_DECLINED] = declined
        else:
            settings.pop(ai.SETTING_KEY_DECLINED, None)
        in_clear = self._form_in_clear()
        if in_clear:
            settings[ai.SETTING_KEY_IN_CLEAR] = in_clear
        else:
            settings.pop(ai.SETTING_KEY_IN_CLEAR, None)
        settings[ai.SETTING_MODEL] = self.model.currentText().strip() or ai.DEFAULT_MODEL
        settings[ai.SETTING_BASE_URL] = self.base_url.text().strip() or ai.DEFAULT_BASE_URL
        settings[ai.SETTING_FRAMES] = self.frames.value()
        settings[ai.SETTING_SEARCH] = self.search.currentData()
        settings[ai.SETTING_PRIVACY_MODE] = self.privacy_mode_check.isChecked()
        settings[ai.SETTING_PRIVATE_MODEL] = private_model
        settings[ai.SETTING_STRICT] = self.strict_check.isChecked()
        settings[ai.SETTING_NAME_UPSTREAMS] = self.upstreams_check.isChecked()
        for choice in privacy.CHOICES:
            privacy.set_allowed(settings, choice, self.allowed(choice))
        store.save_app_settings(settings)
        if settings[ai.SETTING_PRIVACY_MODE] and levels.privacy_at(
                private_model, self._address()).service in (levels.NANOGPT_PRIVATE,
                                                            levels.TINFOIL):
            sealed.check()      # known before the first request needs it
        network_status.settings_changed()
        super().accept()

    def _store_other_providers_keys(self) -> None:
        """The keys typed here for providers not shown now, each kept for its own preset
        address in its own entry (another service's has no address but the one shown, so
        only the shown one's is kept). Raises when the keyring can't be written."""
        for provider, typed in self._typed.items():
            if provider in (self._shown_provider, ai.OTHER):
                continue
            key, _origin, slot = self._saved[provider]
            typed = typed.strip()
            if typed == key:
                continue
            if typed:
                ai.store_key(typed, ai.PROVIDER_ADDRESSES[provider], provider)
            elif slot == provider:
                ai.store_key("", ai.PROVIDER_ADDRESSES[provider], provider)

    # --- asking Nano-GPT ---------------------------------------------------

    def fetch_models(self) -> None:
        self._ask_about_the_key()
        settings = self.settings()
        if not ai.allowed(settings):
            self.status.setText(AI_OFF_HERE)
            return
        if not self._may_send_plain(settings, NOTHING_SENT, held_too=True):
            self.status.setText("Nothing was sent.")
            return
        settings = self.settings()          # a Yes just given goes with it
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
        """Under the model: its name and price where the provider's list said, and how
        private it is at this endpoint, at one of three levels (ai.model_privacy)."""
        model = self._models.get(model_id.strip())
        note = ai.RECOMMENDED.get(model_id.strip(), "")
        parts = []
        if model:
            parts.append(model["name"])
            if model.get("price"):
                parts.append(model["price"])
        if note:
            parts.append(note)
        if hasattr(self, "base_url") and model_id.strip():
            settings = {ai.SETTING_BASE_URL: self._address()}
            label, meaning = ai.model_privacy(settings, model_id.strip())
            parts.append(label)
            self.model_note.setToolTip(tooltip(meaning))
        self.model_note.setText(" · ".join(dict.fromkeys(p for p in parts if p)))

    def _endpoint_host(self) -> str:
        url = self.base_url.text().strip() or ai.DEFAULT_BASE_URL
        return outbound.host_of(url) or url

    def _update_search(self) -> None:
        self._describe_model(self.model.currentText())
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
        self._ask_about_the_key()
        settings = self.settings()
        if not ai.allowed(settings):
            self.status.setText(AI_OFF_HERE)
            return
        if not self._may_send_plain(settings, NOTHING_SENT, held_too=True):
            self.status.setText("Nothing was sent.")
            return
        settings = self.settings()          # a Yes just given goes with it
        if not settings.get(ai.SETTING_KEY):
            self.status.setText(settings.get(ai.KEY_HELD) or "Paste an API key first.")
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
            # What was attested, what each shortfall costs, and what the reply's mark
            # means (receipt_tip).
            tip = "\n\n".join(part for part in (
                private_ai.what_was_attested(session.attestation, clear=False),
                *private_ai.shortfall_tips(session.attestation),
                private_ai.receipt_tip(session.last_receipt(),
                                       private_ai.relay_of(session))) if part)
        self.status.setText(text)
        self.status.setToolTip(tooltip(tip) if tip else "")

    def check_attestation(self) -> None:
        self._ask_about_the_key()
        settings = self.settings()
        if not self._may_send_plain(settings, NOTHING_SENT, held_too=True):
            self._tone(self.attest_status, "Nothing was sent.", "")
            return
        settings = self.settings()          # a Yes just given goes with it
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
        # What each shortfall costs, the same words in every DA app (shortfall_tips).
        tips = private_ai.shortfall_tips(session.attestation)
        self.attest_status.setToolTip(tooltip("\n\n".join(tips)) if tips else "")

    def _attestation_failed(self, message: str) -> None:
        self._update_privacy()
        self._tone(self.attest_status, f"Refused: {message}", "error")
        self.attest_status.setToolTip("")

    def fetch_private_models(self) -> None:
        settings = self.settings()
        self.private_list_button.setEnabled(False)
        self._tone(self.private_model_note,
                   f"Asking {ai.service_name(self._address())} for its private models…", "")
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
