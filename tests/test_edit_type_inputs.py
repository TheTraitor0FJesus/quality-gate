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


def test_inventory_unions_declared_source_roots_and_tracks_their_membership(
	tmp_path: Path,
) -> None:
	(tmp_path / "bot").mkdir()
	(tmp_path / "bot/main.py").write_bytes(b"from scripts.api import value\n")
	(tmp_path / "scripts").mkdir()
	(tmp_path / "scripts/api.py").write_bytes(b"value: int = 1\n")
	(tmp_path / "stubs/apscheduler").mkdir(parents=True)
	(tmp_path / "stubs/apscheduler/py.typed").write_bytes(b"\n")
	(tmp_path / "stubs/apscheduler/api.pyi").write_bytes(b"class Scheduler: ...\n")

	before = inventory(
		tmp_path,
		"bot",
		managed=True,
		deadline=time.monotonic() + 5,
		source_roots=("stubs", "scripts"),
	)
	reordered = inventory(
		tmp_path,
		"bot",
		managed=True,
		deadline=time.monotonic() + 5,
		source_roots=("scripts", "stubs"),
	)
	assert before == reordered
	assert {source.path for source in before[0]} == {
		"bot/main.py",
		"scripts/api.py",
		"stubs/apscheduler/api.pyi",
	}
	assert {directory["path"] for directory in before[1]} == {
		"bot",
		"scripts",
		"stubs",
		"stubs/apscheduler",
	}

	(tmp_path / "scripts/api.py").write_bytes(b"value: str = 'changed'\n")
	after_mutation = inventory(
		tmp_path,
		"bot",
		managed=True,
		deadline=time.monotonic() + 5,
		source_roots=("scripts", "stubs"),
	)
	assert after_mutation[0] != before[0]
	(tmp_path / "stubs/apscheduler/extra.pyi").write_bytes(b"class Extra: ...\n")
	after_addition = inventory(
		tmp_path,
		"bot",
		managed=True,
		deadline=time.monotonic() + 5,
		source_roots=("scripts", "stubs"),
	)
	assert after_addition[0] != after_mutation[0]
	assert after_addition[1] != after_mutation[1]
	(tmp_path / "stubs/apscheduler/extra.pyi").unlink()
	after_deletion = inventory(
		tmp_path,
		"bot",
		managed=True,
		deadline=time.monotonic() + 5,
		source_roots=("scripts", "stubs"),
	)
	assert after_deletion == after_mutation


def test_inventory_rejects_missing_overlapping_and_linked_source_roots(tmp_path: Path) -> None:
	(tmp_path / "bot/shared").mkdir(parents=True)
	(tmp_path / "bot/main.py").write_bytes(b"value: int = 1\n")
	with pytest.raises(EditError, match=r"^configuration$"):
		inventory(
			tmp_path,
			"bot",
			managed=True,
			deadline=time.monotonic() + 3,
			source_roots=("bot/shared",),
		)
	with pytest.raises(EditError, match=r"^dependencies$"):
		inventory(
			tmp_path,
			"bot",
			managed=True,
			deadline=time.monotonic() + 3,
			source_roots=("missing",),
		)
	with pytest.raises(EditError, match=r"^configuration$"):
		inventory(
			tmp_path,
			"bot",
			managed=True,
			deadline=time.monotonic() + 3,
			source_roots=(".",),
		)
	with pytest.raises(EditError, match=r"^configuration$"):
		inventory(
			tmp_path,
			".",
			managed=True,
			deadline=time.monotonic() + 3,
			source_roots=("shared",),
		)
	(tmp_path / "outside").mkdir()
	try:
		(tmp_path / "linked").symlink_to(tmp_path / "outside", target_is_directory=True)
	except OSError as error:
		pytest.skip(f"Directory symlinks are unavailable: {error}")
	with pytest.raises(EditError, match=r"^unsafe_path$"):
		inventory(
			tmp_path,
			"bot",
			managed=True,
			deadline=time.monotonic() + 3,
			source_roots=("linked",),
		)


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
