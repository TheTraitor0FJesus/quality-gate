from __future__ import annotations

from pathlib import Path

import pytest

from quality_gate.contracts import Status
from quality_gate.lessons import (
	LessonError,
	ensure_release_ready,
	lessons_result,
	load_lessons,
	release_readiness,
)


def _write_lesson(root: Path, name: str, content: str) -> None:
	lessons = root / "lessons"
	lessons.mkdir(exist_ok=True)
	(lessons / name).write_text(content, encoding="utf-8")


def test_malformed_lesson_is_rejected(tmp_path: Path) -> None:
	_write_lesson(
		tmp_path,
		"incident.md",
		"""---
id: incident-1
status: open
incident: A defect escaped.
---

# Incident

The lesson is incomplete.
""",
	)

	with pytest.raises(LessonError, match="missing lesson field"):
		load_lessons(tmp_path)


def test_malformed_lesson_is_unchecked_at_result_boundary(tmp_path: Path) -> None:
	_write_lesson(tmp_path, "incident.md", "not front matter\n")

	result = lessons_result(tmp_path)

	assert result.status is Status.UNCHECKED


def test_oversized_lesson_is_rejected(tmp_path: Path) -> None:
	lessons = tmp_path / "lessons"
	lessons.mkdir()
	(lessons / "incident.md").write_bytes(b"x" * (1024 * 1024 + 1))

	with pytest.raises(LessonError, match="1 MiB"):
		load_lessons(tmp_path)


def _lesson(status: str = "open", *, adaptation: str = "", evidence: str = "") -> str:
	return f"""---
id: incident-1
status: {status}
incident: A defect escaped the gate.
expected_layer: repository check
miss_cause: The case was not covered.
adaptation: {adaptation}
evidence: {evidence}
---

# Lesson

The incident and its prevention are recorded here.
"""


def test_duplicate_lesson_ids_are_rejected(tmp_path: Path) -> None:
	_write_lesson(tmp_path, "first.md", _lesson())
	_write_lesson(tmp_path, "second.md", _lesson())

	with pytest.raises(LessonError, match="duplicate lesson id"):
		load_lessons(tmp_path)


def test_unlearned_lesson_blocks_audit_release_readiness(tmp_path: Path) -> None:
	_write_lesson(tmp_path, "incident.md", _lesson())

	result = release_readiness(tmp_path)

	assert result.status is Status.FAILED
	assert result.findings[0].path == "lessons/incident.md"
	with pytest.raises(RuntimeError):
		ensure_release_ready(tmp_path)


def test_learned_lesson_allows_release_readiness(tmp_path: Path) -> None:
	_write_lesson(
		tmp_path,
		"incident.md",
		_lesson("learned", adaptation="Added a regression check.", evidence="CI run 123."),
	)

	result = release_readiness(tmp_path)

	assert result.status is Status.PASSED
	ensure_release_ready(tmp_path)
