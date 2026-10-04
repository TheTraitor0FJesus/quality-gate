"""Repository-level Git, workflow, and documentation contract checks."""

from __future__ import annotations

import os
import re
import subprocess
import tomllib
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast
from urllib.parse import unquote, urlsplit

from .contracts import CheckResult, Finding, Manifest, Status
from .publication_config import validate_config

_CONFLICT_MARKER = re.compile(r"^\s*(?:<{7}|={7}|>{7})(?:\s|$)")
_EXTERNAL_LINK = re.compile(r"^[a-z][a-z0-9+.-]*:", re.IGNORECASE)
_MARKDOWN_LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\n]+)\)")
_WORKFLOW_REFERENCE = re.compile(r"^\s*(?:-\s*)?uses\s*:\s*([^\s#]+)", re.IGNORECASE)
_KEY = re.compile(r"^(?P<indent> *)(?P<key>[^:#\s][^:]*):(?:\s*(?P<value>.*))?$")
_SHA = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)
_DEFAULT_BRANCHES = {"main", "master"}
_TOP_LEVEL = 0
_JOB_LEVEL = 2
_PROPERTY_LEVEL = 4
_JUNK_NAMES = {
	".coverage",
	".ds_store",
	".mypy_cache",
	".pytest_cache",
	".ruff_cache",
	".quality-gate-tmp",
	".venv",
	"__pycache__",
	"desktop.ini",
	"node_modules",
	"thumbs.db",
	"venv",
}
_JUNK_SUFFIXES = (
	".pyc",
	".pyo",
	".swp",
	".swo",
	".tmp",
	".temp",
	".bak",
	".orig",
)
_JUNK_NAMES_EXACT = {".DS_Store", "Thumbs.db", "desktop.ini"}


@dataclass(frozen=True, slots=True)
class _TreeEntry:
	path: str
	abs_path: Path
	is_dir: bool = False
	is_symlink: bool = False


@dataclass(slots=True)
class _TreeScan:
	entries: list[_TreeEntry] = field(default_factory=list)
	unreadable: list[Finding] = field(default_factory=list)


def _scan_tree(root: Path) -> _TreeScan:
	scan = _TreeScan()

	def visit(directory: Path, relative: str = "") -> None:
		try:
			children = sorted(Path(directory).iterdir(), key=lambda item: item.name.casefold())
		except OSError:
			scan.unreadable.append(
				Finding(relative, message="candidate path is unreadable", action="restore access")
			)
			return
		for child in children:
			path = f"{relative}/{child.name}" if relative else child.name
			try:
				if child.is_symlink():
					scan.entries.append(_TreeEntry(path, child, is_symlink=True))
				elif child.is_dir():
					scan.entries.append(_TreeEntry(path, child, is_dir=True))
					visit(child, path)
				else:
					scan.entries.append(_TreeEntry(path, child))
			except OSError:
				scan.unreadable.append(
					Finding(path, message="candidate path is unreadable", action="restore access")
				)

	visit(root)
	return scan


def _unchecked(check_id: str, summary: str, findings: tuple[Finding, ...]) -> CheckResult:
	return CheckResult(
		check_id=check_id,
		status=Status.UNCHECKED,
		summary=summary,
		findings=findings,
		recovery_action="restore a readable, supported candidate and retry the quality gate",
	)


def _failed_or_waived(
	check_id: str, summary: str, findings: list[Finding], manifest: Manifest
) -> CheckResult:
	if not findings:
		return CheckResult(check_id, Status.PASSED, summary)
	remaining = [
		finding for finding in findings if manifest.resolve_waiver(check_id, finding.path) is None
	]
	if not remaining:
		return CheckResult(
			check_id,
			Status.WAIVED,
			f"{summary}; exact current waiver applies",
			waiver_target=findings[0].path,
		)
	return CheckResult(
		check_id,
		Status.FAILED,
		summary,
		findings=tuple(remaining),
		recovery_action="fix the reported repository finding or add one exact current human waiver",
	)


def _git_conflict_result(manifest: Manifest, scan: _TreeScan) -> CheckResult:
	if scan.unreadable:
		return _unchecked(
			"repository.git.conflict_markers",
			"Git candidate cannot be read",
			tuple(scan.unreadable),
		)
	findings: list[Finding] = []
	for entry in scan.entries:
		if entry.is_dir or entry.is_symlink:
			continue
		try:
			data = entry.abs_path.read_bytes()
			delimiter = b"\x00"
			if delimiter in data:
				continue
			text = data.decode("utf-8")
		except (OSError, UnicodeError):
			continue
		for line_number, line in enumerate(text.splitlines(), start=1):
			if _CONFLICT_MARKER.match(line):
				findings.append(
					Finding(
						entry.path,
						line_number,
						"merge conflict marker is present",
						"resolve the merge conflict",
					)
				)
	return _failed_or_waived(
		"repository.git.conflict_markers",
		"candidate text contains merge conflict markers",
		findings,
		manifest,
	)


