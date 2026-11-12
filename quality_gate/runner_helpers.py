"""Shared temporary directory and check-error reporting helpers."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from .contracts import CheckResult, Finding, Status, load_manifest, redact
from .runner_context import repository_root
from .runner_errors import QualityGateError

EXIT_UNCHECKED = 2


def temporary_directory(root: Path) -> tempfile.TemporaryDirectory[str]:
	return tempfile.TemporaryDirectory(prefix="quality-gate-")


def _error_result(error: QualityGateError) -> CheckResult:
	status = Status.UNCHECKED if error.exit_code == EXIT_UNCHECKED else Status.FAILED
	return CheckResult(
		check_id=error.check_id,
		status=status,
		summary=redact(str(error)),
		findings=(Finding(message=redact(str(error))),),
		recovery_action=error.recovery_action,
	)


def emit(message: str) -> None:
	sys.stdout.write(f"{message}\n")


def _progress(message: str) -> None:
	sys.stderr.write(f"QUALITY GATE: {message}\n")
	sys.stderr.flush()


MANIFEST_NAME = "quality-gate.toml"


def validate(root: Path | None = None) -> None:
	actual_root = repository_root(root)
	load_manifest(actual_root)
	emit(f"QUALITY GATE VALID (schema 2): {actual_root / MANIFEST_NAME}")
