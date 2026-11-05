"""The edit-cache test fixture stays under the real repository temp root."""

from pathlib import Path

import pytest

import edit_check_support78 as support


def test_edit_cache_uses_short_repository_temp_for_a_staged_snapshot(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	repository = tmp_path / "repository"
	(repository / ".git").mkdir(parents=True)
	candidate = repository / "temp" / "snapshot" / "candidate"
	candidate.mkdir(parents=True)
	monkeypatch.setattr(support, "REPOSITORY", candidate)
	monkeypatch.setattr(support, "prepare_edit_cache", lambda path: path)
	fixture = support.edit_cache.__wrapped__()
	directory = next(fixture)
	assert directory.parent == repository / "temp"
	assert directory.is_dir()
	fixture.close()
	assert not directory.exists()