def _is_junk(path: str) -> bool:
	parts = path.split("/")
	for part in parts:
		if part in _JUNK_NAMES_EXACT or part.casefold() in _JUNK_NAMES:
			return True
		if part.casefold().endswith(_JUNK_SUFFIXES) or part.endswith("~"):
			return True
	return False


def _git_junk_result(manifest: Manifest, scan: _TreeScan) -> CheckResult:
	if scan.unreadable:
		return _unchecked(
			"repository.git.tracked_junk",
			"Git candidate cannot be read",
			tuple(scan.unreadable),
		)
	findings = [
		Finding(
			entry.path,
			message="tracked repository junk is present",
			action="remove it from the candidate",
		)
		for entry in scan.entries
		if _is_junk(entry.path)
	]
	return _failed_or_waived(
		"repository.git.tracked_junk",
		"candidate contains tracked repository junk",
		findings,
		manifest,
	)


def _git_large_blob_result(manifest: Manifest, scan: _TreeScan) -> CheckResult:
	if scan.unreadable:
		return _unchecked(
			"repository.git.large_blobs",
			"Git candidate cannot be read",
			tuple(scan.unreadable),
		)
	limit = manifest.repository.max_blob_size_mib * 1024 * 1024
	findings: list[Finding] = []
	for entry in scan.entries:
		if entry.is_dir or entry.is_symlink:
			continue
		try:
			size = entry.abs_path.stat().st_size
		except OSError:
			return _unchecked(
				"repository.git.large_blobs",
				"Git candidate blob size cannot be read",
				(Finding(entry.path, message="blob size is unreadable", action="restore access"),),
			)
		if size > limit:
			findings.append(
				Finding(
					entry.path,
					message=f"blob is {size} bytes; limit is {limit} bytes",
					action="remove the blob or use an approved large-file storage decision",
				)
			)
	return _failed_or_waived(
		"repository.git.large_blobs", "candidate contains oversized Git blobs", findings, manifest
	)


def _git_case_result(
	manifest: Manifest,
	scan: _TreeScan,
	repository: Path | None = None,
	index_file: Path | None = None,
) -> CheckResult:
	if scan.unreadable:
		return _unchecked(
			"repository.git.case_collisions",
			"Git candidate cannot be read",
			tuple(scan.unreadable),
		)
	paths: dict[str, list[str]] = {}
	if repository is None or not (repository / ".git").exists():
		candidate_paths = [entry.path for entry in scan.entries]
	else:
		try:
			environment = os.environ.copy()
			if index_file is not None:
				environment["GIT_INDEX_FILE"] = str(index_file)
			result = subprocess.run(
				["git", "-C", str(repository), "ls-files", "--cached", "-z"],
				capture_output=True,
				check=False,
				env=environment,
				timeout=manifest.repository.command_timeout_seconds,
			)
		except (OSError, subprocess.TimeoutExpired):
			return _unchecked(
				"repository.git.case_collisions",
				"staged paths cannot be read",
				(
					Finding(
						message="Git could not enumerate staged paths",
						action="restore Git access",
					),
				),
			)
		if result.returncode:
			return _unchecked(
				"repository.git.case_collisions",
				"staged paths cannot be read",
				(
					Finding(
						message="Git could not enumerate staged paths",
						action="restore Git access",
					),
				),
			)
		candidate_paths = [os.fsdecode(path) for path in result.stdout.split(b"\0") if path]
	for path in candidate_paths:
		paths.setdefault(path.casefold(), []).append(path.replace("\\", "/"))
	findings = [
		Finding(
			", ".join(sorted(names)),
			message="paths collide on a case-insensitive filesystem",
			action="rename one path",
		)
		for names in paths.values()
		if len(names) > 1
	]
	return _failed_or_waived(
		"repository.git.case_collisions",
		"candidate contains case-colliding paths",
		findings,
		manifest,
	)


def _git_symlink_result(root: Path, manifest: Manifest, scan: _TreeScan) -> CheckResult:
	if scan.unreadable:
		return _unchecked(
			"repository.git.unsafe_symlinks",
			"Git candidate cannot be read",
			tuple(scan.unreadable),
		)
	findings: list[Finding] = []
	for entry in scan.entries:
		if not entry.is_symlink:
			continue
		try:
			target = Path(entry.abs_path.readlink())
			resolved = (entry.abs_path.parent / target).resolve(strict=False)
		except (OSError, RuntimeError, ValueError):
			return _unchecked(
				"repository.git.unsafe_symlinks",
				"symbolic-link target cannot be resolved",
				(
					Finding(
						entry.path,
						message="symbolic-link target is unreadable",
						action="restore access",
					),
				),
			)
		if target.is_absolute() or not resolved.is_relative_to(root):
			findings.append(
				Finding(
					entry.path,
					message="symbolic link resolves outside the repository",
					action="replace it with a safe relative link",
				)
			)
	return _failed_or_waived(
		"repository.git.unsafe_symlinks",
		"candidate contains unsafe symbolic links",
		findings,
		manifest,
	)


