"""Parse the bounded GitHub workflow shape used by integrity checks."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import cast

_WORKFLOW_REFERENCE = re.compile(r"^\s*(?:-\s*)?uses\s*:\s*([^\s#]+)", re.IGNORECASE)
_KEY = re.compile(r"^(?P<indent> *)(?P<key>[^:#\s][^:]*):(?:\s*(?P<value>.*))?$")
_TOP_LEVEL = 0
_JOB_LEVEL = 2
_PROPERTY_LEVEL = 4


@dataclass(frozen=True, slots=True)
class _WorkflowJob:
	id: str
	name: str | None
	timeout: str | None
	uses: str | None
	condition: str | None
	needs: str | None
	inputs: tuple[tuple[str, str], ...]
	has_steps: bool
	is_reusable: bool
	has_matrix: bool
	permissions: tuple[tuple[str | None, str], ...]


@dataclass(frozen=True, slots=True)
class _Workflow:
	path: str
	source: str
	uses: tuple[str, ...]
	jobs: tuple[_WorkflowJob, ...]
	on_text: str
	events: frozenset[str]
	permissions: tuple[tuple[str | None, str], ...]
	concurrency_group: str | None
	concurrency_cancel: str | None


def _unquote_scalar(value: str) -> str:
	if value.startswith(("'", '"')) and value[1:] and value.endswith(value[0]):
		return value[1:-1]
	return value


def _key(line: str) -> tuple[int, str, str] | None:
	match = _KEY.match(line.rstrip())
	if not match:
		return None
	key = _unquote_scalar(match.group("key").strip())
	return len(match.group("indent")), key, (match.group("value") or "").strip()


def _workflow_keys(text: str) -> list[tuple[int, str, str] | None]:
	lines = text.splitlines()
	if any("\t" in line[: len(line) - len(line.lstrip())] for line in lines):
		raise ValueError("workflow uses tabs for indentation")
	return [_key(line.split("#", 1)[0]) for line in lines]


def _workflow_on_text(lines: list[str], keys: list[tuple[int, str, str] | None]) -> str:
	on_start = next(
		(
			index
			for index, item in enumerate(keys)
			if item and item[0] == _TOP_LEVEL and item[1] == "on"
		),
		None,
	)
	if on_start is None:
		raise ValueError("workflow has no trigger mapping")
	on_end = next(
		(index for index in range(on_start + 1, len(keys)) if _is_top_level(keys[index])),
		len(keys),
	)
	return "\n".join(lines[on_start:on_end])


def _is_top_level(item: tuple[int, str, str] | None) -> bool:
	return item is not None and item[0] == _TOP_LEVEL


def _permission_entries(
	keys: list[tuple[int, str, str] | None],
	start: int,
	end: int,
	indent: int,
) -> tuple[tuple[str | None, str], ...]:
	for index in range(start, end):
		item = keys[index]
		if not item or item[0] != indent or item[1] != "permissions":
			continue
		if item[2]:
			raw_value = _unquote_scalar(item[2])
			if raw_value.startswith("{"):
				return tuple(
					(scope, value)
					for scope, value in re.findall(
						r"([a-z-]+)\s*:\s*([a-z-]+)", raw_value.casefold()
					)
				)
			return ((None, raw_value),)
		entries: list[tuple[str | None, str]] = []
		for child in keys[index + 1 : end]:
			if child and child[0] <= indent:
				break
			if child and child[0] == indent + 2:
				entries.append((child[1], _unquote_scalar(child[2])))
		return tuple(entries)
	return ()


def _workflow_inputs(
	keys: list[tuple[int, str, str] | None], start: int, end: int
) -> tuple[tuple[str, str], ...]:
	for index in range(start + 1, end):
		item = keys[index]
		if not item or item[0] != _PROPERTY_LEVEL or item[1] != "with":
			continue
		if item[2]:
			return ()
		inputs: list[tuple[str, str]] = []
		for child in keys[index + 1 : end]:
			if child and child[0] <= _PROPERTY_LEVEL:
				break
			if child and child[0] == _PROPERTY_LEVEL + 2:
				inputs.append((child[1], _unquote_scalar(child[2])))
		return tuple(inputs)
	return ()


def _workflow_needs(
	lines: list[str], keys: list[tuple[int, str, str] | None], start: int, end: int
) -> str | None:
	for index in range(start + 1, end):
		key_entry = keys[index]
		if not key_entry or key_entry[0] != _PROPERTY_LEVEL or key_entry[1] != "needs":
			continue
		if key_entry[2]:
			return _unquote_scalar(key_entry[2])
		values: list[str] = []
		for child_index in range(index + 1, end):
			dependency_line = lines[child_index].split("#", 1)[0]
			if not dependency_line.strip():
				continue
			indent = len(dependency_line) - len(dependency_line.lstrip(" "))
			if indent <= _PROPERTY_LEVEL:
				break
			dependency_item = dependency_line.lstrip(" ")
			if indent != _PROPERTY_LEVEL + 2 or not dependency_item.startswith("- "):
				return "${{ invalid needs }}"
			value = dependency_item[2:].strip()
			if not value:
				return "${{ invalid needs }}"
			values.append(_unquote_scalar(value))
		return f"[{','.join(values)}]" if values else ""
	return None


def _flow_mapping_key_at(value: str, start: int) -> str | None:
	while start < len(value) and value[start].isspace():
		start += 1
	end = start
	if start < len(value) and value[start] in "'\"":
		quote = value[start]
		end += 1
		key: list[str] = []
		while end < len(value):
			character = value[end]
			if quote == '"' and character == "\\" and end + 1 < len(value):
				key.append(value[end + 1])
				end += 2
				continue
			if character == quote:
				if quote == "'" and end + 1 < len(value) and value[end + 1] == "'":
					key.append("'")
					end += 2
					continue
				break
			key.append(character)
			end += 1
		if end == len(value):
			return None
		end += 1
	else:
		while end < len(value) and value[end] not in "\t\r\n :{}[],":
			end += 1
		key = list(value[start:end])
	while end < len(value) and value[end].isspace():
		end += 1
	return "".join(key) if end < len(value) and value[end] == ":" else None


def _flow_mapping_has_key(value: str, expected: str) -> bool:
	"""Find one simple flow-style mapping key without matching quoted string content."""
	index = 0
	quote: str | None = None
	escaped = False
	while index < len(value):
		character = value[index]
		if quote is not None:
			if quote == '"' and escaped:
				escaped = False
			elif quote == '"' and character == "\\":
				escaped = True
			elif character == quote:
				if quote == "'" and index + 1 < len(value) and value[index + 1] == "'":
					index += 1
				else:
					quote = None
		elif character in "'\"":
			quote = character
		elif character in "{," and _flow_mapping_key_at(value, index + 1) == expected:
			return True
		index += 1
	return False


def _workflow_jobs(keys: list[tuple[int, str, str] | None], lines: list[str]) -> list[_WorkflowJob]:
	jobs_start = next(
		(
			index
			for index, item in enumerate(keys)
			if item and item[0] == _TOP_LEVEL and item[1] == "jobs"
		),
		None,
	)
	if jobs_start is None:
		raise ValueError("workflow has no jobs mapping")
	jobs_end = next(
		(index for index in range(jobs_start + 1, len(keys)) if _is_top_level(keys[index])),
		len(keys),
	)
	job_starts = [
		index
		for index, item in enumerate(keys[jobs_start + 1 : jobs_end], jobs_start + 1)
		if item and item[0] == _JOB_LEVEL and item[1] != "jobs"
	]
	jobs: list[_WorkflowJob] = []
	for position, start in enumerate(job_starts):
		end = job_starts[position + 1] if position + 1 < len(job_starts) else jobs_end
		raw_properties = {
			item[1]: item[2]
			for item in keys[start + 1 : end]
			if item and item[0] == _PROPERTY_LEVEL
		}
		properties = {
			item[1]: _unquote_scalar(item[2])
			for item in keys[start + 1 : end]
			if item and item[0] == _PROPERTY_LEVEL
		}
		job_item = cast(tuple[int, str, str], keys[start])
		jobs.append(
			_WorkflowJob(
				job_item[1],
				properties.get("name"),
				properties.get("timeout-minutes"),
				properties.get("uses"),
				properties.get("if"),
				_workflow_needs(lines, keys, start, end),
				_workflow_inputs(keys, start, end),
				any(
					item and item[0] == _PROPERTY_LEVEL and item[1] == "steps"
					for item in keys[start + 1 : end]
				),
				"uses" in properties,
				any(
					item and item[0] == _PROPERTY_LEVEL + 2 and item[1] == "matrix"
					for item in keys[start + 1 : end]
				)
				or _flow_mapping_has_key(raw_properties.get("strategy", ""), "matrix"),
				_permission_entries(keys, start + 1, end, _PROPERTY_LEVEL),
			)
		)
	return jobs


def _needs_tokens(job: _WorkflowJob) -> frozenset[str] | None:
	"""Parse a static scalar or list of direct GitHub Actions job dependencies."""
	if job.needs is None:
		return None
	raw = job.needs.strip()
	if raw.startswith("[") and raw.endswith("]"):
		values = [_unquote_scalar(item.strip()) for item in raw[1:-1].split(",")]
	else:
		values = [_unquote_scalar(raw)]
	if (
		not values
		or any(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", value) is None for value in values)
		or len(values) != len(set(values))
	):
		return None
	return frozenset(values)


def _workflow_events(keys: list[tuple[int, str, str] | None]) -> frozenset[str]:
	for index, item in enumerate(keys):
		if not item or item[0] != _TOP_LEVEL or item[1] != "on":
			continue
		if item[2]:
			return frozenset(re.findall(r"[a-z_]+", item[2].casefold()))
		on_end = next(
			(position for position in range(index + 1, len(keys)) if _is_top_level(keys[position])),
			len(keys),
		)
		return frozenset(
			child[1].casefold()
			for child in keys[index + 1 : on_end]
			if child and child[0] == _JOB_LEVEL
		)
	return frozenset()


def _workflow_concurrency(
	keys: list[tuple[int, str, str] | None],
) -> tuple[str | None, str | None]:
	for index, item in enumerate(keys):
		if not item or item[0] != _TOP_LEVEL or item[1] != "concurrency":
			continue
		group: str | None = None
		cancel: str | None = None
		for child in keys[index + 1 :]:
			if child and child[0] == _TOP_LEVEL:
				break
			if child and child[0] == _JOB_LEVEL and child[1] == "group":
				group = _unquote_scalar(child[2]) or None
			elif child and child[0] == _JOB_LEVEL and child[1] == "cancel-in-progress":
				cancel = _unquote_scalar(child[2]).casefold() or None
		return group, cancel
	return None, None


def _parse_workflow(path: Path, relative: str) -> _Workflow:
	try:
		text = path.read_text(encoding="utf-8-sig")
	except (OSError, UnicodeError) as error:
		raise ValueError("workflow is unreadable") from error
	lines = text.splitlines()
	keys = _workflow_keys(text)
	on_text = _workflow_on_text([line.split("#", 1)[0] for line in lines], keys)
	uses = tuple(
		_unquote_scalar(match.group(1))
		for line in lines
		if (match := _WORKFLOW_REFERENCE.match(line)) is not None
	)
	jobs = _workflow_jobs(keys, lines)
	permissions = _permission_entries(keys, 0, len(keys), _TOP_LEVEL)
	concurrency_group, concurrency_cancel = _workflow_concurrency(keys)
	return _Workflow(
		relative,
		text,
		uses,
		tuple(jobs),
		on_text,
		_workflow_events(keys),
		permissions,
		concurrency_group,
		concurrency_cancel,
	)
