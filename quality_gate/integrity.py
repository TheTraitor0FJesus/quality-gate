"""Repository-level Git, workflow, and documentation contract checks."""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote

from .contracts import CheckResult, Finding, Manifest, Status
from .integrity_origin import _is_quality_gate_origin
from .integrity_workflow_common import _workflow_finding
from .integrity_workflow_parser import _parse_workflow, _Workflow
from .integrity_workflows import (
	_DEPENDABOT_AUTO_MERGE_WORKFLOW_PATH,
	_workflow_findings,
	_workflow_paths,
)

_CONFLICT_MARKER = re.compile(r"^\s*(?:<{7}|={7}|>{7})(?:\s|$)")
MAX_CONFLICT_FILE_BYTES = 8 * 1024 * 1024
MAX_CONFLICT_SCAN_BYTES = 256 * 1024 * 1024
MAX_CONFLICT_FINDINGS = 4096
_EXTERNAL_LINK = re.compile(r"^[a-z][a-z0-9+.-]*:", re.IGNORECASE)
_MARKDOWN_LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\n]+)\)")
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


class _ConflictScanError(Exception):
	"""A specific candidate file could not be completely scanned within limits."""

	def __init__(self, finding: Finding) -> None:
		super().__init__(finding.message)
		self.finding = finding


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


def _unchecked(
	check_id: str,
	summary: str,
	findings: tuple[Finding, ...],
	*,
	omitted_findings: int | None = 0,
) -> CheckResult:
	return CheckResult(
		check_id=check_id,
		status=Status.UNCHECKED,
		summary=summary,
		findings=findings,
		recovery_action="restore a readable, supported candidate and retry the quality gate",
		omitted_findings=omitted_findings,
	)