def git_integrity_results(
	root: Path,
	manifest: Manifest,
	repository: Path | None = None,
	index_file: Path | None = None,
) -> tuple[CheckResult, ...]:
	scan = _scan_tree(root)
	return (
		_git_conflict_result(manifest, scan),
		_git_junk_result(manifest, scan),
		_git_large_blob_result(manifest, scan),
		_git_case_result(manifest, scan, repository, index_file),
		_git_symlink_result(root, manifest, scan),
	)


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


def _workflow_finding(path: str, message: str, action: str) -> Finding:
	return Finding(path, message=message, action=action)


def _workflow_paths(root: Path) -> list[Path]:
	return sorted(
		path
		for path in root.glob(".github/workflows/*")
		if not path.is_symlink() and path.is_file() and path.suffix.casefold() in {".yml", ".yaml"}
	)


def _single_workflow_findings(workflow: _Workflow, is_quality_gate_provider: bool) -> list[Finding]:
	findings: list[Finding] = []
	findings.extend(_workflow_reference_findings(workflow))
	is_pull_request = bool({"pull_request", "pull_request_target"} & workflow.events)
	findings.extend(
		_workflow_permission_findings(workflow, is_pull_request, is_quality_gate_provider)
	)
	if workflow.concurrency_group is None or workflow.concurrency_cancel is None:
		findings.append(
			_workflow_finding(
				workflow.path,
				"workflow does not declare explicit concurrency cancellation behavior",
				"declare concurrency.cancel-in-progress explicitly",
			)
		)
	elif is_pull_request and workflow.concurrency_cancel != "true":
		findings.append(
			_workflow_finding(
				workflow.path,
				"pull-request concurrency does not cancel superseded runs",
				"set concurrency.cancel-in-progress to true",
			)
		)
	for job in workflow.jobs:
		if not job.is_reusable and (
			job.timeout is None or not job.timeout.isdigit() or int(job.timeout) <= 0
		):
			findings.append(
				_workflow_finding(
					workflow.path,
					f"job {job.id!r} has no positive timeout-minutes",
					"declare a bounded job timeout",
				)
			)
	if workflow.path == ".github/workflows/release.yml" and any(
		job.id == "write-image" and job.has_matrix for job in workflow.jobs
	):
		findings.append(
			_workflow_finding(
				workflow.path,
				"release image writer job declares a matrix",
				"remove the matrix so one approved writer job makes one package write call",
			)
		)
	return findings


def _workflow_permission_findings(
	workflow: _Workflow, is_pull_request: bool, is_quality_gate_provider: bool
) -> list[Finding]:
	findings: list[Finding] = []
	metadata_review = _approved_dependabot_review_workflow(workflow)
	if not workflow.permissions:
		findings.append(
			_workflow_finding(
				workflow.path,
				"workflow does not declare top-level permissions",
				"declare the minimum required read permissions",
			)
		)
	elif any(
		value.casefold() not in {"", "read", "none"} for _scope, value in workflow.permissions
	):
		findings.append(
			_workflow_finding(
				workflow.path,
				"workflow permissions are broader than read-only",
				"reduce permissions to the minimum required values",
			)
		)
	write_permissions = [
		(scope, job.id)
		for job in workflow.jobs
		for scope, value in job.permissions
		if value.casefold() not in {"", "read", "none"}
	]
	provider_internal = _provider_internal_write_workflow(workflow, is_quality_gate_provider)
	if (
		write_permissions
		and is_pull_request
		and not _approved_release_writer_route(workflow, is_quality_gate_provider)
		and not provider_internal
		and not metadata_review
	):
		findings.append(
			_workflow_finding(
				workflow.path,
				"write-capable jobs are reachable from pull requests",
				"remove write permissions or isolate the jobs in a deployment-only workflow",
			)
		)
	elif (
		write_permissions
		and not _approved_release_writer_route(workflow, is_quality_gate_provider)
		and not provider_internal
		and not metadata_review
	):
		unrelated_scopes = sorted(
			{scope or "write-all" for scope, _job_id in write_permissions}
			- {"contents", "packages"}
		)
		if unrelated_scopes:
			findings.append(
				_workflow_finding(
					workflow.path,
					"deployment job uses an unrelated write permission",
					"limit job-level writes to contents and packages",
				)
			)
		if not _is_deployment_only(workflow):
			findings.append(
				_workflow_finding(
					workflow.path,
					"write-capable workflow is not restricted to deployment triggers",
					"limit it to default-branch pushes and/or workflow_dispatch",
				)
			)
	if write_permissions and _approved_release_writer_route(workflow, is_quality_gate_provider):
		findings.extend(
			_release_writer_permission_findings(
				workflow, write_permissions, is_quality_gate_provider
			)
		)
	if write_permissions and provider_internal:
		findings.extend(_provider_internal_permission_findings(workflow))
	return findings


