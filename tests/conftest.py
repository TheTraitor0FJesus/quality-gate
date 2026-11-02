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

_RELEASE_CANDIDATE_PATH = pytest.StashKey[Path]()


@pytest.fixture
def release_candidate_path(
	request: pytest.FixtureRequest,
	tmp_path_factory: pytest.TempPathFactory,
) -> Path:
	try:
		return request.node.stash[_RELEASE_CANDIDATE_PATH]
	except KeyError:
		return tmp_path_factory.mktemp("candidate")


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
	"""Check release-test temp paths before collection ends."""
	release_test_file = Path(__file__).with_name("test_release.py").resolve()
	tmp_factory = _PreviewTempPathFactory(session.config._tmp_path_factory.getbasetemp())

	for item in session.items:
		uses_release_candidate_path = "release_candidate_path" in item.fixturenames
		if "tmp_path" not in item.fixturenames and not uses_release_candidate_path:
			continue
		if Path(item.path).resolve() != release_test_file:
			continue

		candidate: Path | None = None
		candidate_created = False
		try:
			if uses_release_candidate_path:
				candidate = session.config._tmp_path_factory.mktemp("candidate")
				item.stash[_RELEASE_CANDIDATE_PATH] = candidate
			else:
				candidate = _mk_tmp(SimpleNamespace(node=item), tmp_factory)
				candidate.mkdir()
				candidate_created = True
			with temporary_workspace(candidate):
				_default_release_workspace()
		except ReleaseControllerError as error:
			raise pytest.UsageError(
				"Provider release-test workspace preflight failed before test execution: "
				f"{error}"
			) from error
		except OSError as error:
			location = f" {candidate}" if candidate is not None else ""
			raise pytest.UsageError(
				"Provider release-test workspace preflight could not prepare its temporary "
				f"path{location}: {error}"
			) from error
		finally:
			if candidate_created and candidate is not None:
				shutil.rmtree(candidate)


def pytest_collection_finish(session: pytest.Session) -> None:
	"""Reject insufficient Windows release-test path headroom before any test runs."""
	if os.name == "nt":
		_preflight_release_test_paths(session)
