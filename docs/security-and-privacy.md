# Security and privacy

How DA Media Browser handles your security and privacy: what leaves your computer,
where it goes and when, what is kept on it, and what the app cannot protect you from.
It describes the app as of 0.23.0. The [README](../README.md) says how to use each
part; this document explains what each part does with your data, and points to the
README for the details.

## In short

What leaves this computer:

- **Only what Settings → Privacy allows.** There are five switches — MusicBrainz, the
  Cover Art Archive, setlist.fm, the AI and the AI's web search — and every one is off
  until you tick it. Each place in the app that sends checks its own switch right where
  it would send, so a path through the app that forgot to ask still sends nothing.
- **Never anything about a video or library marked Private.** Right-click it: the app
  still scans, plays and measures it, reads its own chapters and works on it here, but
  MusicBrainz, the Cover Art Archive, setlist.fm and the AI are sent nothing about it.
- **To the AI, when you press a button that asks it**: a video's chapter times, its file
  and folder names, a tracklist, and frames from it — a disc menu's pictures for **Read
  the Menu**; the titles for **Romanise Titles**. With [Privacy Mode](../README.md#privacy-mode)
  on, only to an attested private model.
- **In Privacy Mode, attestation requests**, to nano-gpt and to the services that vouch
  for the hardware: Intel and NVIDIA, and for end-to-end encrypted models Tinfoil's and
  Sigstore's servers. None of them is sent anything about your videos.
- **Only with Name the provider behind each TEE reply on**: the id of a reply's session,
  to RedPill (`api.redpill.ai`).

What does not:

- **Nothing is sent because a window opened.** Every control that sends says what and
  to whom before you press it; steps that run here say "on this computer". The one thing
  that goes by itself is a cover: once a video has been matched to a MusicBrainz release,
  its sleeve is fetched in the background if cover art is allowed.
- **No telemetry, no crash reports, no update check.** The app does not call home.
- **An API key goes only to its own service.** Nano-GPT's to the AI endpoint, setlist.fm's
  to setlist.fm, and never to anyone else.
- **Your videos stay where they are.** The app only reads media files — ffprobe, ffmpeg,
  libbluray and mpv all only read — and writes nothing into a library folder.
- **What Was Sent is never written to disk.** Help → What Was Sent lists every request as
  it is made, in memory, and is gone when the app closes.

## The services it contacts

Every one of these is reached over https. Only the AI endpoint may be plain http, and
only on this computer, your local network or a Tor onion service (see
[The network rules](#the-network-rules)).

| Service | When | What it is sent | What it is not sent |
|---|---|---|---|
| MusicBrainz (`musicbrainz.org`) | **Search MusicBrainz** in Detect Chapters, **Just Figure It Out**, and Identify Library's MusicBrainz method — with its switch on | A search, usually the video's file or folder name, and the ids of the releases it finds; the app's name, version and project page, as MusicBrainz asks every application to say | Frames, chapters you named, any key |
| The Cover Art Archive (`coverartarchive.org`, and the Internet Archive, where it keeps the pictures) | In the background, for a video matched to a MusicBrainz release that has no cover beside it or inside it — with its switch on | The release's id, and the same name, version and project page | Anything else about the video. A video marked Private is never looked up, not even for its cover |
| setlist.fm (`api.setlist.fm`) | **Search setlist.fm**, **Just Figure It Out**, Identify Library's setlist.fm method, and **Test** on the setlist.fm tab — with its switch on | An artist's name and a date, year or place, usually from the video's name; your setlist.fm key | Frames, the Nano-GPT key |
| The AI endpoint: Nano-GPT (`nano-gpt.com`), or another OpenAI-compatible service set on the AI tab | **Ask AI**, **Read the Menu** and **Just Figure It Out** in Detect Chapters, Identify Library's AI methods (and a video's right-click Identify), **Romanise Titles**, and **Test** on the AI tab — with the AI's switch on | Chapter times, file and folder names, tracklists and frames (or the titles to romanise), the model's name, and the Nano-GPT key | Anything for a video marked Private. In Privacy Mode: anything at all to the ordinary model |
| The AI endpoint's model lists | **List Models** and **List Private Models** on the AI tab and the Privacy tab | For List Models, the key, if there is one, so that the prices are yours | For List Private Models, nothing: nano-gpt's lists of its private and TEE models are public, and fetched with no key |
| A web search, which Nano-GPT runs for the model (Kagi by default, Perplexity or LinkUp) | When the AI is asked to look a show up, with the web search's switch on, on Nano-GPT's endpoint, outside Privacy Mode | Nano-GPT has the search provider search the web about the show — the artist, tour and venue | Never in Privacy Mode: Nano-GPT would have to read the request to search for it |
| nano-gpt's TEE checks (Privacy Mode, `TEE/` models) | Before an operation's first request, again before a request once that is 15 minutes old, on **Check Attestation**, and after each reply | For an attestation, the model's name and a random nonce — one for the model's own report and one for RedPill's gateway's; for a reply's check, its id or receipt id, and the route its headers named. The Nano-GPT key | Your videos: those go with the request itself |
| nano-gpt's Private Mode relay (Privacy Mode, `private/` models) | Before each request (a preflight that names the model), and with it | The request sealed end to end; nano-gpt sees your account, the model, when, how much and the cost | The text: only the attested enclave can open it |
| Intel (`api.trustedservices.intel.com`, `certificates.trustedservices.intel.com`) | When a `TEE/` model is attested, and by Tinfoil's client for an Intel TDX enclave | Which platform the quote comes from, to fetch Intel's signed TCB information, revocation lists and quoting enclave identity | No key, nothing about your videos |
| NVIDIA (`nras.attestation.nvidia.com`) | When a `TEE/` model with GPUs is attested | The GPU evidence nano-gpt relayed, with the app's own nonce; and a request for NVIDIA's public keys | No key, nothing about your videos |
| Tinfoil (`github-proxy.tinfoil.sh`, `kds-proxy.tinfoil.sh`) and Sigstore (`tuf-repo-cdn.sigstore.dev`) | When a `private/` enclave is attested: before an operation's first request, again once its attestation is five minutes old, and on **Check Attestation** | Requests for the router's release and its Sigstore record, for AMD's certificate for the enclave's processor, and for Sigstore's trust root | No key, nothing about your videos. The enclave's own report comes through nano-gpt's relay |
| RedPill (`api.redpill.ai`) | Only with **Name the provider behind each TEE reply** on (Settings → Privacy, off by default), after a `TEE/` reply whose signed receipt holds, at most once a session | The session's id, which the receipt names | Your videos and any key. It goes directly, not through Nano-GPT: RedPill sees this computer's internet address, or the proxy's, and that one of its models was used around then. See [Naming the provider](#naming-the-provider-behind-a-tee-reply) |
| Your web browser | When you click a link (setlist.fm's credit, About's) | The link, handed to the browser | A link to plain http on the internet is not opened at all |

Every request that carries anything about your videos is listed on Help → What Was Sent
as it is made, and so is each attestation, as one entry for all the services it asked,
and each lookup of RedPill's records; the model lists, which carry nothing of yours, are
not. The status bar's right-hand corner says where things can go now (see
[Seeing where things go](#seeing-where-things-go)).

## The network rules

- **https, or nothing.** An address is used only if it is https, or if it never leaves
  this computer or its local network (`localhost`, `127.x`, `10.x`, `172.16`-`31.x`,
  `192.168.x`, link-local and IPv6 unique local addresses, `.local` names), or if it is
  a Tor onion service. Any other plain http address is refused, with the reason, before
  a byte is sent. It is never rewritten to https for you, and certificates are always
  checked.
- **Plain http on your own network is not encrypted.** The app allows it, because that
  is how a model server on this computer or the one next to it is reached, and it does
  not ask first: the key and everything sent cross that network as they are, readable
  by anyone on it. Give such a server an https address if it can have one.
- **An address is judged as written**, not by looking up where its name points. One
  with a space, a backslash or a control character in it is refused outright, and so is
  a plain http host with a `%` escape in it, since the parsers involved disagree on
  where such a host ends.
- **An address with a user name or password written into it** (`https://alice:secret@host/v1`)
  is refused, in words that repeat neither: the app sends neither, so it says so rather
  than send the request without them. The key goes in its own field.
- **Redirects.** Nothing that carries a key or what you send follows one: the AI's
  requests, model lists included, key or none; Privacy Mode's checks with the key, and
  its sealed requests; setlist.fm's; MusicBrainz's, whose searches name your videos; and
  NVIDIA's attestation service, which is sent the GPUs' evidence. A redirect would send
  the request on to wherever the server pointed, so it is an error saying where, without
  a password, and nothing is sent there; if the AI endpoint has moved, its new address
  goes in Settings → AI. Fetches that carry none of that — the Cover Art Archive's
  pictures, which it sends on to the Internet Archive, Intel's certificates and
  revocation lists, NVIDIA's signing keys, RedPill's session records and Tinfoil's
  client's own fetches — follow one only to an address that could have been used in the
  first place: never from https to plain http, and never from a server on the internet
  onto this computer or its network.
- **The environment's proxy** (`https_proxy`, `http_proxy`, `ALL_PROXY`) carries every
  request the app makes — the AI's, the lookups, the covers, the attestation services,
  RedPill's session records, Tinfoil's client — except to this computer and its own
  network, which are always reached directly. The app has no proxy setting of its own.
  A proxy the app's requests can't go through (SOCKS: use Tor's `HTTPTunnelPort`, an
  http proxy, instead) means nothing is sent, rather than sent around it. Plain http to
  an onion service goes only through a proxy on this computer. A proxy's own user name
  and password, if the environment gives one, go to that proxy; to one elsewhere over
  plain http, they cross the network as they are.

## Keys and passwords

- **The Nano-GPT and setlist.fm keys are kept in your desktop's keyring** (GNOME Keyring,
  or KWallet), under `da-media-browser`, never in a file. `NANOGPT_API_KEY` and
  `SETLISTFM_API_KEY` in the environment take their place. A key an older version left in
  `settings.json` is never read back, and goes the next time settings are saved. With no
  keyring, Settings says so, and the environment's key is the only one there is.
- **A password written into an address is never kept.** Settings won't save an AI
  endpoint whose address has one, and `settings.json` is always written without it,
  whichever window saves; the user name, if one is left, is still refused, and says how
  to put it right.
- **A network share's password belongs to your desktop's keyring.** The app connects a
  share through the desktop's own tools (`gio`, or KDE's), and keeps the share's address
  with the library without its password.
- **No message shows a key or a password.** Every message that names an address names
  it without its user name and password, and a key with a space, a line break or a
  character no key has in it is refused without being repeated.
- **Each key goes only to its own service.** Nano-GPT's goes to the AI endpoint alone —
  the requests, its model list, the TEE checks and Private Mode's relay — and never to
  Intel, NVIDIA, Tinfoil, Sigstore or RedPill. setlist.fm's goes to setlist.fm alone.
  MusicBrainz and the Cover Art Archive are sent no key.

## AI models

### Ordinary models

The endpoint on the AI tab receives everything in the request, and can read it, and so
can whoever it passes it on to: for Nano-GPT, the provider that runs the model. What
they keep is up to their own policies. A `private/` model is never sent this way: the
plain path refuses one however it is written.

### Privacy Mode

[Privacy Mode](../README.md#privacy-mode) (Settings → Privacy, under *Send videos to the
AI*) sends the AI's requests only to the private model set beside it, attested before
anything is sent to it. An operation's privacy is fixed when it starts — a Detect
Chapters window opened, an Identify Library run — and only ever tightens: switch Privacy
Mode on, or the AI off, and a run already going sends nothing more to the ordinary
model. There is no web search in Privacy Mode, and if the private model cannot be used,
nothing is sent: it never falls back to the ordinary one.

### End-to-end encrypted models (`private/`)

nano-gpt's Private Mode models run in enclaves operated by Tinfoil. Before anything is
sent, the enclave is attested: its hardware report (AMD SEV-SNP or Intel TDX) against
the vendor's root, and its software release against Sigstore's record of Tinfoil's
releases (one older than the latest is named). Each request is then sealed on this
computer to a key only that enclave holds, and the reply comes back sealed the same way.
nano-gpt relays ciphertext: it sees your account, the model, when, how much and the cost,
never the frames or the names.

- The enclave is attested again before a request once its attestation is five minutes
  old (time the computer spent asleep counts), and when it says its key has changed.
- Nothing is ever sent unsealed, and nothing falls back to the plain endpoint: the
  transport under the sealing refuses anything that wasn't sealed. Only a successful
  reply that came back sealed is opened; anything else is refused unread.
- No code from nano-gpt runs: the app uses Tinfoil's own client, pinned by hash, which
  the AppImage carries and `tools/install-private-mode.sh` installs from source.
- Before a `private/` model is first used, it is looked up in nano-gpt's public list of
  them, with no key, so that a retired model is never sent anything.

### TEE models (`TEE/`)

A `TEE/` model runs in a trusted execution environment, but **what is sent passes
nano-gpt's servers in the clear**, so nano-gpt can read it: the frames, the names, the
tracklist. Attestation shows where the request is answered, not that nano-gpt cannot
read it. For a request that should be sealed end to end, choose a `private/` model.

**Before anything is sent**, the model is attested with a fresh random nonce. Its Intel
TDX quote is verified the way Intel's own Quote Verification Library does it, up to
Intel's root, which is pinned in the app; the GPU evidence goes to NVIDIA's attestation
service with the app's own nonce, and NVIDIA's signed verdict is checked in turn; and
RedPill's gateway's own report is asked for beside the model's, with a nonce of its own.
Anything forged, revoked, for another nonce, from a debug machine, or Intel's
information arriving unreadable, is refused, and nothing is sent. What could not be
checked, or is not current, is shown in amber.

**What is attested** depends on who serves the model, and Check Attestation, What Was
Sent and the line after an answer say which, first of all, with what that leaves to
someone's word:

- **RedPill's gateway** (most `TEE/` models, which RedPill serves): it reads **TEE gateway
  attested**. The gateway passes each request on to the machine that runs the model,
  and that machine is not attested by this app: that it is a verified TEE is the
  gateway's word, in each reply's signed receipt. The GPUs NVIDIA vouched for are not
  tied to the gateway's quote, so they may be another machine's: *GPUs not tied to the
  quote*. A model attested through the gateway alone — its own report relayed for a
  nonce that isn't the app's, or timing out — has no GPU evidence of its own either.
- **Chutes' instances** (models Chutes serves itself): every instance that could answer
  is attested, each tied to its own GPUs. They sign no replies, so nothing shows which
  instance answered a request. Some models Chutes serves first are answered by RedPill's
  gateway now and then, so the gateway is attested too.
- **One enclave** not known to be either: the one whose key signs the replies.

**What attestation does not prove**: which software runs (no measurement in a quote is
compared with a published one); what was sent (a reply's record hashes the request as
nano-gpt passed it on); the model's own machine, where RedPill's gateway answers; and
that the GPUs are the attested machine's.

**Again every 15 minutes**: a model is attested again before the next request once its
attestation is 15 minutes old, time asleep included. While a model is being attested, a
request to it waits; while it is refused, nothing is sent to it.

**Each reply is checked**, where its provider signs one. The request is streamed, every
byte of the reply is hashed as it arrives, and the reply is checked against the record
nano-gpt keeps of it, signed by the attested key:

- *✓ TEE, verified*: the attested key signed the record of the exact bytes that came,
  and RedPill's receipt for it, where one came, holds.
- *TEE, signed, text not compared*: signed by the attested key, but the bytes could not
  be compared (nano-gpt rebuilds some models' streams; a reply cut short has no whole).
- *⚠ TEE, check failed*: signed by another key, for other bytes, or its receipt does not
  hold.
- *⚠ TEE, not verified*: no record could be read — Chutes signs none, or the check could
  not be made. A reply of a model attested through RedPill's gateway alone also reads
  this unless its receipt holds, since only the receipt says the model's own enclave
  answered.

The mark is shown after **Test** on the AI tab and under Detect Chapters' answer, whose
tooltips say what it means and what is someone's word, and on Help → What Was Sent.

**What was sent is marked too.** On What Was Sent, a request to a `TEE/` model reads
*received by the attested TEE* only once the record of its reply, signed by the attested
key, shows it (verified, or signed). Otherwise it reads *⚠ not shown to reach the
attested enclave*: nano-gpt routes each request, Chutes signs no replies, and a model may
be served elsewhere when its provider is busy.

**Strict** (Settings → Privacy, off by default) refuses any enclave that proved less
than it could — anything that would be shown in amber — and sends nothing to it. As
nano-gpt's models are in Nov 2026, that leaves `private/` models and the `TEE/` models
Chutes alone serves. After a reply whose check failed, or one the attested keys were to
sign that fell short of verified, Strict attests its model again before sending it
anything more, and the mark's tooltip says so.

### Naming the provider behind a TEE reply

**Name the provider behind each TEE reply** (Settings → Privacy, under Strict, off by
default) shows which provider RedPill's gateway passed a reply on to — Phala, Fortanix,
NEAR AI, 0G, Tinfoil's router — beside its mark: *✓ TEE, verified · run by Phala*.

- **What it costs you.** After a `TEE/` reply whose signed receipt holds, the app asks
  `api.redpill.ai` for its public record of the session the receipt names. That request
  goes to RedPill directly, not through Nano-GPT (through the environment's proxy, if one
  is set), so RedPill sees this computer's internet address, or the proxy's, and that one
  of its models was used around then — which Nano-GPT's relay otherwise keeps from it.
- **What it sends.** The session's id alone: never anything about your videos, never a
  key.
- **How much.** Each session's record is read once while the app runs (a session lasts
  about an hour), never more than 1 MB of it, and never for a model Chutes serves
  itself, whose records are megabytes each and change from one reply to the next.
- **What it is worth.** The record is believed only if its SHA-256 is the session id the
  attested gateway signed in the receipt, and the reply was served within the session's
  time. What it says is still RedPill's word: this app does not check that machine. A
  record that cannot be read or believed names nobody, and the tooltip says why; the
  reply's mark never changes for it.
- **When it applies.** Like the rest of Privacy Mode, it only ever tightens a run: a
  reply's provider is looked up only if the setting was on when the run began — a Detect
  Chapters window opened, an Identify Library run — and is still on at that reply.
  Switched off, nothing more is looked up, at once, in a run already going too; switched
  on, it applies from the next run. While it is on, the status bar and What Was Sent name
  RedPill among the places things can go, and each lookup is listed as it is made.

## Seeing where things go

- **The status bar's right-hand corner** says where things can go now — "Online:
  MusicBrainz · Covers · AI", "AI, sealed end to end" or "AI, TEE attested first" in
  Privacy Mode, "Nothing goes online" — and hovering it lists each place, with why one
  is off. Each time something is sent, it says where for a few seconds.
- **Help → What Was Sent** lists every request since the app started, as it is made: to
  whom, what it carried, how it travelled (in the clear over https, to a TEE model
  attested first, or sealed end to end), whether it went, and which window or run it was
  for — and for the AI, the words it was sent, the frames left out. An attestation is
  listed with what it attested. Above the list it says where things can go from now on.
  **Copy** puts the list on the clipboard; the list is kept in memory only.
- **Every control that sends says where first**: Detect Chapters' buttons, Identify
  Library's methods and its estimate, a video's right-click *Identify (Asks MusicBrainz
  and the AI)* or *Identify (On This Computer)*, Romanise Titles, and Settings' List
  Models, Test and Check Attestation.

## What is kept on this computer

| What | Where |
|---|---|
| Your libraries' records: chapters, names, what was matched | `~/.local/share/da-media-browser/libraries/` (or under `$XDG_DATA_HOME`), with a backup of a record before it is merged, moved, restored into or forgotten |
| Covers | `~/.local/share/da-media-browser/artwork/` |
| Settings | `~/.local/share/da-media-browser/settings.json` |
| The Nano-GPT and setlist.fm keys | your desktop's keyring, under `da-media-browser` |
| Tinfoil's client, when the app runs from source | `~/.local/share/da-media-browser/private-mode/` |
| Sigstore's trust root, once a `private/` model has been attested | Tinfoil's client keeps it in `sigstore-python`'s own folders, `~/.local/share/sigstore-python` and `~/.cache/sigstore-python` |
| Screenshots you take (`S`) | `Pictures/DA Media Browser` |
| Exports you make (catalogs, chapters, songs as audio) | where you choose — never inside a library folder |

The data folder is readable only by you (`0700`): it names every video in every library.
Every file the app writes, renames or deletes is checked first to be inside it, so even
a bug can't reach a video.

Kept in memory only, and gone when the app quits: What Was Sent, the frames taken from
a video, the audio levels and stage lighting measured, what each enclave proved, and
the RedPill session records read (64 at most). mpv's control socket is kept in your
session's own runtime folder, which no other user can enter, where there is one.

## Other protections

- **Your libraries live on a NAS, as most do.** Nothing reads a whole file or loads the
  network by itself: measuring a video's audio (**Show Audio Levels** in Manual Edit,
  Detect Chapters' own measuring, Just Figure It Out's) happens only when you ask, says
  what it is doing, and can be stopped. What runs by itself is cheap: a scan's look at
  each file's own information, and for a video with no cover found elsewhere, one frame
  from its middle.