_DEPENDABOT_REVIEW_WORKFLOW = """name: Dependabot review

on:
  pull_request_target:
    branches: [main]
    types: [opened, reopened, ready_for_review]

permissions:
  contents: read

concurrency:
  group: dependabot-review-${{ github.event.pull_request.number }}
  cancel-in-progress: true

jobs:
  request-review:
    if: >-
      github.event.pull_request.user.login == 'dependabot[bot]' &&
      github.event.pull_request.user.type == 'Bot' &&
      !github.event.pull_request.draft
    runs-on: ubuntu-24.04
    timeout-minutes: 5
    permissions:
      pull-requests: write
    steps:
      - name: Request review from the repository owner
        shell: bash
        env:
          GH_TOKEN: ${{ github.token }}
          GH_REPO: ${{ github.repository }}
          PR_NUMBER: ${{ github.event.pull_request.number }}
          REVIEWER: ${{ github.repository_owner }}
        run: |
          reviewers=$(gh pr view "$PR_NUMBER" --repo "$GH_REPO" \\
            --json reviewRequests,reviews \\
            --jq '[.reviewRequests[].login, .reviews[].author.login] | unique | .[]')
          if grep -Fx "$REVIEWER" <<< "$reviewers" >/dev/null; then
            exit 0
          fi
          gh api --method POST "repos/$GH_REPO/pulls/$PR_NUMBER/requested_reviewers" \\
            -f "reviewers[]=$REVIEWER" --silent
"""


def _approved_dependabot_review_workflow(workflow: _Workflow) -> bool:
	"""Allow only the exact metadata-only Dependabot owner-review workflow template."""
	return workflow.path == ".github/workflows/dependabot-review.yml" and (
		workflow.source.rstrip("\r\n") == _DEPENDABOT_REVIEW_WORKFLOW.rstrip("\r\n")
	)


_QUALITY_GATE_PROVIDER = "TheTraitor0FJesus/quality-gate"
_RELEASE_READ_PERMISSIONS = {
	"contents": "read",
	"pull-requests": "read",
	"actions": "read",
	"packages": "read",
}


def _permission_map(job: _WorkflowJob) -> dict[str | None, str]:
	return {scope: value.casefold() for scope, value in job.permissions}


def _input_map(job: _WorkflowJob) -> dict[str, str]:
	return dict(job.inputs)


_RELEASE_PR_INPUT = "${{ format('{0}', github.event.pull_request.number || inputs.pr_number) }}"
_PREPARED_CANDIDATE_INPUT = "${{ needs.prepare.outputs.candidate-id }}"
_RELEASE_PR_INPUTS = {"pr-number": _RELEASE_PR_INPUT}
_PREPARED_CANDIDATE_INPUTS = {
	"pr-number": _RELEASE_PR_INPUT,
	"candidate-id": _PREPARED_CANDIDATE_INPUT,
}


def _provider_internal_write_workflow(workflow: _Workflow, is_quality_gate_provider: bool) -> bool:
	return is_quality_gate_provider and workflow.path in {
		".github/workflows/release-publish.yml",
		".github/workflows/image-writer.yml",
	}


def _provider_internal_permission_findings(workflow: _Workflow) -> list[Finding]:
	"""Check the shared reusable jobs that receive the caller's scoped write token."""
	writer = workflow.path.endswith("/image-writer.yml")
	job_id = "push-images" if writer else "publish"
	write_scope = "packages" if writer else "contents"
	expected_permissions = {
		"contents": "read",
		"pull-requests": "read",
		"actions": "read",
		"packages": "read",
	}
	expected_permissions[write_scope] = "write"
	jobs = {job.id: job for job in workflow.jobs}
	job = jobs.get(job_id)
	if (
		workflow.events != {"workflow_call"}
		or set(jobs) != {job_id}
		or job is None
		or (writer and job.name != "Push images")
		or _permission_map(job) != expected_permissions
	):
		return [
			_workflow_finding(
				workflow.path,
				"shared reusable writer permissions or call boundary are invalid",
				"keep one workflow_call job with only its required job-scoped write permission",
			)
		]
	return []


def _full_shared_reference(job: _WorkflowJob, filename: str) -> str | None:
	pattern = re.compile(
		rf"{re.escape(_QUALITY_GATE_PROVIDER)}/\.github/workflows/{re.escape(filename)}@([0-9a-f]{{40}})"
	)
	match = pattern.fullmatch(job.uses or "")
	return match.group(1) if match else None


def _pull_request_section(on_text: str) -> list[str] | None:
	lines = on_text.splitlines()
	start = next(
		(index for index, line in enumerate(lines) if re.fullmatch(r"  pull_request:\s*", line)),
		None,
	)
	if start is None:
		return None
	section: list[str] = []
	for line in lines[start + 1 :]:
		indent = len(line) - len(line.lstrip(" "))
		if line and indent <= _JOB_LEVEL:
			break
		section.append(line)
	return section


def _listed_pull_request_branches(section: list[str], branch_index: int) -> tuple[str, ...] | None:
	branches: list[str] = []
	for line in section[branch_index + 1 :]:
		indent = len(line) - len(line.lstrip(" "))
		if line and indent <= _PROPERTY_LEVEL:
			break
		if not line:
			continue
		match = re.fullmatch(r"\s{6}-\s*['\"]?([^'\"\s]+)['\"]?\s*", line)
		if match is None:
			return None
		branches.append(match.group(1))
	return tuple(branches)


