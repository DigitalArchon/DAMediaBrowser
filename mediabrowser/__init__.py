# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
# Privacy Mode's end-to-end encryption client, run from source, goes ahead of
# everything else on the path, before anything imports cryptography: see
# core/vendored.py.
from mediabrowser.core import vendored as _vendored

_vendored.activate()
