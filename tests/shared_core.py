# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later
"""What the tests ported from DA Assistant's (test_chutes, test_tinfoil_direct,
test_anthropic) read a stream with: DA Assistant's LLMClient._consume, which this app has
as ai._consume. Its window shows a reply as it comes (`on_piece`); this app shows one only
once it is whole, so a piece is "shown" only when the whole stream was read."""

from mediabrowser.core import ai


class LLMClient:
    @staticmethod
    def _consume(lines, on_piece=None, _on_reasoning=None, _extra=None):
        result = ai._consume(lines)
        if on_piece is not None and result["text"]:
            on_piece(result["text"], result["text"])
        return result