def _pull_request_branches(on_text: str) -> tuple[str, ...] | None:
	section = _pull_request_section(on_text)
	if section is None:
		return None
	if any(re.match(r"^    branches-ignore\s*:", line) for line in section):
		return None
	indices = [index for index, line in enumerate(section) if re.match(r"^    branches\s*:", line)]
	if len(indices) != 1:
		return None
	branch_line = section[indices[0]]
	inline = re.fullmatch(r"\s*branches\s*:\s*\[(.*)\]\s*", branch_line)
	if inline:
		return tuple(_unquote_scalar(value.strip()) for value in inline.group(1).split(","))
	if re.fullmatch(r"\s*branches\s*:\s*", branch_line) is None:
		return None
	return _listed_pull_request_branches(section, indices[0])


def _has_default_pull_request_filter(on_text: str) -> bool:
	return _pull_request_branches(on_text) in {("main",), ("master",)}


def _release_route_trigger_is_approved(workflow: _Workflow) -> bool:
	return (
		workflow.path == ".github/workflows/release.yml"
		and workflow.events == {"pull_request", "workflow_dispatch"}
		and re.search(r"(?m)^    types: \[closed\]$", workflow.on_text) is not None
		and _has_default_pull_request_filter(workflow.on_text)
		and not any(event in workflow.events for event in {"pull_request_target", "workflow_run"})
	)


def _release_route_jobs(
	workflow: _Workflow,
) -> tuple[_WorkflowJob, _WorkflowJob, _WorkflowJob | None] | None:
	jobs = {job.id: job for job in workflow.jobs}
	prepare = jobs.get("prepare")
	publish = jobs.get("publish")
	writer = jobs.get("write-image")
	publish_needs = _needs_tokens(publish) if publish is not None else None
	writer_needs = _needs_tokens(writer) if writer is not None else None
	if (
		prepare is None
		or publish is None
		or not prepare.is_reusable
		or prepare.has_steps
		or prepare.condition
		!= "github.event_name == 'workflow_dispatch' || github.event.pull_request.merged == true"
		or not publish.is_reusable
		or publish.has_steps
		or publish.condition != "needs.prepare.outputs.status == 'build-required'"
		or publish_needs is None
		or "prepare" not in publish_needs
		or "build-and-verify" not in publish_needs
		or (
			writer is not None
			and (
				not writer.is_reusable
				or writer.name != "Write image"
				or writer.has_steps
				or writer.condition != "needs.prepare.outputs.status == 'build-required'"
				or writer.has_matrix
				or writer_needs is None
				or "prepare" not in writer_needs
				or "build-and-verify" not in writer_needs
				or "write-image" not in publish_needs
			)
		)
	):
		return None
	return prepare, publish, writer


def _configured_image_producers(root: Path, workflow: _Workflow) -> list[dict[str, object]] | None:
	config_path = root / ".release" / "publisher.toml"
	try:
		if config_path.parent.is_symlink() or config_path.is_symlink() or not config_path.is_file():
			raise ValueError("publisher configuration is missing or is not a regular file")
		if config_path.stat().st_size > 1024 * 1024:
			raise ValueError("publisher configuration exceeds 1 MiB")
		content = config_path.read_bytes()
		if len(content) > 1024 * 1024:
			raise ValueError("publisher configuration exceeds 1 MiB")
		config = tomllib.loads(content.decode("utf-8"))
		validate_config(config)
		if config.get("workflow") != workflow.path:
			raise ValueError("publisher configuration names a different workflow")
		producers = config["producers"]
		image_producers = [
			producer for producer in producers if producer.get("kind", "archive") == "image"
		]
		if not image_producers:
			raise ValueError("image writer route has no configured image producers")
	except (OSError, UnicodeError, TypeError, ValueError):
		return None
	return image_producers


def _image_producer_configuration_finding(workflow: _Workflow) -> Finding:
	return _workflow_finding(
		workflow.path,
		"image writer route has no valid matching publisher configuration",
		"declare the release workflow and every image producer in .release/publisher.toml",
	)


def _resolve_image_producer_job(
	workflow: _Workflow, producer: dict[str, object]
) -> _WorkflowJob | None:
	producer_name = producer.get("job")
	if not isinstance(producer_name, str) or not producer_name.strip() or "${{" in producer_name:
		return None
	matches = [job for job in workflow.jobs if job.name == producer_name]
	if len(matches) != 1:
		return None
	job = matches[0]
	if job.name is None or "${{" in job.name or job.is_reusable or job.has_matrix:
		return None
	return job


