"""Repository-level Git, workflow, and documentation contract checks."""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote

from .contracts import CheckResult, Finding, Manifest, Status

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
	is_reusable: bool
	permissions: tuple[tuple[str | None, str], ...]


@dataclass(frozen=True, slots=True)
class _Workflow:
	path: str
	uses: tuple[str, ...]
	jobs: tuple[_WorkflowJob, ...]
	on_text: str
	events: frozenset[str]
	permissions: tuple[tuple[str | None, str], ...]
	concurrency_group: str | None
	concurrency_cancel: str | None


def _key(line: str) -> tuple[int, str, str] | None:
	match = _KEY.match(line.rstrip())
	if not match:
		return None
	key = match.group("key").strip().strip("'\"")
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
			raw_value = item[2].strip("'\"")
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
				entries.append((child[1], child[2].strip("'\"")))
		return tuple(entries)
	return ()


def _workflow_jobs(keys: list[tuple[int, str, str] | None]) -> list[_WorkflowJob]:
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
		properties = {
			item[1]: item[2].strip("'\"")
			for item in keys[start + 1 : end]
			if item and item[0] == _PROPERTY_LEVEL
		}
		job_item = keys[start]
		assert job_item is not None
		jobs.append(
			_WorkflowJob(
				job_item[1],
				properties.get("name"),
				properties.get("timeout-minutes"),
				"uses" in properties,
				_permission_entries(keys, start + 1, end, _PROPERTY_LEVEL),
			)
		)
	return jobs


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
				group = child[2].strip("'\"") or None
			elif child and child[0] == _JOB_LEVEL and child[1] == "cancel-in-progress":
				cancel = child[2].strip("'\"").casefold() or None
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
		match.group(1).strip("'\"")
		for line in lines
		if (match := _WORKFLOW_REFERENCE.match(line)) is not None
	)
	jobs = _workflow_jobs(keys)
	permissions = _permission_entries(keys, 0, len(keys), _TOP_LEVEL)
	concurrency_group, concurrency_cancel = _workflow_concurrency(keys)
	return _Workflow(
		relative,
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


def _single_workflow_findings(workflow: _Workflow) -> list[Finding]:
	findings: list[Finding] = []
	findings.extend(_workflow_reference_findings(workflow))
	is_pull_request = bool({"pull_request", "pull_request_target"} & workflow.events)
	findings.extend(_workflow_permission_findings(workflow, is_pull_request))
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
	return findings


def _workflow_permission_findings(workflow: _Workflow, is_pull_request: bool) -> list[Finding]:
	findings: list[Finding] = []
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
	if write_permissions and is_pull_request:
		findings.append(
			_workflow_finding(
				workflow.path,
				"write-capable jobs are reachable from pull requests",
				"remove write permissions or isolate the jobs in a deployment-only workflow",
			)
		)
	elif write_permissions:
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
	return findings


def _workflow_findings(workflows: list[_Workflow]) -> list[Finding]:
	findings = [
		finding for workflow in workflows for finding in _single_workflow_findings(workflow)
	]
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
			branch.strip().strip("'\"").casefold()
			for branch in inline[1:-1].split(",")
			if branch.strip()
		)
	return tuple(
		item.group(1).strip("'\"").casefold()
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


def workflow_result(root: Path, manifest: Manifest) -> CheckResult:
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
	findings = _workflow_findings(workflows)
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
