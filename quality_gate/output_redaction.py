"""Redact recognizable secrets before retaining public diagnostics."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
	from collections.abc import Iterable


def redact(text: str, secrets: Iterable[str] = ()) -> str:
	result = text
	tokens = {secret for secret in secrets if secret}
	for secret in tuple(tokens):
		tokens.update(secret[:length] for length in range(8, len(secret) + 1))
	for token in sorted(tokens, key=len, reverse=True):
		result = result.replace(token, "<REDACTED>")
	for pattern in (
		r"(?i)(?:ghp_|github_pat_|sk-|xox[baprs]-)[A-Za-z0-9_-]{8,}",
		r"(?i)(?:password|passwd|token|secret|api[_-]?key)\s*[=:]\s*[^\s,;]+",
	):
		result = re.sub(pattern, "<REDACTED>", result)
	return result