def _image_producer_job_findings(
	workflow: _Workflow,
	writer_needs: frozenset[str] | None,
	producer: dict[str, object],
) -> list[Finding]:
	job = _resolve_image_producer_job(workflow, producer)
	if job is None:
		return [
			_workflow_finding(
				workflow.path,
				"image producer does not map to one local job with a unique literal name",
				"set each configured producer job to one unique literal local job name",
			)
		]
	findings: list[Finding] = []
	permissions = _permission_map(job)
	if (
		permissions.get("contents") != "read"
		or permissions.get("packages") != "read"
		or any(value not in {"read", "none"} for value in permissions.values())
	):
		findings.append(
			_workflow_finding(
				workflow.path,
				"image producer job lacks explicit read-only contents and packages permissions",
				"grant the producer job contents: read and packages: read "
				"without write permissions",
			)
		)
	if writer_needs is None or job.id not in writer_needs:
		findings.append(
			_workflow_finding(
				workflow.path,
				"image writer does not directly depend on every configured image producer",
				"add each producer job ID as a direct write-image needs entry",
			)
		)
	return findings


def _image_producer_findings(root: Path, workflow: _Workflow) -> list[Finding]:
	"""Bind every configured image producer to a static local job before writer dispatch."""
	jobs = {job.id: job for job in workflow.jobs}
	writer = jobs.get("write-image")
	if (
		workflow.path != ".github/workflows/release.yml"
		or writer is None
		or _full_shared_reference(writer, "image-writer.yml") is None
	):
		return []
	image_producers = _configured_image_producers(root, workflow)
	if image_producers is None:
		return [_image_producer_configuration_finding(workflow)]
	writer_needs = _needs_tokens(writer)
	return [
		finding
		for producer in image_producers
		for finding in _image_producer_job_findings(workflow, writer_needs, producer)
	]


def _provider_release_route_is_approved(
	prepare: _WorkflowJob, publish: _WorkflowJob, writer: _WorkflowJob | None
) -> bool:
	return (
		prepare.uses == "./.github/workflows/release-prepare.yml"
		and _input_map(prepare) == _RELEASE_PR_INPUTS
		and publish.uses == "./.github/workflows/release-publish.yml"
		and _input_map(publish) == _PREPARED_CANDIDATE_INPUTS
		and writer is None
	)


def _consumer_release_route_is_approved(
	prepare: _WorkflowJob, publish: _WorkflowJob, writer: _WorkflowJob | None
) -> bool:
	if _input_map(prepare) != _RELEASE_PR_INPUTS:
		return False
	if _input_map(publish) != _PREPARED_CANDIDATE_INPUTS:
		return False
	prepare_sha = _full_shared_reference(prepare, "release-prepare.yml")
	publish_sha = _full_shared_reference(publish, "release-publish.yml")
	if prepare_sha is None or publish_sha != prepare_sha:
		return False
	if writer is not None and (
		_full_shared_reference(writer, "image-writer.yml") != prepare_sha
		or _input_map(writer) != _PREPARED_CANDIDATE_INPUTS
	):
		return False
	return True


def _approved_release_writer_route(workflow: _Workflow, is_quality_gate_provider: bool) -> bool:
	"""Allow only the merged/recovery dispatcher to call the shared publisher and image writer."""
	if not _release_route_trigger_is_approved(workflow):
		return False
	route_jobs = _release_route_jobs(workflow)
	if route_jobs is None:
		return False
	prepare, publish, writer = route_jobs
	if is_quality_gate_provider:
		return _provider_release_route_is_approved(prepare, publish, writer)
	return _consumer_release_route_is_approved(prepare, publish, writer)


def _release_writer_permission_findings(
	workflow: _Workflow,
	write_permissions: list[tuple[str | None, str]],
	is_quality_gate_provider: bool,
) -> list[Finding]:
	"""Enforce the exact reusable jobs and their complete job-scoped token permissions."""
	findings: list[Finding] = []
	jobs = {job.id: job for job in workflow.jobs}
	allowed = {
		("contents", "publish"),
		*([("packages", "write-image")] if "write-image" in jobs else []),
	}
	actual = {(scope, job_id) for scope, job_id in write_permissions}
	if actual != allowed:
		findings.append(
			_workflow_finding(
				workflow.path,
				"release writes are not limited to the shared publisher and dedicated image "
				"writer calls",
				"grant contents: write only to publish and packages: write only to write-image",
			)
		)
	for scope, job_id, provider_workflow in (
		("contents", "publish", "release-publish.yml"),
		*([("packages", "write-image", "image-writer.yml")] if "write-image" in jobs else []),
	):
		job = jobs.get(job_id)
		valid_reference = (
			job is not None
			and job.is_reusable
			and not job.has_steps
			and (
				job.uses == f"./.github/workflows/{provider_workflow}"
				if (
					is_quality_gate_provider
					and workflow.path == ".github/workflows/release.yml"
					and provider_workflow == "release-publish.yml"
				)
				else _full_shared_reference(job, provider_workflow) is not None
			)
		)
		permissions = _permission_map(job) if job is not None else {}
		needs = _needs_tokens(job) if job is not None else None
		expected = {**_RELEASE_READ_PERMISSIONS, scope: "write"}
		if (
			not valid_reference
			or permissions != expected
			or job is None
			or job.condition != "needs.prepare.outputs.status == 'build-required'"
			or needs is None
			or "prepare" not in needs
			or "build-and-verify" not in needs
		):
			findings.append(
				_workflow_finding(
					workflow.path,
					f"{scope}: write is not scoped to the exact shared {provider_workflow} call",
					"use the full pinned shared workflow call with only the required "
					"job permissions",
				)
			)
	return findings


