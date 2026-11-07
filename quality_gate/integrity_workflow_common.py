"""Shared workflow finding construction."""

from __future__ import annotations

from .contracts import Finding


def _workflow_finding(path: str, message: str, action: str) -> Finding:
	return Finding(path, message=message, action=action)
