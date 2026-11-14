"""Fixtures and helpers for the split feature tests."""

from __future__ import annotations

import hashlib
import os
import sys
import tempfile
import time
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

import quality_gate.distribution as distribution
from quality_gate.cli import main
from quality_gate.contracts import load_manifest
from quality_gate.distribution import DistributionError, PolicyCache
from quality_gate.launcher import prepare_bootstrap
from quality_gate.operation import GateOperation
from quality_gate.runner import EXIT_UNCHECKED
from quality_gate.runtime import (
	RuntimeManager,
	RuntimeUnavailable,
	runtime_fingerprint,
	runtime_identity,
)


@pytest.fixture
def runtime_root() -> Iterator[Path]:
	# Keep hashed cache paths below Windows' limit inside nested staged Gate workspaces.
	with tempfile.TemporaryDirectory(prefix="runtime-") as directory:
		yield Path(directory)


def _release(source: Path, version: str = "v2.0.0") -> None:
	wheel_name = f"quality_gate-{version.removeprefix('v')}-py3-none-any.whl"
	(source / wheel_name).write_bytes(b"wheel")
	(source / "policy.txt").write_text("shared policy\n", encoding="utf-8")
	(source / "ruff.exe").write_bytes(b"ruff")

	def digest(name: str) -> str:
		return hashlib.sha256((source / name).read_bytes()).hexdigest()

	wheel_digest = digest(wheel_name)
	policy_digest = digest("policy.txt")
	ruff_digest = digest("ruff.exe")
	(source / "release.toml").write_text(
		f"""[release]
version = "{version}"

[[release.files]]
path = "{wheel_name}"
sha256 = "{wheel_digest}"

[[release.files]]
path = "policy.txt"
sha256 = "{policy_digest}"
kind = "policy"

[[release.tools]]
name = "ruff"
version = "0.15.12"
path = "ruff.exe"
sha256 = "{ruff_digest}"
""",
		encoding="utf-8",
	)


__all__ = [
	"DistributionError",
	"EXIT_UNCHECKED",
	"GateOperation",
	"Iterator",
	"Path",
	"PolicyCache",
	"RuntimeManager",
	"RuntimeUnavailable",
	"_release",
	"contextmanager",
	"distribution",
	"hashlib",
	"load_manifest",
	"main",
	"os",
	"prepare_bootstrap",
	"pytest",
	"runtime_fingerprint",
	"runtime_identity",
	"runtime_root",
	"sys",
	"tempfile",
	"time",
	"zipfile",
]