def _workflow_findings(
	root: Path, workflows: list[_Workflow], is_quality_gate_provider: bool
) -> list[Finding]:
	findings = [
		finding
		for workflow in workflows
		for finding in _single_workflow_findings(workflow, is_quality_gate_provider)
	]
	findings.extend(
		finding for workflow in workflows for finding in _image_producer_findings(root, workflow)
	)
	quality_jobs = [
		(job, workflow)
		for workflow in workflows
		for job in workflow.jobs
		if job.id.casefold() == "quality-gate" or (job.name or "").casefold() == "quality gate"
	]
	if len(quality_jobs) != 1:
		findings.append(
			_workflow_finding(
				".github/workflows",
				"workflow must expose exactly one stable Quality Gate job identity",
				"keep one job named quality-gate",
			)
		)
	elif "pull_request" not in quality_jobs[0][1].events:
		findings.append(
			_workflow_finding(
				quality_jobs[0][1].path,
				"Quality Gate job is not reachable from pull requests",
				"declare the pull_request trigger on the Quality Gate workflow",
			)
		)
	if len(quality_jobs) == 1 and not _has_default_push_trigger(quality_jobs[0][1].on_text):
		findings.append(
			_workflow_finding(
				quality_jobs[0][1].path,
				"Quality Gate job is not reachable from the default branch",
				"declare the default-branch push trigger on the Quality Gate workflow",
			)
		)
	return findings


def _workflow_reference_findings(workflow: _Workflow) -> list[Finding]:
	return [
		_workflow_finding(
			workflow.path,
			"workflow reference is not pinned to a full commit SHA",
			"pin the reference to a 40-character commit SHA",
		)
		for reference in workflow.uses
		if not reference.startswith("./")
		and ("@" not in reference or not _SHA.fullmatch(reference.rsplit("@", 1)[-1]))
	]


def _has_default_push_trigger(on_text: str) -> bool:
	push_text = _push_trigger_text(on_text)
	if push_text is None:
		return False
	branches = _push_branches(push_text)
	if branches is None:
		if re.search(r"(?m)^[ ]{4}(?:branches-ignore|tags|tags-ignore)[ ]*:", push_text):
			return False
		return True
	return bool(set(branches) & _DEFAULT_BRANCHES)


def _push_trigger_text(on_text: str) -> str | None:
	match = re.search(
		r"(?ms)^[ ]{2}push[ ]*:[ ]*$.*?(?=^[ ]{2}[a-z_][a-z0-9_-]*[ ]*:|\Z)",
		on_text,
	)
	return match.group(0) if match else None


def _push_branches(push_text: str) -> tuple[str, ...] | None:
	match = re.search(r"(?m)^[ ]{4}branches[ ]*:[ ]*(?P<inline>[^\n]*)$", push_text)
	if match is None:
		return None
	inline = match.group("inline").strip()
	if inline:
		if not (inline.startswith("[") and inline.endswith("]")):
			return ()
		return tuple(
			_unquote_scalar(branch.strip()).casefold()
			for branch in inline[1:-1].split(",")
			if branch.strip()
		)
	return tuple(
		_unquote_scalar(item.group(1)).casefold()
		for item in re.finditer(r"(?m)^[ ]{6}-[ ]*([^\s#]+)", push_text[match.end() :])
	)


def _is_deployment_only(workflow: _Workflow) -> bool:
	if not workflow.events or not workflow.events <= {"push", "workflow_dispatch"}:
		return False
	if workflow.events == {"workflow_dispatch"}:
		return True
	push_text = _push_trigger_text(workflow.on_text)
	if push_text is None:
		return False
	branches = _push_branches(push_text)
	return branches is not None and bool(branches) and set(branches) <= _DEFAULT_BRANCHES


def _is_repository_root(path: Path) -> bool:
	try:
		requested_root = path.resolve()
		result = subprocess.run(
			[
				"git",
				"-C",
				str(requested_root),
				"rev-parse",
				"--show-toplevel",
			],
			capture_output=True,
			check=False,
			timeout=5,
		)
		if result.returncode:
			return False
		git_root = Path(result.stdout.decode("utf-8").strip()).resolve()
	except (OSError, RuntimeError, UnicodeError, subprocess.TimeoutExpired):
		return False
	return os.path.normcase(str(git_root)) == os.path.normcase(str(requested_root))


