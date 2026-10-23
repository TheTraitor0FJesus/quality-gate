"""Fixed filesystem evidence includes additions and rejects unsafe environment links."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from quality_gate.edit_protocol import EditError
from quality_gate.edit_type_inputs import environment_tree, inventory, request_files

ENVIRONMENT_FILES_WITH_BASE = 2
DIRECTORIES_OVER_SOURCE_CAP = 1025


def test_environment_directory_with_bytecode_suffix_is_not_ignored(tmp_path: Path) -> None:
	root = tmp_path / "environment"
	(root / "data.pyc").mkdir(parents=True)
	(root / "data.pyc/metadata").write_bytes(b"first")
	base = tmp_path / "python"
	base.write_bytes(b"interpreter")
	before = environment_tree(root, base, time.monotonic() + 3)
	(root / "data.pyc/metadata").write_bytes(b"other")
	after = environment_tree(root, base, time.monotonic() + 3)
	assert before["sha256"] != after["sha256"]
	assert before["file_count"] == ENVIRONMENT_FILES_WITH_BASE
	assert before["bytes"] == len(b"firstinterpreter")


def test_source_directory_membership_detects_added_deleted_and_empty_directories(
	tmp_path: Path,
) -> None:
	(tmp_path / "source.py").write_bytes(b"value = 1\n")
	first_sources, first_directories = inventory(
		tmp_path, ".", managed=False, deadline=time.monotonic() + 3
	)
	(tmp_path / "new").mkdir()
	second_sources, second_directories = inventory(
		tmp_path, ".", managed=False, deadline=time.monotonic() + 3
	)
	assert first_sources == second_sources
	assert first_directories != second_directories
	assert {record["path"] for record in second_directories} == {".", "new"}
	(tmp_path / "source.py").unlink()
	last_sources, last_directories = inventory(
		tmp_path, ".", managed=False, deadline=time.monotonic() + 3
	)
	assert last_sources == []
	assert last_directories != second_directories


def test_deleted_request_is_invalidated_by_recreation(tmp_path: Path) -> None:
	entries = [{"path": "deleted.py", "sha256": None}]
	assert request_files(tmp_path, entries) == [
		{"path": "deleted.py", "input_sha256": None, "final_sha256": None}
	]
	(tmp_path / "deleted.py").write_bytes(b"")
	with pytest.raises(EditError, match=r"^stale_input$"):
		request_files(tmp_path, entries)


def test_environment_hardlinks_are_not_accepted_as_regular_files(tmp_path: Path) -> None:
	root = tmp_path / "environment"
	root.mkdir()
	base = tmp_path / "python"
	base.write_bytes(b"interpreter")
	os.link(base, root / "linked")
	with pytest.raises(EditError, match=r"^unsafe_path$"):
		environment_tree(root, base, time.monotonic() + 3)


def test_source_limit_does_not_count_empty_directories(tmp_path: Path) -> None:
	(tmp_path / "source.py").write_bytes(b"value: int = 1\n")
	for index in range(DIRECTORIES_OVER_SOURCE_CAP):
		(tmp_path / f"d{index}").mkdir()
	sources, directories = inventory(tmp_path, ".", managed=False, deadline=time.monotonic() + 30)
	assert len(sources) == 1
	assert len(directories) == DIRECTORIES_OVER_SOURCE_CAP + 1
