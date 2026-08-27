"""Reviewable escaped-defect lessons and release learning checks."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .contracts import CheckResult, Finding, Status

LESSONS_DIRECTORY = "lessons"
_LESSON_ID = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_FIELDS = {
	"id",
	"status",
	"incident",
	"expected_layer",
	"miss_cause",
	"adaptation",
	"evidence",
}
_MIN_FRONT_MATTER_LINES = 3
_MIN_QUOTED_VALUE_LENGTH = 2
_MAX_LESSON_BYTES = 1024 * 1024
_MAX_LESSON_FILES = 1000
_MAX_FIELD_VALUE_LENGTH = 16_384


class LessonError(ValueError):
	"""A lesson collection is malformed or cannot be read."""

	def __init__(self, message: str, *, path: str | None = None) -> None:
		self.path = path
		super().__init__(f"{path}: {message}" if path else message)


@dataclass(frozen=True, slots=True)
class Lesson:
	"""One escaped-defect lesson stored as English Markdown."""

	id: str
	status: str
	incident: str
	expected_layer: str
	miss_cause: str
	adaptation: str
	evidence: str
	path: str

	@property
	def is_complete(self) -> bool:
		"""Return whether the lesson has both adaptation and evidence."""
		return bool(self.adaptation.strip() and self.evidence.strip())


def _value(raw: str, *, path: str, field: str) -> str:
	value = raw.strip()
	if not value:
		return ""
	if len(value) > _MAX_FIELD_VALUE_LENGTH:
		raise LessonError(f"{field} exceeds the maximum length", path=path)
	if value[0] in {'"', "'"}:
		if len(value) < _MIN_QUOTED_VALUE_LENGTH or value[-1] != value[0]:
			raise LessonError(f"{field} must be a closed string", path=path)
		if value[0] == '"':
			try:
				return str(json.loads(value))
			except json.JSONDecodeError as error:
				raise LessonError(f"{field} is not a valid quoted string", path=path) from error
		return value[1:-1]
	return value


def _parse_frontmatter(text: str, relative: str) -> dict[str, str]:
	lines = text.splitlines()
	if len(lines) < _MIN_FRONT_MATTER_LINES or lines[0].strip() != "---":
		raise LessonError("lesson must start with YAML front matter", path=relative)
	try:
		end = lines.index("---", 1)
	except ValueError as error:
		raise LessonError("lesson front matter is not closed", path=relative) from error
	if end == 1:
		raise LessonError("lesson front matter must not be empty", path=relative)
	values: dict[str, str] = {}
	for line in lines[1:end]:
		if not line.strip() or line.lstrip().startswith("#"):
			continue
		if ":" not in line:
			raise LessonError("front matter must use field: value entries", path=relative)
		key, raw = line.split(":", 1)
		key = key.strip().casefold()
		if key not in _FIELDS:
			raise LessonError(f"unknown lesson field {key!r}", path=relative)
		if key in values:
			raise LessonError(f"duplicate lesson field {key!r}", path=relative)
		values[key] = _value(raw, path=relative, field=key)
	return values


def _parse(path: Path, relative: str) -> Lesson:
	try:
		raw = path.read_bytes()
	except OSError as error:
		raise LessonError("lesson is unreadable or not valid UTF-8", path=relative) from error
	if len(raw) > _MAX_LESSON_BYTES:
		raise LessonError("lesson exceeds the 1 MiB size limit", path=relative)
	try:
		text = raw.decode("utf-8")
	except UnicodeDecodeError as error:
		raise LessonError("lesson is unreadable or not valid UTF-8", path=relative) from error
	values = _parse_frontmatter(text, relative)

	missing = sorted(_FIELDS - values.keys())
	if missing:
		raise LessonError(f"missing lesson field {missing[0]!r}", path=relative)
	lesson_id = values["id"]
	if not _LESSON_ID.fullmatch(lesson_id):
		raise LessonError(
			"id must use lowercase letters, numbers, dots, dashes, or underscores", path=relative
		)
	if values["status"] not in {"open", "learned"}:
		raise LessonError("status must be 'open' or 'learned'", path=relative)
	for field in ("incident", "expected_layer", "miss_cause"):
		if not values[field].strip():
			raise LessonError(f"{field} must not be empty", path=relative)
	return Lesson(
		lesson_id,
		values["status"],
		values["incident"],
		values["expected_layer"],
		values["miss_cause"],
		values["adaptation"],
		values["evidence"],
		relative,
	)


def load_lessons(root: Path | str = ".") -> tuple[Lesson, ...]:
	"""Load all lesson Markdown files, rejecting malformed or duplicate lessons."""
	lessons_root = Path(root).resolve() / LESSONS_DIRECTORY
	if not lessons_root.exists():
		return ()
	if not lessons_root.is_dir():
		raise LessonError("lessons path is not a directory", path=LESSONS_DIRECTORY)
	lessons: list[Lesson] = []
	seen: dict[str, str] = {}
	errors: list[str] = []
	try:
		paths = sorted(
			path for path in lessons_root.glob("*.md") if path.name.casefold() != "readme.md"
		)
	except OSError as error:
		raise LessonError("lessons directory cannot be read", path=LESSONS_DIRECTORY) from error
	if len(paths) > _MAX_LESSON_FILES:
		raise LessonError("lessons directory exceeds the 1000-file limit", path=LESSONS_DIRECTORY)
	for path in paths:
		relative = path.relative_to(Path(root).resolve()).as_posix()
		try:
			lesson = _parse(path, relative)
		except LessonError as error:
			errors.append(str(error))
			continue
		previous = seen.get(lesson.id.casefold())
		if previous is not None:
			errors.append(
				f"{relative}: duplicate lesson id {lesson.id!r} (also declared in {previous})"
			)
			continue
		seen[lesson.id.casefold()] = relative
		lessons.append(lesson)
	if errors:
		raise LessonError("; ".join(errors), path=LESSONS_DIRECTORY)
	return tuple(lessons)


def lessons_result(
	root: Path | str = ".",
	*,
	is_complete_required: bool = False,
	check_id: str = "lessons.learning",
) -> CheckResult:
	"""Return the audit or release result for the repository's lessons."""
	try:
		lessons = load_lessons(root)
	except LessonError as error:
		finding = Finding(
			error.path or LESSONS_DIRECTORY,
			message=str(error),
			action="repair the lesson Markdown and rerun the audit",
		)
		return CheckResult(
			check_id=check_id,
			status=Status.UNCHECKED,
			summary="lesson collection is malformed or unavailable",
			findings=(finding,),
			recovery_action="repair every lesson and rerun the audit",
		)
	if not lessons:
		return CheckResult(check_id, Status.PASSED, "no escaped-defect lessons are recorded")
	incomplete = [lesson for lesson in lessons if not lesson.is_complete]
	if is_complete_required and incomplete:
		return CheckResult(
			check_id,
			Status.FAILED,
			"lessons are missing an adaptation or evidence",
			findings=tuple(
				Finding(
					lesson.path,
					message="lesson is not learned: adaptation and evidence are required",
					action="record the gate adaptation and verification evidence",
				)
				for lesson in incomplete
			),
			recovery_action="complete every lesson with an adaptation and evidence",
		)
	return CheckResult(
		check_id,
		Status.PASSED,
		f"{len(lessons)} lesson(s) recorded; open lessons remain visible",
	)


def release_readiness(root: Path | str = ".") -> CheckResult:
	"""Return the release controller result for lesson completion."""
	return lessons_result(root, is_complete_required=True, check_id="release.lessons")


class ReleaseBlockedError(RuntimeError):
	"""A release cannot proceed while lessons are malformed or unlearned."""


ReleaseBlocked = ReleaseBlockedError


def ensure_release_ready(root: Path | str = ".") -> None:
	"""Raise when the Quality Gate release learning state is incomplete."""
	result = release_readiness(root)
	if result.status is not Status.PASSED:
		raise ReleaseBlockedError(result.summary)