def _is_quality_gate_origin(repository_root: Path) -> bool:
	"""Bind provider-only workflow exceptions to the checkout's actual origin."""
	github_repository = os.environ.get("GITHUB_REPOSITORY")
	if github_repository and github_repository.casefold() != _QUALITY_GATE_PROVIDER.casefold():
		return False
	if not _is_repository_root(repository_root):
		return False
	try:
		result = subprocess.run(
			["git", "-C", str(repository_root), "remote", "get-url", "origin"],
			capture_output=True,
			check=False,
			timeout=5,
		)
	except (OSError, subprocess.TimeoutExpired):
		return False
	if result.returncode:
		return False
	try:
		remote = result.stdout.decode("utf-8").strip()
	except UnicodeError:
		return False
	if remote.startswith("git@github.com:"):
		path = remote.removeprefix("git@github.com:")
	else:
		try:
			parsed = urlsplit(remote)
			port = parsed.port
		except ValueError:
			return False
		if (
			parsed.scheme.casefold() not in {"https", "ssh"}
			or (parsed.hostname or "").casefold() != "github.com"
			or port not in {None, 22 if parsed.scheme.casefold() == "ssh" else 443}
			or parsed.query
			or parsed.fragment
		):
			return False
		if parsed.password or (
			parsed.username and not (parsed.scheme.casefold() == "ssh" and parsed.username == "git")
		):
			return False
		path = parsed.path.lstrip("/")
	path = path.removesuffix(".git").rstrip("/")
	return path.casefold() == _QUALITY_GATE_PROVIDER.casefold()


def workflow_result(
	root: Path,
	manifest: Manifest,
	*,
	repository_root: Path | None = None,
) -> CheckResult:
	workflow_paths = _workflow_paths(root)
	if not workflow_paths:
		return _unchecked(
			"repository.workflow",
			"required GitHub Actions workflow is missing",
			(
				_workflow_finding(
					".github/workflows",
					"no supported workflow file is declared",
					"add a supported GitHub Actions workflow",
				),
			),
		)
	workflows: list[_Workflow] = []
	for path in workflow_paths:
		relative = path.relative_to(root).as_posix()
		try:
			workflows.append(_parse_workflow(path, relative))
		except ValueError as error:
			return _unchecked(
				"repository.workflow",
				"workflow input is not parseable",
				(_workflow_finding(relative, str(error), "repair the workflow YAML and retry"),),
			)
	findings = _workflow_findings(
		root,
		workflows,
		_is_quality_gate_origin(repository_root or root),
	)
	return _failed_or_waived(
		"repository.workflow",
		"workflow hygiene requirements are not satisfied",
		findings,
		manifest,
	)


def _markdown_files(root: Path) -> Iterator[tuple[str, Path]]:
	for path in sorted(root.rglob("*.md")):
		if path.is_symlink() or not path.is_file():
			continue
		yield path.relative_to(root).as_posix(), path


def documentation_link_result(root: Path, manifest: Manifest) -> CheckResult:
	findings: list[Finding] = []
	for relative, path in _markdown_files(root):
		try:
			text = path.read_text(encoding="utf-8")
		except (OSError, UnicodeError):
			return _unchecked(
				"repository.documentation.links",
				"Markdown document cannot be read",
				(
					_workflow_finding(
						relative,
						"document is unreadable or not valid UTF-8",
						"restore a readable Markdown document",
					),
				),
			)
		for match in _MARKDOWN_LINK.finditer(text):
			target = match.group(1).strip()
			if target.startswith("<") and ">" in target:
				target = target[1 : target.index(">")]
			else:
				target = target.split()[0] if target.split() else ""
			target = unquote(target.split("#", 1)[0].split("?", 1)[0])
			if (
				not target
				or target.startswith("/")
				or target.startswith("//")
				or target.startswith("#")
				or _EXTERNAL_LINK.match(target)
			):
				continue
			resolved = (path.parent / target).resolve(strict=False)
			if not resolved.is_relative_to(root) or not resolved.exists():
				findings.append(
					_workflow_finding(
						relative,
						f"internal Markdown link does not resolve: {target}",
						"repair the relative link",
					)
				)
	return _failed_or_waived(
		"repository.documentation.links",
		"internal Markdown links resolve",
		findings,
		manifest,
	)


def documentation_component_result(root: Path, manifest: Manifest) -> CheckResult:
	findings: list[Finding] = []
	texts: list[str] = []
	for relative, path in _markdown_files(root):
		try:
			texts.append(path.read_text(encoding="utf-8").replace("\\", "/"))
		except (OSError, UnicodeError):
			return _unchecked(
				"repository.documentation.components",
				"Markdown document cannot be read",
				(
					_workflow_finding(
						relative,
						"document is unreadable or not valid UTF-8",
						"restore a readable Markdown document",
					),
				),
			)
	if not texts:
		return CheckResult(
			"repository.documentation.components",
			Status.NOT_APPLICABLE,
			"no Markdown document declares a Python component path",
			recovery_action="document declared component paths when Markdown documentation exists",
		)
	for component in manifest.python:
		pattern = re.compile(rf"(?<![\w.-]){re.escape(component.path)}(?![\w.-])")
		if not any(pattern.search(text) for text in texts):
			findings.append(
				_workflow_finding(
					component.path,
					"declared Python component path is not documented",
					"document the manifest component path",
				)
			)
	return _failed_or_waived(
		"repository.documentation.components",
		"documented component paths match the manifest",
		findings,
		manifest,
	)


def documentation_results(root: Path, manifest: Manifest) -> tuple[CheckResult, ...]:
	return (
		documentation_link_result(root, manifest),
		documentation_component_result(root, manifest),
	)
