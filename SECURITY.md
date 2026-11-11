# Security policy

## Reporting a vulnerability

Please email **jason@digitalarchon.com.au** with the details and, if you can, steps to
reproduce. Don't open a public issue or pull request for a vulnerability.

You'll get a reply as soon as I can manage. This is a one-person project with no bug bounty,
but reports are taken seriously and credited in the fix unless you'd rather not be named.

## Supported versions

DA Media Browser is in beta. Only the latest release (or `main`) gets fixes.

## What's in scope

Anything that breaks one of DA Media Browser's promises, for example:

- anything about your videos reaching MusicBrainz, the Cover Art Archive, setlist.fm or the AI
  while Settings → Privacy doesn't allow it, or anything about a video or library marked
  Private reaching any of them at all;
- in Privacy Mode, anything reaching a model whose enclave wasn't attested first, or an
  attested `TEE/` model when a sealed `private/` one was chosen, or the ordinary model at all;
  a `private/` model's request leaving this computer unsealed; a web search being made;
- flaws in the attestation checks: Intel's TDX quote verification, NVIDIA's GPU verdict, a
  `TEE/` reply's check against its signed record and RedPill's receipt, or the sealed client's
  check of Tinfoil's enclave and its release; a mark saying more than its check showed, or
  naming what was attested as more than it was;
- anything reaching RedPill's `api.redpill.ai` while *Name the provider behind each TEE reply*
  is off, or anything but a reply's session id when it is on;
- a request going over plain http anywhere but this computer, its local network or an onion
  service, a redirect followed from https to plain http or onto your own network, an API key
  carried to a server it wasn't sent to, or a certificate left unchecked;
- an API key, or a password written into an address, reaching `settings.json`, a library
  file, a backup, an export or a message on screen;
- Help → What Was Sent leaving out a request the app made, or saying it travelled more safely
  than it did;
- anything the app writes landing inside a library, or a media file being changed, moved or
  deleted;
- a crafted video, disc, catalog or library file doing more than it should when it's read.

Weaknesses in upstream projects (NanoGPT, Tinfoil, MusicBrainz, setlist.fm, mpv, FFmpeg,
libbluray, Qt and so on) should go to those projects, though a heads-up is welcome if DA Media
Browser can mitigate them.
