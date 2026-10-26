"""Provider test preflights that must run before test actions."""

from __future__ import annotations

import os
import shutil
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest
from _pytest.tmpdir import _mk_tmp

from quality_gate.release import ReleaseControllerError, _default_release_workspace
from quality_gate.temp_workspace import temporary_workspace


class _PreviewTempPathFactory:
	"""Predict pytest tmp_path names without reserving their directories."""

	def __init__(self, basetemp: Path) -> None:
		self.basetemp = basetemp
		self._next_indices: Counter[str] = Counter()

	def mktemp(self, basename: str, *, numbered: bool = True) -> Path:
		if not numbered:
			raise ValueError("the release-test preflight expects numbered tmp_path entries")
		index = self._next_indices[basename]
		while (self.basetemp / f"{basename}{index}").exists():
			index += 1
		self._next_indices[basename] = index + 1
		return self.basetemp / f"{basename}{index}"


def _preflight_release_test_paths(session: pytest.Session) -> None:
	"""Check the same nested temp path the release tests will use before collection ends."""
	release_test_file = Path(__file__).with_name("test_release.py").resolve()
	tmp_factory = _PreviewTempPathFactory(session.config._tmp_path_factory.getbasetemp())

	for item in session.items:
		if "tmp_path" not in item.fixturenames:
			continue
		candidate = _mk_tmp(SimpleNamespace(node=item), tmp_factory)
		if Path(item.path).resolve() != release_test_file:
			continue

		try:
			candidate.mkdir()
			try:
				with temporary_workspace(candidate):
					_default_release_workspace()
			finally:
				shutil.rmtree(candidate)
		except ReleaseControllerError as error:
			raise pytest.UsageError(
				"Provider release-test workspace preflight failed before test execution: "
				f"{error}"
			) from error
		except OSError as error:
			raise pytest.UsageError(
				"Provider release-test workspace preflight could not prepare its temporary "
				f"path {candidate}: {error}"
			) from error


def pytest_collection_finish(session: pytest.Session) -> None:
	"""Reject insufficient Windows release-test path headroom before any test runs."""
	if os.name == "nt":
		_preflight_release_test_paths(session)