- **Locked and Private.** A video or library marked Locked is changed by nothing — no
  renaming, no naming, no Identify, and a rescan keeps it as it is. One marked Private
  goes nowhere, as above.
- **Links** say on hover what they open, and open in your web browser only when you
  click them; one to plain http on the internet is not opened at all.
- **What nano-gpt's TEE gateway or Private Mode's relay writes** in an error is shown on
  one short line, without line breaks or characters you could not see, and never as the
  model's words: nothing vouches for it.
- **Builds can be checked.** Every AppImage can be rebuilt from its source and compared
  byte for byte with the one published, and everything in it, Tinfoil's client too, is
  pinned by hash; see [the README's AppImage section](../README.md#appimage).

## Limits

- **The AI endpoint sees what you send it**, except a `private/` model's relay, which
  sees ciphertext. What it keeps is up to its own policy.
- **A `TEE/` model's request passes nano-gpt in the clear.** Attestation shows genuine,
  current hardware holding a key bound to this app's nonce. It does not show which
  software runs, and where RedPill's gateway answers, the machine that runs the model is
  the gateway's word.
- **End-to-end encryption rests on the enclave**: on Tinfoil's software, measured and
  checked against its published release, and on AMD's or Intel's hardware and the keys
  they vouch for. nano-gpt still sees your account, and when, and how much, you send.
- **MusicBrainz, the Cover Art Archive and setlist.fm see what they are asked**, and this
  computer's internet address (or the proxy's), as any website does.
- **Plain http on your own network is not encrypted** (see
  [The network rules](#the-network-rules)).
- **Anything running as you on this computer** can read your libraries' records, the
  settings, the keyring once it is unlocked, and the app's memory.
- **Answers are written by language models**, and setlists and tracklists come from
  public databases: any of them can be wrong.

## Reporting a security problem

See [SECURITY.md](../SECURITY.md): how to report a vulnerability privately, and what is
in scope.