def _failed_or_waived(
	check_id: str,
	summary: str,
	findings: list[Finding],
	manifest: Manifest,
	*,
	non_waivable_targets: frozenset[str] = frozenset(),
) -> CheckResult:
	if not findings:
		return CheckResult(check_id, Status.PASSED, summary)
	remaining = [
		finding
		for finding in findings
		if finding.path in non_waivable_targets
		or manifest.resolve_waiver(check_id, finding.path) is None
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


def git_conflict_markers(
	manifest: Manifest | None,
	sources: Iterable[tuple[str, bytes]],
	*,
	max_findings: int | None = None,
) -> CheckResult:
	"""Apply the Gate's conflict-marker and exact-waiver semantics to explicit bytes."""
	result, _file_results = _conflict_marker_results(
		manifest, sources, max_findings=max_findings, include_file_results=False
	)
	return result


def _conflict_marker_result(manifest: Manifest | None, findings: list[Finding]) -> CheckResult:
	check_id = "repository.git.conflict_markers"
	summary = "candidate text contains merge conflict markers"
	if manifest is not None:
		return _failed_or_waived(check_id, summary, findings, manifest)
	if findings:
		return CheckResult(
			check_id,
			Status.FAILED,
			summary,
			findings=tuple(findings),
			recovery_action="fix the reported repository finding",
		)
	return CheckResult(check_id, Status.PASSED, "candidate text contains no merge conflict markers")


def git_conflict_marker_results(
	manifest: Manifest | None,
	sources: Iterable[tuple[str, bytes]],
	*,
	max_findings: int | None = None,
) -> tuple[CheckResult, tuple[CheckResult, ...]]:
	"""Return aggregate and aligned per-file results from one marker scan."""
	return _conflict_marker_results(
		manifest, sources, max_findings=max_findings, include_file_results=True
	)


def _conflict_marker_file_findings(
	path: str, data: bytes, finding_count: int, max_findings: int | None
) -> list[Finding] | CheckResult:
	if b"\x00" in data:
		return []
	try:
		text = data.decode("utf-8")
	except UnicodeError:
		return []
	findings: list[Finding] = []
	for line_number, line in enumerate(text.splitlines(), start=1):
		if not _CONFLICT_MARKER.match(line):
			continue
		if max_findings is not None and finding_count + len(findings) >= max_findings:
			return _unchecked(
				"repository.git.conflict_markers",
				"conflict-marker findings exceed the scan limit",
				(
					Finding(
						path,
						line_number,
						"conflict-marker finding limit was reached",
						"reduce the unresolved markers and retry",
					),
				),
				omitted_findings=None,
			)
		findings.append(
			Finding(
				path,
				line_number,
				"merge conflict marker is present",
				"resolve the merge conflict",
			)
		)
	return findings


def _conflict_marker_results(
	manifest: Manifest | None,
	sources: Iterable[tuple[str, bytes]],
	*,
	max_findings: int | None,
	include_file_results: bool,
) -> tuple[CheckResult, tuple[CheckResult, ...]]:
	findings: list[Finding] = []
	file_findings: list[list[Finding]] | None = [] if include_file_results else None
	for path, data in sources:
		current = _conflict_marker_file_findings(path, data, len(findings), max_findings)
		if isinstance(current, CheckResult):
			return current, ()
		findings.extend(current)
		if file_findings is not None:
			file_findings.append(current)

	return (
		_conflict_marker_result(manifest, findings),
		tuple(_conflict_marker_result(manifest, current) for current in file_findings or ()),
	)


def _conflict_file_error(path: str, message: str, action: str) -> _ConflictScanError:
	return _ConflictScanError(Finding(path, message=message, action=action))


def _read_conflict_file(entry: _TreeEntry, remaining_bytes: int) -> bytes:
	try:
		if entry.abs_path.stat().st_size > MAX_CONFLICT_FILE_BYTES:
			raise _conflict_file_error(
				entry.path,
				"file exceeds the conflict-marker scan size limit",
				"reduce the file size and retry the quality gate",
			)
		with entry.abs_path.open("rb") as source:
			content = source.read(MAX_CONFLICT_FILE_BYTES + 1)
	except _ConflictScanError:
		raise
	except OSError as error:
		raise _conflict_file_error(
			entry.path,
			"file content is unreadable",
			"restore access and retry the quality gate",
		) from error
	if len(content) > MAX_CONFLICT_FILE_BYTES:
		raise _conflict_file_error(
			entry.path,
			"file exceeds the conflict-marker scan size limit",
			"reduce the file size and retry the quality gate",
		)
	if len(content) > remaining_bytes:
		raise _conflict_file_error(
			entry.path,
			"repository exceeds the conflict-marker scan byte limit",
			"reduce the candidate bytes and retry the quality gate",
		)
	return content


def _conflict_sources(scan: _TreeScan) -> Iterator[tuple[str, bytes]]:
	total_bytes = 0
	for entry in scan.entries:
		if entry.is_dir or entry.is_symlink:
			continue
		content = _read_conflict_file(entry, MAX_CONFLICT_SCAN_BYTES - total_bytes)
		total_bytes += len(content)
		yield entry.path, content


def _git_conflict_result(manifest: Manifest, scan: _TreeScan) -> CheckResult:
	if scan.unreadable:
		return _unchecked(
			"repository.git.conflict_markers",
			"Git candidate cannot be read",
			tuple(scan.unreadable),
		)
	try:
		return git_conflict_markers(
			manifest, _conflict_sources(scan), max_findings=MAX_CONFLICT_FINDINGS
		)
	except _ConflictScanError as error:
		return _unchecked(
			"repository.git.conflict_markers",
			"Git candidate cannot be completely scanned",
			(error.finding,),
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
		non_waivable_targets=frozenset({_DEPENDABOT_AUTO_MERGE_WORKFLOW_PATH}),
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


def required_documents_result(root: Path, manifest: Manifest) -> CheckResult:
	findings: list[Finding] = []
	unreadable = False
	for document in manifest.repository.required_documents:
		target = root / document
		try:
			if not target.is_file():
				findings.append(
					Finding(
						document,
						message="required document is missing",
						action=f"restore {document}",
					)
				)
			elif not target.read_bytes().strip():
				findings.append(
					Finding(
						document, message="required document is empty", action=f"restore {document}"
					)
				)
		except OSError:
			unreadable = True
			findings.append(
				Finding(
					document,
					message="required document is unreadable",
					action=f"restore {document}",
				)
			)
	return CheckResult(
		check_id="manifest.documents",
		status=(Status.UNCHECKED if unreadable else Status.FAILED if findings else Status.PASSED),
		summary="required documents satisfy the schema 2 contract"
		if not findings
		else "required documents are incomplete",
		findings=tuple(findings),
		recovery_action=(
			"restore access to every unreadable document and run check again"
			if unreadable
			else "restore every required document and run check again"
			if findings
			else None
		),
	)
