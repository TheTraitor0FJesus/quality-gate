"""Deterministic quality checks used by the local hook and CI."""

from __future__ import annotations

import hashlib
import io
import json
import locale
import logging
import os
import platform
import re
import subprocess
import sys
import tempfile
import threading
import time
import tokenize
import tomllib
from collections.abc import Callable, Iterable, Iterator
from contextlib import AbstractContextManager, ExitStack, contextmanager, nullcontext
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Literal

from . import __version__
from .admission import (
	AdmissionError,
	child_admission_environment,
	gate_admission,
	running_in_ci,
)
from .contracts import (
	DEFAULT_COMMAND_TIMEOUT_SECONDS,
	DEFAULT_GATE_TIMEOUT_SECONDS,
	CheckResult,
	Finding,
	Manifest,
	Status,
	ValidationError,
	Verdict,
	WebComponent,
	load_manifest,
	redact,
)
from .contracts import PythonComponent as ManifestPythonComponent
from .distribution import DistributionError
from .edit_protocol import EditError
from .integrity import documentation_results, git_integrity_results, workflow_result
from .launcher import PreparedEnvironment, prepare, prepare_bootstrap
from .lessons import lessons_result
from .native_cache import (
	NativeToolCache,
	NativeToolCacheError,
	NativeToolCacheIdentity,
	NativeToolCacheSelection,
	native_tool_source_workspace,
)
from .operation import (
	GateOperation,
	OperationTimeoutError,
	current_operation,
	observe_check,
	run_probe,
)
from .reporting import render
from .runtime import RuntimeIdentity, RuntimeUnavailable, runtime_fingerprint
from .secrets import secret_audit_result, secret_candidate_result, secret_history_result
from .snapshot import CandidateSnapshot, SnapshotError, candidate_snapshot, remove_snapshot
from .temp_workspace import TemporaryWorkspaceError, temporary_workspace

_LOGGER = logging.getLogger(__name__)
MANIFEST_NAME = "quality-gate.toml"
POLICY_DIR = Path(__file__).resolve().parent / "policy"
EXIT_UNCHECKED = 2
MAX_COMMAND_OUTPUT_CHARS = 16_384
MAX_COMMAND_OUTPUT_BYTES = MAX_COMMAND_OUTPUT_CHARS * 4 + 1
RUFF_THREADS_ENV = "QUALITY_GATE_RUFF_THREADS"
DEFAULT_RUFF_THREADS = 4
MAX_RUFF_THREADS = 64
MAX_DEPTRY_REPORT_BYTES = 1024 * 1024
MAX_DEPTRY_SOURCE_BYTES = 8 * 1024 * 1024
MAX_DEPTRY_TEXT_CHARS = 1000
PROCESS_CLEANUP_TIMEOUT_SECONDS = 1.0
CHECK_ID_COMPONENT_PARTS = 3
_PYTEST_SUMMARY_COUNT = re.compile(
	(
		r"\b(?P<count>\d+)\s+"
		r"(?P<outcome>passed|failed|errors?|skipped|xfailed|xpassed|deselected|warnings?)\b"
	),
	re.IGNORECASE,
)
_PYTEST_SUMMARY_DURATION = re.compile(r"\bin\s+\d+(?:\.\d+)?s\b", re.IGNORECASE)
_PYTEST_NO_TESTS = re.compile(r"\bno tests ran\b", re.IGNORECASE)
BIOME_POLICY = "biome.toml"
BIOME_CONFIG = "biome.json"
BIOME_MAX_FILE_BYTES = 5 * 1024 * 1024
_DEPTRY_MODULE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,199}$")
_DEPTRY_INLINE_IGNORE = re.compile(r"#\s*deptry:\s*ignore(?:\s*\[[A-Z0-9,\s]+\])?(?=\s|$)")
_DEPTRY_SUPPRESSION_KEYS = {
	"exclude",
	"extend_exclude",
	"ignore",
	"ignore_notebooks",
	"per_rule_ignores",
}


class OutputReadError(RuntimeError):
	"""A subprocess output reader could not finish safely."""


class QualityGateError(RuntimeError):
	"""A configuration or quality check prevents completion."""

	exit_code = 1

	def __init__(
		self,
		message: str,
		*,
		check_id: str = "runtime.command",
		exit_code: int = 1,
		recovery_action: str = "restore verification and retry the quality gate",
	) -> None:
		super().__init__(redact(message))
		self.check_id = check_id
		self.exit_code = exit_code
		self.recovery_action = recovery_action


def _component_check_index(parts: list[str], domain: str) -> int | None:
	if len(parts) < CHECK_ID_COMPONENT_PARTS:
		return None
	if parts[0] != domain or not parts[1].startswith("component_"):
		return None
	try:
		index = int(parts[1].removeprefix("component_")) - 1
	except ValueError:
		return None
	return index if index >= 0 else None


def _python_component_check_scope(
	root: Path, component: ManifestPythonComponent, suffix: str
) -> str:
	if suffix.startswith("test_path_"):
		try:
			missing_index = int(suffix.removeprefix("test_path_")) - 1
		except ValueError:
			missing_index = -1
		missing_paths = tuple(path for path in component.test_paths if not (root / path).exists())
		if 0 <= missing_index < len(missing_paths):
			return missing_paths[missing_index]
	if suffix in {"pytest", "coverage", "coverage_report"}:
		test_paths = tuple(path for path in component.test_paths if (root / path).exists())
		return f"{component.path}; tests={','.join(test_paths)}"
	if suffix == "deptry":
		return f"{component.path}; dependencies={','.join(component.dependency_inputs)}"
	return component.path


_FIXED_CHECK_SCOPES = {
	"manifest.documents": "staged required documents",
	"secrets.candidate": "staged candidate tree",
	"secrets.history": "requested base-to-head Git range",
	"secrets.audit": "all reachable Git history",
}


class _OperationResults(list[CheckResult]):
	"""Retain each observed check immediately so a later timeout cannot erase it."""

	def __init__(self, root: Path) -> None:
		super().__init__()
		self.root = root
		self.manifest: Manifest | None = None

	def append(self, result: CheckResult) -> None:
		operation = current_operation()
		if operation is not None:
			operation.record_unmeasured_result(result, scope=self.scope_for(result.check_id))
		super().append(result)

	def extend(self, results: Iterable[CheckResult]) -> None:
		for result in results:
			self.append(result)

	def append_not_run(self, check_id: str, scope: str, reason: str) -> None:
		"""Retain an expected check that a completed prerequisite prevented."""
		operation = current_operation()
		if operation is not None:
			operation.note_not_run(check_id, scope, reason)
		self.append(
			CheckResult(
				check_id,
				Status.UNCHECKED,
				reason,
				recovery_action="complete the prerequisite and rerun the affected checks",
			)
		)

	def scope_for(self, check_id: str) -> str:
		manifest = self.manifest
		parts = check_id.split(".")
		python_index = _component_check_index(parts, "python")
		if (
			manifest is not None
			and python_index is not None
			and python_index < len(manifest.python)
		):
			return _python_component_check_scope(self.root, manifest.python[python_index], parts[2])
		web_index = _component_check_index(parts, "web")
		if manifest is not None and web_index is not None and web_index < len(manifest.web):
			return manifest.web[web_index].root
		if check_id in _FIXED_CHECK_SCOPES:
			return _FIXED_CHECK_SCOPES[check_id]
		if check_id.startswith("repository.git."):
			return "staged repository tree and Git index"
		if check_id.startswith("repository.documentation."):
			return "staged repository documentation"
		if check_id.startswith("web."):
			return "declared web component assets"
		if check_id.startswith("python."):
			return "declared Python components"
		if check_id.startswith("runtime."):
			return "selected policy and verification runtimes"
		return "staged candidate"


def _planned_check_scopes(
	root: Path,
	manifest: Manifest,
	components: list[PythonComponent],
	mode: Literal["check", "audit"],
	*,
	prepared: PreparedEnvironment | None = None,
) -> tuple[tuple[str, str], ...]:
	scope_resolver = _OperationResults(root)
	scope_resolver.manifest = manifest
	checks: list[tuple[str, str]] = [
		("secrets.candidate", "staged candidate tree"),
		(
			("secrets.audit", "all reachable Git history")
			if mode == "audit"
			else ("secrets.history", "requested base-to-head Git range")
		),
	]
	for index, component in enumerate(manifest.web, start=1):
		for suffix in ("biome_lint", "biome_format"):
			check_id = f"web.component_{index}.{suffix}"
			checks.append((check_id, component.root))
	if not components:
		checks.append(("python.components", "declared Python components"))
	checks.extend(_planned_python_check_scopes(root, components, prepared))
	return tuple(
		(check_id, scope if scope else scope_resolver.scope_for(check_id))
		for check_id, scope in checks
	)


def _planned_python_check_scopes(
	root: Path,
	components: list[PythonComponent],
	prepared: PreparedEnvironment | None,
) -> list[tuple[str, str]]:
	checks: list[tuple[str, str]] = []
	for index, component in enumerate(components, start=1):
		prefix = f"python.component_{index}"
		component_scope = component.path.relative_to(root).as_posix()
		if not component.path_exists:
			checks.append((f"{prefix}.path", component_scope))
		for missing_index, path in enumerate(component.missing_test_paths, start=1):
			checks.append(
				(f"{prefix}.test_path_{missing_index}", path.relative_to(root).as_posix())
			)
		dependency_inputs = tuple(
			path.relative_to(root).as_posix() for path in component.dependency_inputs
		)
		checks.append(
			(f"{prefix}.deptry", f"{component_scope}; dependencies={','.join(dependency_inputs)}")
		)
		checks.extend(
			(
				(f"{prefix}.ruff", component_scope),
				(f"{prefix}.format", component_scope),
			)
		)
		if component.typecheck:
			checks.append((f"{prefix}.mypy", component_scope))
		declared_test_paths = tuple(
			path.relative_to(root).as_posix()
			for path in (*component.test_paths, *component.missing_test_paths)
		)
		if component.tests is not None:
			checks.append(
				(
					f"{prefix}.pytest",
					f"{component_scope}; tests={','.join(declared_test_paths)}",
				)
			)
		test_scope = component_scope
		if component.tests is not None:
			test_scope = f"{component_scope}; tests={','.join(declared_test_paths)}"
		checks.append((f"{prefix}.coverage", test_scope))
		if prepared is not None and component.tests is not None and _has_pinned_coverage(prepared):
			checks.append((f"{prefix}.coverage_report", test_scope))
	return checks


def emit(message: str) -> None:
	sys.stdout.write(f"{message}\n")


def _progress(message: str) -> None:
	sys.stderr.write(f"QUALITY GATE: {message}\n")
	sys.stderr.flush()


def decode_subprocess_output(output: bytes | str | None) -> str:
	"""Decode subprocess output across UTF-8 and Windows console encodings."""
	if output is None:
		return ""
	if isinstance(output, str):
		return output
	encodings = ["utf-8", locale.getpreferredencoding(False)]
	for encoding in dict.fromkeys(encodings):
		try:
			return output.decode(encoding)
		except UnicodeDecodeError:
			continue
	return output.decode("utf-8", errors="replace")


@dataclass(frozen=True)
class PythonComponent:
	path: Path
	tests: Path | None
	requirements: Path | None
	typecheck: bool
	test_paths: tuple[Path, ...] = ()
	timeout_seconds: int = 300
	missing_test_paths: tuple[Path, ...] = ()
	path_exists: bool = True
	dependency_inputs: tuple[Path, ...] = ()


@dataclass(frozen=True, slots=True)
class BiomeBinary:
	version: str
	path: str
	sha256: str


def _manifest_error(error: ValidationError) -> QualityGateError:
	return QualityGateError(
		f"{error.path}: {error.message}",
		check_id="manifest.contract",
		exit_code=2,
		recovery_action=f"correct {error.path} and run validate",
	)


def repository_root(root: Path | None) -> Path:
	if root is not None:
		return root.resolve()
	operation = current_operation()
	try:
		result = run_probe(
			["git", "rev-parse", "--show-toplevel"],
			timeout=None if operation is None else operation.command_timeout_seconds,
		)
	except (OSError, subprocess.TimeoutExpired) as error:
		raise QualityGateError(
			"Git could not identify the repository root.",
			check_id="runtime.repository",
			exit_code=EXIT_UNCHECKED,
			recovery_action="run the command inside a Git repository or pass --root",
		) from error
	if result.returncode:
		raise QualityGateError(
			"Run quality-gate inside a Git repository or pass --root.",
			check_id="runtime.repository",
			exit_code=2,
			recovery_action="run the command inside a Git repository or pass --root",
		)
	try:
		return Path(result.stdout.decode("utf-8").strip()).resolve()
	except (OSError, UnicodeError, RuntimeError) as error:
		raise QualityGateError(
			"Git returned an unusable repository root.",
			check_id="runtime.repository",
			exit_code=EXIT_UNCHECKED,
			recovery_action="run the command inside a Git repository or pass --root",
		) from error


def relative_path(root: Path, value: object, field: str) -> Path:
	if not isinstance(value, str) or not value:
		raise QualityGateError(
			f"{field} must be a non-empty relative path.",
			check_id="python.contract",
			exit_code=2,
			recovery_action=f"correct {field} and run validate",
		)
	resolved_root = root.resolve()
	path = (resolved_root / value).resolve()
	if resolved_root not in path.parents and path != resolved_root:
		raise QualityGateError(
			f"{field} must stay inside the repository.",
			check_id="python.contract",
			exit_code=2,
			recovery_action=f"correct {field} and run validate",
		)
	return path


def load_components(root: Path) -> list[PythonComponent]:
	try:
		manifest = load_manifest(root)
	except ValidationError as error:
		raise _manifest_error(error) from error
	components: list[PythonComponent] = []
	for index, item in enumerate(manifest.python, start=1):
		path = relative_path(root, item.path, f"python entry {index}.path")
		test_paths = tuple(
			relative_path(root, test_path, f"python entry {index}.test_paths[{test_index}]")
			for test_index, test_path in enumerate(item.test_paths)
		)
		missing_tests = [test_path for test_path in test_paths if not test_path.exists()]
		existing_tests = tuple(test_path for test_path in test_paths if test_path.exists())
		components.append(
			PythonComponent(
				path=path,
				tests=test_paths[0] if test_paths else None,
				requirements=None,
				typecheck=item.typecheck,
				dependency_inputs=tuple(
					relative_path(
						root,
						dependency_input,
						f"python entry {index}.dependency_inputs[{dependency_index}]",
					)
					for dependency_index, dependency_input in enumerate(item.dependency_inputs)
				),
				test_paths=existing_tests,
				timeout_seconds=item.timeout_seconds,
				missing_test_paths=tuple(missing_tests),
				path_exists=path.is_dir(),
			)
		)
	return components


def _is_excluded(path: PurePosixPath, patterns: tuple[str, ...]) -> bool:
	for pattern in patterns:
		if path.match(pattern):
			return True
		prefix = pattern[:-3].rstrip("/") + "/"
		if pattern.endswith("/**") and path.as_posix().startswith(prefix):
			return True
	return False


def _is_link(path: Path) -> bool:
	is_junction = getattr(path, "is_junction", None)
	return path.is_symlink() or (is_junction is not None and is_junction())


def _unsafe_web_path(root: Path, path: Path) -> bool:
	resolved_root = root.resolve(strict=True)
	resolved_path = path.resolve(strict=True)
	if resolved_path != resolved_root and resolved_root not in resolved_path.parents:
		return True
	relative_parts = path.relative_to(root).parts
	path_chain = (
		root.joinpath(*relative_parts[:index]) for index in range(1, len(relative_parts) + 1)
	)
	return any(_is_link(candidate) for candidate in path_chain)


def _web_assets(
	root: Path,
	component: WebComponent,
	patterns: tuple[str, ...],
) -> tuple[tuple[Path, ...], tuple[Finding, ...]]:
	component_root = root / component.root
	assets: set[Path] = set()
	unsafe: list[Finding] = []
	for pattern in patterns:
		for candidate in component_root.glob(pattern):
			relative = PurePosixPath(candidate.relative_to(component_root).as_posix())
			if _is_excluded(relative, component.exclude):
				continue
			try:
				is_unsafe = _unsafe_web_path(root, candidate)
			except OSError:
				is_unsafe = True
			if is_unsafe:
				unsafe.append(
					Finding(
						path=candidate.relative_to(root).as_posix(),
						message="declared web asset is outside or traverses a symbolic link",
						action="replace it with a project-owned regular file",
					)
				)
				continue
			if not candidate.is_file():
				continue
			assets.add(candidate)
	return tuple(sorted(assets, key=lambda path: path.relative_to(root).as_posix())), tuple(unsafe)


def _web_budget_result(
	root: Path,
	component: WebComponent,
	component_index: int,
	*,
	language: Literal["javascript", "css"],
) -> CheckResult:
	patterns = component.javascript if language == "javascript" else component.css
	label = "JavaScript" if language == "javascript" else "CSS"
	check_id = f"web.component_{component_index}.{language}_budget"
	if not patterns:
		return CheckResult(
			check_id=check_id,
			status=Status.NOT_APPLICABLE,
			summary=f"no {label} assets are declared",
			recovery_action=f"declare {label} asset patterns if this component owns them",
		)
	component_root = root / component.root
	root_parts = component.root.split("/")
	path_chain = tuple(
		root.joinpath(*root_parts[:index]) for index in range(1, len(root_parts) + 1)
	)
	try:
		resolved_root = root.resolve(strict=True)
		resolved_component_root = component_root.resolve(strict=True)
	except OSError:
		return CheckResult(
			check_id=check_id,
			status=Status.UNCHECKED,
			summary=f"{label} asset boundary could not be resolved",
			findings=(
				Finding(
					path=component.root,
					message="declared web component root could not be resolved",
					action="restore a readable project-owned component root",
				),
			),
			recovery_action="restore a readable web component root and retry",
		)
	root_is_unsafe = (
		resolved_component_root != resolved_root
		and resolved_root not in resolved_component_root.parents
	) or any(_is_link(path) for path in path_chain)
	if root_is_unsafe:
		return CheckResult(
			check_id=check_id,
			status=Status.UNCHECKED,
			summary=f"{label} asset boundary is unsafe",
			findings=(
				Finding(
					path=component.root,
					message="declared web component root is or traverses a symbolic link",
					action="use a project-owned regular directory inside the repository",
				),
			),
			recovery_action="declare a regular web component root inside the repository",
		)
	if not component_root.is_dir():
		return CheckResult(
			check_id=check_id,
			status=Status.UNCHECKED,
			summary=f"{label} asset budget is unavailable",
			findings=(
				Finding(
					path=component.root,
					message="declared web component root does not exist",
					action="restore the declared web component root",
				),
			),
			recovery_action="restore the declared web component root and retry the quality gate",
		)
	try:
		assets, unsafe = _web_assets(root, component, patterns)
		sizes = tuple((asset, asset.stat().st_size) for asset in assets)
	except OSError:
		return CheckResult(
			check_id=check_id,
			status=Status.UNCHECKED,
			summary=f"{label} assets could not be measured",
			recovery_action="restore readable project-owned web assets and retry the quality gate",
		)
	if unsafe:
		return CheckResult(
			check_id=check_id,
			status=Status.UNCHECKED,
			summary=f"{label} asset boundary contains an unsafe file",
			findings=unsafe,
			recovery_action="replace linked or external assets with project-owned regular files",
		)
	if not sizes:
		return CheckResult(
			check_id=check_id,
			status=Status.UNCHECKED,
			summary=f"declared {label} patterns match no project-owned assets",
			recovery_action=f"correct the {label} patterns or add the declared assets and retry",
		)
	file_kib = component.javascript_file_kib if language == "javascript" else component.css_file_kib
	total_kib = (
		component.javascript_total_kib if language == "javascript" else component.css_total_kib
	)
	file_limit = file_kib * 1024
	total_limit = total_kib * 1024
	findings = [
		Finding(
			path=asset.relative_to(root).as_posix(),
			message=f"{label} file size {size} bytes exceeds {file_limit} bytes",
			action=f"reduce the file to at most {file_kib} KiB or set a reviewed component limit",
		)
		for asset, size in sizes
		if size > file_limit
	]
	total_size = sum(size for _, size in sizes)
	if total_size > total_limit:
		findings.append(
			Finding(
				path=component.root,
				message=f"total {label} size {total_size} bytes exceeds {total_limit} bytes",
				action=(
					f"reduce component assets to at most {total_kib} KiB or set a reviewed limit"
				),
			)
		)
	if findings:
		return CheckResult(
			check_id=check_id,
			status=Status.FAILED,
			summary=f"{label} assets exceed the configured size budget",
			findings=tuple(findings),
			recovery_action="reduce the staged assets or review and declare a justified limit",
		)
	return CheckResult(
		check_id=check_id,
		status=Status.PASSED,
		summary=f"{len(sizes)} {label} asset(s), {total_size} bytes, within budget",
	)


def _fixed_check_result(result: CheckResult) -> Callable[[], CheckResult]:
	def check() -> CheckResult:
		return result

	return check


def _web_budget_check(
	root: Path,
	component: WebComponent,
	component_index: int,
	language: Literal["javascript", "css"],
) -> Callable[[], CheckResult]:
	def check() -> CheckResult:
		return _web_budget_result(root, component, component_index, language=language)

	return check


def web_budget_results(root: Path, manifest: Manifest) -> tuple[CheckResult, ...]:
	results: list[CheckResult] = []
	for index, component in enumerate(manifest.web, start=1):
		for language in ("javascript", "css"):
			check_id = f"web.component_{index}.{language}_budget"
			results.append(
				observe_check(
					check_id,
					component.root,
					_web_budget_check(root, component, index, language),
				)
			)
	return tuple(results)


def _biome_unchecked_results(
	component_index: int,
	message: str,
	*,
	findings: tuple[Finding, ...] = (),
	recovery_action: str = (
		"restore the Biome policy and declared web assets, then retry the quality gate"
	),
) -> list[CheckResult]:
	return [
		CheckResult(
			check_id=f"web.component_{component_index}.biome_{check}",
			status=Status.UNCHECKED,
			summary=message,
			findings=findings,
			recovery_action=recovery_action,
		)
		for check in ("lint", "format")
	]


def _biome_inventory(policy_root: Path) -> BiomeBinary:
	"""Return the pinned Biome version, platform path, and digest."""
	try:
		raw = tomllib.loads(
			(policy_root / "quality_gate" / "policy" / BIOME_POLICY).read_text(encoding="utf-8")
		)
	except (OSError, UnicodeError, tomllib.TOMLDecodeError) as error:
		raise QualityGateError(
			"Biome inventory is missing or invalid",
			check_id="runtime.policy",
			exit_code=EXIT_UNCHECKED,
			recovery_action="restore the pinned Biome inventory and retry the quality gate",
		) from error
	biome = raw.get("biome")
	platform = "windows-x64" if os.name == "nt" else "linux-x64"
	if not isinstance(biome, dict) or not isinstance(biome.get("version"), str):
		raise QualityGateError(
			"Biome inventory has no pinned version",
			check_id="runtime.policy",
			exit_code=EXIT_UNCHECKED,
			recovery_action="restore the pinned Biome inventory and retry the quality gate",
		)
	platforms = biome.get("platforms")
	entry = platforms.get(platform) if isinstance(platforms, dict) else None
	expected_path = {
		"linux-x64": "biome-linux-x64",
		"windows-x64": "biome-win32-x64.exe",
	}[platform]
	path = entry.get("path") if isinstance(entry, dict) else None
	digest = entry.get("sha256") if isinstance(entry, dict) else None
	if (
		not isinstance(path, str)
		or not path.strip()
		or path != expected_path
		or not isinstance(digest, str)
		or not re.fullmatch(r"[0-9a-f]{64}", digest)
	):
		raise QualityGateError(
			f"Biome inventory has no usable {platform} binary pin",
			check_id="runtime.policy",
			exit_code=EXIT_UNCHECKED,
			recovery_action="restore the pinned Biome inventory and retry the quality gate",
		)
	return BiomeBinary(biome["version"], path, digest)


def _biome_command(prepared: PreparedEnvironment) -> list[str]:
	pinned = _biome_inventory(prepared.policy_root)
	release_manifest = getattr(prepared, "release_manifest", None)
	tools = getattr(release_manifest, "tools", ())
	tool = next((item for item in tools if item.name == "biome"), None)
	if tool is None or (
		tool.version != pinned.version
		or tool.path.replace("\\", "/") != pinned.path
		or tool.sha256 != pinned.sha256
	):
		raise QualityGateError(
			"selected release does not contain the pinned Biome binary",
			check_id="runtime.policy",
			exit_code=EXIT_UNCHECKED,
			recovery_action="sync the release with the exact pinned Biome inventory and retry",
		)
	config = prepared.policy_root / "quality_gate" / "policy" / BIOME_CONFIG
	if not config.is_file():
		raise QualityGateError(
			"Biome configuration is unavailable",
			check_id="runtime.policy",
			exit_code=EXIT_UNCHECKED,
			recovery_action="restore the pinned Biome configuration and retry the quality gate",
		)
	binary = prepared.policy_root / tool.path
	try:
		if not binary.is_file() or hashlib.sha256(binary.read_bytes()).hexdigest() != tool.sha256:
			raise OSError("Biome binary is missing or corrupt")
	except OSError as error:
		raise QualityGateError(
			"Biome binary is missing or corrupt",
			check_id="runtime.policy",
			exit_code=EXIT_UNCHECKED,
			recovery_action="sync the exact pinned Biome binary and retry the quality gate",
		) from error
	return [str(binary), "ci", "--config-path", str(config)]


def _biome_asset_paths(root: Path, component: WebComponent, component_index: int) -> list[str]:
	budget_results: list[CheckResult] = []
	if component.javascript:
		budget_results.append(
			_web_budget_result(root, component, component_index, language="javascript")
		)
	if component.css:
		budget_results.append(_web_budget_result(root, component, component_index, language="css"))
	if any(result.status is Status.UNCHECKED for result in budget_results):
		raise QualityGateError(
			"Biome asset inputs are unavailable",
			check_id="runtime.policy",
			exit_code=EXIT_UNCHECKED,
			recovery_action="restore readable declared web assets and retry the quality gate",
		)
	assets: set[Path] = set()
	for patterns in (component.javascript, component.css):
		matched, unsafe = _web_assets(root, component, patterns)
		if unsafe:
			raise QualityGateError(
				"Biome asset boundary contains an unsafe file",
				check_id="runtime.policy",
				exit_code=EXIT_UNCHECKED,
				recovery_action=(
					"replace linked or external assets with project-owned regular files"
				),
			)
		assets.update(matched)
	if not assets:
		raise QualityGateError(
			"declared Biome patterns match no project-owned assets",
			check_id="runtime.policy",
			exit_code=EXIT_UNCHECKED,
			recovery_action="correct the declared web patterns and retry the quality gate",
		)
	if any(asset.stat().st_size > BIOME_MAX_FILE_BYTES for asset in assets):
		raise QualityGateError(
			"declared Biome asset exceeds the policy file-size limit",
			check_id="runtime.policy",
			exit_code=EXIT_UNCHECKED,
			recovery_action=(
				"reduce the declared asset below the Biome policy file-size limit and retry"
			),
		)
	return [asset.relative_to(root).as_posix() for asset in sorted(assets)]


def _biome_component_commands(
	component_index: int,
	command: list[str],
	paths: list[str],
	timeout: int,
) -> tuple[tuple[str, list[str], int], tuple[str, list[str], int]]:
	return (
		(
			f"web.component_{component_index}.biome_lint",
			[
				*command,
				"--formatter-enabled=false",
				"--linter-enabled=true",
				"--assist-enabled=false",
				*paths,
			],
			timeout,
		),
		(
			f"web.component_{component_index}.biome_format",
			[
				*command,
				"--formatter-enabled=true",
				"--linter-enabled=false",
				"--assist-enabled=false",
				*paths,
			],
			timeout,
		),
	)


def _run_web_checks(
	actual_root: Path,
	manifest: Manifest,
	prepared: PreparedEnvironment,
	*,
	deadline: float,
) -> list[CheckResult]:
	if not manifest.web:
		return []
	commands: list[tuple[str, list[str], int]] = []
	pre_results = _OperationResults(actual_root)
	pre_results.manifest = manifest
	for component_index, component in enumerate(manifest.web, start=1):
		try:
			paths = _biome_asset_paths(actual_root, component, component_index)
			command = _biome_command(prepared)
		except (OSError, QualityGateError) as error:
			quality_error = (
				error
				if isinstance(error, QualityGateError)
				else QualityGateError(
					str(error),
					check_id="runtime.policy",
					exit_code=EXIT_UNCHECKED,
					recovery_action=(
						"restore the pinned Biome policy and web assets, "
						"then retry the quality gate"
					),
				)
			)
			for result in _biome_unchecked_results(
				component_index,
				str(quality_error),
				recovery_action=quality_error.recovery_action,
			):
				pre_results.append(
					observe_check(
						result.check_id,
						component.root,
						_fixed_check_result(result),
						process_required=True,
					)
				)
			continue
		commands.extend(
			_biome_component_commands(
				component_index, command, paths, manifest.repository.command_timeout_seconds
			)
		)
	with temporary_directory(actual_root) as temporary_path:
		executed, errors, _ = _execute_commands(
			actual_root,
			_safe_environment(temporary_path),
			commands,
			manifest.repository.command_timeout_seconds,
			deadline,
			manifest=manifest,
		)
	return [*pre_results, *_command_results(executed, errors, {})]


def _web_check_results(
	actual_root: Path,
	manifest: Manifest,
	prepared: PreparedEnvironment,
	results: list[CheckResult],
	*,
	deadline: float,
) -> list[CheckResult]:
	try:
		return _run_web_checks(actual_root, manifest, prepared, deadline=deadline)
	except (QualityGateError, DistributionError, RuntimeUnavailable, OSError) as error:
		existing_biome_ids = {result.check_id for result in results if ".biome_" in result.check_id}
		fallback: list[CheckResult] = []
		for component_index in range(1, len(manifest.web) + 1):
			for result in _biome_unchecked_results(
				component_index,
				redact(str(error)),
				recovery_action="restore the pinned Biome policy and retry the quality gate",
			):
				if result.check_id not in existing_biome_ids:
					fallback.append(
						observe_check(
							result.check_id,
							manifest.web[component_index - 1].root,
							_fixed_check_result(result),
							process_required=True,
						)
					)
					existing_biome_ids.add(result.check_id)
		return fallback


def _run_python_snapshot_checks(
	actual_root: Path,
	manifest: Manifest,
	components: list[PythonComponent],
	prepared: PreparedEnvironment,
	*,
	deadline: float,
) -> list[CheckResult]:
	policy_path = prepared.policy_root / "quality_gate" / "policy"
	if not policy_path.is_dir():
		raise QualityGateError(
			"cached policy release has no policy directory",
			check_id="runtime.policy",
			exit_code=EXIT_UNCHECKED,
			recovery_action="sync a complete policy release and retry the quality gate",
		)
	return _run_python_checks(actual_root, manifest, components, prepared, deadline=deadline)


def validate(root: Path | None = None) -> None:
	actual_root = repository_root(root)
	load_manifest(actual_root)
	emit(f"QUALITY GATE VALID (schema 2): {actual_root / MANIFEST_NAME}")


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


def _run_bounded_subprocess(
	command: list[str],
	root: Path,
	environment: dict[str, str],
	timeout: float | None,
) -> tuple[int, str]:
	"""Run a subprocess while retaining only a bounded amount of output."""
	operation = current_operation()
	if operation is not None:
		try:
			code, output, diagnostics = operation.run_process(
				command,
				root=root,
				environment=environment,
				timeout_seconds=timeout,
				limit=MAX_COMMAND_OUTPUT_BYTES,
				truncate=True,
			)
		except EditError as error:
			if str(error) == "deadline":
				raise subprocess.TimeoutExpired(command, timeout or 0) from error
			raise OutputReadError("owned process output could not be collected") from error
		return code, decode_subprocess_output(output + diagnostics)
	return _run_legacy_subprocess(command, root, environment, timeout)


def _run_legacy_subprocess(
	command: list[str],
	root: Path,
	environment: dict[str, str],
	timeout: float | None,
) -> tuple[int, str]:
	"""Preserve standalone formatting/tool callers outside an owned Gate operation."""
	process = subprocess.Popen(
		command,
		cwd=root,
		env=environment,
		stdout=subprocess.PIPE,
		stderr=subprocess.STDOUT,
	)
	retained = bytearray()
	reader_errors: list[Exception] = []
	stdout = process.stdout
	if stdout is None:
		raise OutputReadError("subprocess output pipe is unavailable")

	def drain_output() -> None:
		try:
			while True:
				chunk = stdout.read(4096)
				if not chunk:
					return
				remaining = MAX_COMMAND_OUTPUT_BYTES - len(retained)
				if remaining > 0:
					retained.extend(chunk[:remaining])
		except (OSError, ValueError) as error:
			reader_errors.append(error)

	reader = threading.Thread(target=drain_output, daemon=True)
	reader.start()
	try:
		returncode = process.wait(timeout=timeout)
	except subprocess.TimeoutExpired:
		process.kill()
		process.wait(timeout=PROCESS_CLEANUP_TIMEOUT_SECONDS)
		reader.join(timeout=PROCESS_CLEANUP_TIMEOUT_SECONDS)
		raise
	reader.join(timeout=PROCESS_CLEANUP_TIMEOUT_SECONDS)
	if reader.is_alive():
		raise subprocess.TimeoutExpired(command, PROCESS_CLEANUP_TIMEOUT_SECONDS)
	if reader_errors:
		raise OutputReadError("subprocess output could not be read") from reader_errors[0]
	return returncode, decode_subprocess_output(bytes(retained))


def run(
	command: list[str],
	root: Path,
	environment: dict[str, str],
	*,
	timeout: float | None = None,
) -> str:
	"""Run one quality command and return its decoded standard output."""
	try:
		returncode, output = _run_bounded_subprocess(command, root, environment, timeout)
	except OSError as error:
		raise QualityGateError(
			f"{' '.join(command)} could not be executed: {error}",
			check_id="runtime.command",
			exit_code=EXIT_UNCHECKED,
			recovery_action="restore the required tool or runtime and retry the quality gate",
		) from error
	except subprocess.TimeoutExpired as error:
		raise QualityGateError(
			f"{' '.join(command)} timed out after {timeout:g} seconds",
			check_id="runtime.timeout",
			exit_code=2,
			recovery_action="inspect the command and retry within the declared time budget",
		) from error
	except OutputReadError as error:
		raise QualityGateError(
			f"{' '.join(command)} output could not be read",
			check_id="runtime.command",
			exit_code=EXIT_UNCHECKED,
			recovery_action="restore subprocess output handling and retry the quality gate",
		) from error
	if returncode:
		detail = output.strip()
		detail = _bounded_output(detail)
		if not detail:
			detail = f"Command exited with {returncode}."
		lower_detail = detail.casefold()
		missing_tool = any(
			f"no module named {tool_name}" in lower_detail
			for tool_name in ("ruff", "mypy", "pytest", "biome")
		)
		pytest_collection_error = "pytest" in " ".join(command).casefold() and any(
			marker in lower_detail
			for marker in ("error collecting", "no tests collected", "no tests ran")
		)
		raise QualityGateError(
			f"{' '.join(command)}\n\n{redact(detail)}",
			check_id="runtime.command",
			exit_code=EXIT_UNCHECKED if missing_tool or pytest_collection_error else 1,
			recovery_action=(
				(
					"restore the required tool in the verification runtime "
					"and retry the quality gate"
					if missing_tool
					else "restore a collectable pytest suite and retry the quality gate"
				)
				if missing_tool or pytest_collection_error
				else "fix the reported quality finding and retry the quality gate"
			),
		)
	return _bounded_output(output).strip()


def _bounded_output(value: str) -> str:
	"""Limit external command output retained by the gate."""
	if len(value) <= MAX_COMMAND_OUTPUT_CHARS:
		return value
	marker = "\n[output truncated]\n"
	available = MAX_COMMAND_OUTPUT_CHARS - len(marker)
	prefix = available // 2
	suffix = available - prefix
	return value[:prefix] + marker + value[-suffix:]


def _pytest_outcome_summary(output: str) -> str | None:
	"""Extract bounded test totals from pytest's final terminal summary line."""
	for line in reversed(output.splitlines()):
		if not _PYTEST_SUMMARY_DURATION.search(line):
			continue
		if _PYTEST_NO_TESTS.search(line):
			return "0 tests ran"
		counts = [
			f"{match.group('count')} {match.group('outcome').lower()}"
			for match in _PYTEST_SUMMARY_COUNT.finditer(line)
		]
		if counts:
			return ", ".join(counts)
	return None


@contextmanager
def temporary_directory(root: Path) -> Iterator[str]:
	operation = current_operation()
	if operation is None:
		with tempfile.TemporaryDirectory(prefix="quality-gate-") as directory:
			yield directory
		return
	path = Path(tempfile.mkdtemp(prefix="quality-gate-"))
	try:
		operation.register_cleanup(path)
	except BaseException:
		remove_snapshot(path)
		raise
	yield str(path)


def _error_result(error: QualityGateError) -> CheckResult:
	status = Status.UNCHECKED if error.exit_code == EXIT_UNCHECKED else Status.FAILED
	return CheckResult(
		check_id=error.check_id,
		status=status,
		summary=redact(str(error)),
		findings=(Finding(message=redact(str(error))),),
		recovery_action=error.recovery_action,
	)


def _remaining(deadline: float, timeout: int) -> float:
	operation = current_operation()
	if operation is not None:
		return operation.remaining(float(timeout))
	remaining = min(float(timeout), deadline - time.monotonic())
	if remaining <= 0:
		raise QualityGateError(
			"quality gate exceeded its overall time budget",
			check_id="runtime.gate_timeout",
			exit_code=2,
			recovery_action="inspect slow checks and retry within the gate time budget",
		)
	return remaining


def _snapshot_candidate_identity(snapshot: object) -> str:
	"""Read the snapshot module's public identity without interpreting its storage."""
	identity = getattr(snapshot, "index_identity", None)
	if isinstance(identity, str):
		return identity
	return "staged Git index identity unavailable"


def _configure_operation_budget(repository: Path, operation: GateOperation) -> None:
	"""Read staged time limits without restarting the operation clock."""
	try:
		result = run_probe(
			["git", "-C", str(repository), "show", f":{MANIFEST_NAME}"],
			cwd=repository,
			timeout=operation.command_timeout_seconds,
		)
	except (OSError, subprocess.TimeoutExpired):
		return
	if result.returncode:
		return
	try:
		value = tomllib.loads(result.stdout.decode("utf-8"))
	except (UnicodeError, tomllib.TOMLDecodeError):
		return
	repository_table = value.get("repository")
	defaults = repository_table.get("defaults") if isinstance(repository_table, dict) else None
	if not isinstance(defaults, dict):
		return
	gate_timeout = defaults.get("gate_timeout_seconds", DEFAULT_GATE_TIMEOUT_SECONDS)
	command_timeout = defaults.get("command_timeout_seconds", DEFAULT_COMMAND_TIMEOUT_SECONDS)
	if isinstance(gate_timeout, bool) or not isinstance(gate_timeout, int) or gate_timeout <= 0:
		gate_timeout = DEFAULT_GATE_TIMEOUT_SECONDS
	if (
		isinstance(command_timeout, bool)
		or not isinstance(command_timeout, int)
		or command_timeout <= 0
	):
		command_timeout = DEFAULT_COMMAND_TIMEOUT_SECONDS
	operation.configure_budget(float(gate_timeout), command_timeout)


def _ci_history_refs(base: str | None, head: str | None) -> tuple[str | None, str]:
	"""Resolve pull-request commit SHAs from GitHub event data when available."""
	event_path = os.environ.get("GITHUB_EVENT_PATH")
	event_base: str | None = None
	event_head: str | None = None
	if event_path:
		try:
			event = json.loads(Path(event_path).read_text(encoding="utf-8"))
			pull_request = event.get("pull_request", {})
			base_data = pull_request.get("base", {})
			head_data = pull_request.get("head", {})
			if isinstance(base_data, dict) and isinstance(base_data.get("sha"), str):
				event_base = base_data["sha"]
			if isinstance(head_data, dict) and isinstance(head_data.get("sha"), str):
				event_head = head_data["sha"]
		except (OSError, UnicodeError, json.JSONDecodeError, AttributeError):
			_LOGGER.debug("GitHub event payload is unavailable", exc_info=True)
	return (
		base or event_base or os.environ.get("GITHUB_BASE_REF"),
		head or event_head or os.environ.get("GITHUB_SHA") or "HEAD",
	)


def _safe_environment(temporary_path: str) -> dict[str, str]:
	allowed = {
		"LANG",
		"LC_ALL",
		"PATHEXT",
		"PATH",
		"QUALITY_GATE_QUEUE_TIMEOUT_SECONDS",
		"QUALITY_GATE_RUFF_THREADS",
		"SYSTEMROOT",
		"WINDIR",
	}
	environment = {key: value for key, value in os.environ.items() if key in allowed}
	environment.update(
		{
			"HOME": temporary_path,
			"TMP": temporary_path,
			"TEMP": temporary_path,
			"PYTHONIOENCODING": "utf-8",
			"PYTHONUTF8": "1",
			"PYTHONDONTWRITEBYTECODE": "1",
			"RUFF_CACHE_DIR": temporary_path,
			"MYPY_CACHE_DIR": temporary_path,
			"COVERAGE_FILE": str(Path(temporary_path) / ".coverage"),
		}
	)
	if os.name == "nt":
		environment["USERPROFILE"] = temporary_path
	environment.update(child_admission_environment())
	return environment


def _ruff_environment(environment: dict[str, str]) -> dict[str, str]:
	raw = os.environ.get(RUFF_THREADS_ENV)
	if raw is None:
		threads = DEFAULT_RUFF_THREADS
	elif not re.fullmatch(r"[0-9]{1,2}", raw) or not 1 <= int(raw) <= MAX_RUFF_THREADS:
		raise QualityGateError(
			f"{RUFF_THREADS_ENV} must be an integer from 1 through {MAX_RUFF_THREADS}",
			check_id="runtime.ruff_threads",
			exit_code=EXIT_UNCHECKED,
			recovery_action=(
				f"set {RUFF_THREADS_ENV} to a whole number from 1 through {MAX_RUFF_THREADS}"
			),
		)
	else:
		threads = int(raw)
	return environment | {"RAYON_NUM_THREADS": str(threads)}


def _runtime_python(prepared: PreparedEnvironment, component_index: int) -> Path:
	try:
		inspection = prepared.runtimes[component_index - 1]
	except IndexError as error:
		raise QualityGateError(
			f"runtime is unavailable for Python component {component_index}",
			check_id=f"python.component_{component_index}.runtime",
			exit_code=EXIT_UNCHECKED,
			recovery_action="run setup and retry the quality gate",
		) from error
	if not inspection.current or inspection.python is None:
		raise QualityGateError(
			f"runtime is unavailable for Python component {component_index}: "
			f"{inspection.reason or 'runtime is stale'}",
			check_id=f"python.component_{component_index}.runtime",
			exit_code=EXIT_UNCHECKED,
			recovery_action="run setup and retry the quality gate",
		)
	return inspection.python


def _tool_command(prepared: PreparedEnvironment, python: Path, name: str) -> list[str]:
	release_manifest = getattr(prepared, "release_manifest", None)
	if release_manifest is not None:
		for tool in release_manifest.tools:
			if tool.name == name:
				if tool.path.lower().endswith(".whl"):
					return [str(python), "-m", name]
				return [str(prepared.policy_root / tool.path)]
	return [str(python), "-m", name]


def _has_pinned_coverage(prepared: PreparedEnvironment) -> bool:
	"""Return whether the selected policy release pins a coverage provider."""
	release_manifest = getattr(prepared, "release_manifest", None)
	return release_manifest is not None and any(
		tool.name.casefold() == "coverage" for tool in release_manifest.tools
	)


def _dependency_configuration(pyprojects: list[Path]) -> dict[str, object]:
	if len(pyprojects) > 1:
		raise QualityGateError(
			"deptry accepts at most one pyproject.toml for a Python component",
			check_id="runtime.command",
			exit_code=EXIT_UNCHECKED,
			recovery_action="declare one authoritative pyproject.toml per Python component",
		)
	if not pyprojects:
		return {}
	try:
		if pyprojects[0].stat().st_size > MAX_DEPTRY_REPORT_BYTES:
			raise QualityGateError(
				"dependency metadata exceeds the size limit",
				check_id="runtime.command",
				exit_code=EXIT_UNCHECKED,
				recovery_action="reduce the dependency metadata size and retry",
			)
		content = pyprojects[0].read_bytes()
		if len(content) > MAX_DEPTRY_REPORT_BYTES:
			raise QualityGateError(
				"dependency metadata exceeds the size limit",
				check_id="runtime.command",
				exit_code=EXIT_UNCHECKED,
				recovery_action="reduce the dependency metadata size and retry",
			)
		metadata = tomllib.loads(content.decode("utf-8"))
	except (OSError, UnicodeError, tomllib.TOMLDecodeError) as error:
		raise QualityGateError(
			"deptry configuration metadata is unavailable or invalid",
			check_id="runtime.command",
			exit_code=EXIT_UNCHECKED,
			recovery_action="restore valid dependency metadata and retry",
		) from error
	tool = metadata.get("tool", {})
	candidate = tool.get("deptry", {}) if isinstance(tool, dict) else {}
	configuration = candidate if isinstance(candidate, dict) else {}
	for key in sorted(_DEPTRY_SUPPRESSION_KEYS.intersection(configuration)):
		raise QualityGateError(
			f"tool.deptry.{key} bypasses the typed waiver contract",
			check_id="runtime.command",
			exit_code=EXIT_UNCHECKED,
			recovery_action=f"remove tool.deptry.{key} and use one exact current manifest waiver",
		)
	return configuration


def _dependency_requirement_groups(
	actual_root: Path,
	component: PythonComponent,
	pyprojects: list[Path],
	configuration: dict[str, object],
) -> tuple[list[Path], list[Path]]:
	runtime_names = configuration.get("requirements_files", ["requirements.txt"])
	development_names = configuration.get(
		"requirements_files_dev", ["dev-requirements.txt", "requirements-dev.txt"]
	)
	if not isinstance(runtime_names, list) or not all(
		isinstance(value, str) for value in runtime_names
	):
		raise QualityGateError(
			"tool.deptry.requirements_files must classify dependency input paths",
			check_id="runtime.command",
			exit_code=EXIT_UNCHECKED,
			recovery_action="declare valid runtime requirement paths in tool.deptry",
		)
	if not isinstance(development_names, list) or not all(
		isinstance(value, str) for value in development_names
	):
		raise QualityGateError(
			"tool.deptry.requirements_files_dev must classify dependency input paths",
			check_id="runtime.command",
			exit_code=EXIT_UNCHECKED,
			recovery_action="declare valid development requirement paths in tool.deptry",
		)
	runtime_set = {value.replace("\\", "/") for value in runtime_names}
	development_set = {value.replace("\\", "/") for value in development_names}
	runtime_requirements: list[Path] = []
	development_requirements: list[Path] = []
	for path in component.dependency_inputs:
		if path in pyprojects:
			continue
		relative = str(path.relative_to(actual_root)).replace("\\", "/")
		if relative in development_set or path.name in development_set:
			development_requirements.append(path)
		elif relative in runtime_set or path.name in runtime_set:
			runtime_requirements.append(path)
		else:
			raise QualityGateError(
				f"dependency input is not classified as runtime or development: {relative}",
				check_id="runtime.command",
				exit_code=EXIT_UNCHECKED,
				recovery_action=(
					"classify every requirements input in tool.deptry requirements_files or "
					"requirements_files_dev"
				),
			)
	return runtime_requirements, development_requirements


def _dependency_command(
	actual_root: Path,
	component: PythonComponent,
	prepared: PreparedEnvironment,
	python_executable: Path,
	report_path: Path,
) -> list[str]:
	pyprojects = [path for path in component.dependency_inputs if path.name == "pyproject.toml"]
	configuration = _dependency_configuration(pyprojects)
	runtime_requirements, development_requirements = _dependency_requirement_groups(
		actual_root, component, pyprojects, configuration
	)
	if pyprojects and (runtime_requirements or development_requirements):
		raise QualityGateError(
			"deptry cannot analyze mixed pyproject.toml and requirements dependency inputs",
			check_id="runtime.command",
			exit_code=EXIT_UNCHECKED,
			recovery_action=(
				"declare one authoritative dependency metadata format per Python component"
			),
		)
	command = [
		*_tool_command(prepared, python_executable, "deptry"),
		str(component.path.relative_to(actual_root)),
		"--json-output",
		str(report_path),
		"--no-ansi",
		"--ignore-notebooks",
		"--exclude",
		r"(?:.*/)?venv(?:/|$)",
		"--exclude",
		r"(?:.*/)?\.venv(?:/|$)",
		"--exclude",
		r"(?:.*/)?\.direnv(?:/|$)",
		"--exclude",
		r"(?:.*/)?tests(?:/|$)",
		"--exclude",
		r"(?:.*/)?\.git(?:/|$)",
		"--exclude",
		r"(?:.*/)?setup\.py$",
	]
	if pyprojects:
		command.extend(("--config", str(pyprojects[0].relative_to(actual_root))))
	else:
		controlled_config = report_path.with_name("deptry-config.toml")
		controlled_config.write_text("", encoding="utf-8")
		command.extend(("--config", str(controlled_config)))
	for test_path in component.test_paths:
		relative_test = str(test_path.relative_to(actual_root)).replace("\\", "/")
		command.extend(("--exclude", rf"{re.escape(relative_test)}(?:/|$)"))
	if runtime_requirements:
		command.extend(
			(
				"--requirements-files",
				",".join(str(path.relative_to(actual_root)) for path in runtime_requirements),
			)
		)
	if development_requirements:
		command.extend(
			(
				"--requirements-files-dev",
				",".join(str(path.relative_to(actual_root)) for path in development_requirements),
			)
		)
	return command


def _dependency_issue(value: object) -> tuple[Finding, str]:
	if not isinstance(value, dict):
		raise ValueError("deptry issue is not an object")
	error = value.get("error")
	location = value.get("location")
	module = value.get("module")
	if not isinstance(error, dict) or not isinstance(location, dict) or not isinstance(module, str):
		raise ValueError("deptry issue fields are invalid")
	code = error.get("code")
	message = error.get("message")
	path = location.get("file")
	line = location.get("line")
	if (
		code not in {"DEP001", "DEP002", "DEP003", "DEP004"}
		or not isinstance(message, str)
		or not isinstance(path, str)
		or not _DEPTRY_MODULE.fullmatch(module)
		or len(message) > MAX_DEPTRY_TEXT_CHARS
		or not message.isprintable()
		or (line is not None and (not isinstance(line, int) or line < 1))
	):
		raise ValueError("deptry issue contains an unsupported value")
	normalized_path = path.replace("\\", "/")
	parsed_path = PurePosixPath(normalized_path)
	if (
		len(normalized_path) > MAX_DEPTRY_TEXT_CHARS
		or not normalized_path
		or parsed_path.is_absolute()
		or normalized_path == "."
		or ".." in parsed_path.parts
		or not normalized_path.isprintable()
		or ":" in parsed_path.parts[0]
	):
		raise ValueError("deptry issue path is not repository-relative")
	target = f"{normalized_path}::{code}::{module}"
	return (
		Finding(
			path=normalized_path,
			line=line,
			message=f"{code} {message}",
			action=f"fix the dependency declaration or add a reviewed waiver for {target}",
		),
		target,
	)


def _dependency_inline_suppressions(
	actual_root: Path, component: PythonComponent
) -> tuple[Finding, ...]:
	findings = []
	for path in sorted(component.path.rglob("*.py")):
		if any(
			path == test_path or test_path in path.parents for test_path in component.test_paths
		):
			continue
		try:
			if path.stat().st_size > MAX_DEPTRY_SOURCE_BYTES:
				raise QualityGateError(
					"Python source exceeds the dependency-analysis size limit",
					check_id="runtime.command",
					exit_code=EXIT_UNCHECKED,
					recovery_action="split the oversized Python source and retry",
				)
			content = path.read_bytes()
		except OSError as error:
			raise QualityGateError(
				"Python source is unreadable during dependency suppression validation",
				check_id="runtime.command",
				exit_code=EXIT_UNCHECKED,
				recovery_action="restore readable Python source and retry",
			) from error
		if len(content) > MAX_DEPTRY_SOURCE_BYTES:
			raise QualityGateError(
				"Python source exceeds the dependency-analysis size limit",
				check_id="runtime.command",
				exit_code=EXIT_UNCHECKED,
				recovery_action="split the oversized Python source and retry",
			)
		try:
			comments = tuple(
				token
				for token in tokenize.tokenize(io.BytesIO(content).readline)
				if token.type == tokenize.COMMENT
			)
		except (SyntaxError, tokenize.TokenError) as error:
			raise QualityGateError(
				"Python source cannot be tokenized for dependency suppression validation",
				check_id="runtime.command",
				exit_code=EXIT_UNCHECKED,
				recovery_action="restore parseable Python source and retry",
			) from error
		for comment in comments:
			if not _DEPTRY_INLINE_IGNORE.search(comment.string):
				continue
			findings.append(
				Finding(
					path=str(path.relative_to(actual_root)).replace("\\", "/"),
					line=comment.start[0],
					message="inline deptry suppression bypasses the typed waiver contract",
					action="remove the inline suppression and use an exact manifest waiver",
				)
			)
	return tuple(findings)


def _read_dependency_report(report_path: Path) -> list[object]:
	try:
		if report_path.stat().st_size > MAX_DEPTRY_REPORT_BYTES:
			raise ValueError("deptry report exceeds the size limit")
		content = report_path.read_bytes()
	except OSError as error:
		raise ValueError("deptry report is unavailable") from error
	if len(content) > MAX_DEPTRY_REPORT_BYTES:
		raise ValueError("deptry report exceeds the size limit")
	try:
		value = json.loads(content.decode("utf-8"))
	except (UnicodeError, json.JSONDecodeError) as error:
		raise ValueError("deptry report is not valid UTF-8 JSON") from error
	if not isinstance(value, list):
		raise ValueError("deptry report is not a list")
	return value


def _dependency_hygiene_result(
	actual_root: Path,
	manifest: Manifest,
	component: PythonComponent,
	prepared: PreparedEnvironment,
	component_index: int,
	python_executable: Path | None,
	report_path: Path,
	deadline: float,
) -> CheckResult:
	check_id = f"python.component_{component_index}.deptry"
	component_scope = component.path.relative_to(actual_root).as_posix()
	dependency_scope = ",".join(
		path.relative_to(actual_root).as_posix() for path in component.dependency_inputs
	)
	return observe_check(
		check_id,
		f"{component_scope}; dependencies={dependency_scope}",
		lambda: _dependency_hygiene_result_impl(
			actual_root,
			manifest,
			component,
			prepared,
			component_index,
			python_executable,
			report_path,
			deadline,
		),
		process_required=True,
	)


def _dependency_hygiene_result_impl(
	actual_root: Path,
	manifest: Manifest,
	component: PythonComponent,
	prepared: PreparedEnvironment,
	component_index: int,
	python_executable: Path | None,
	report_path: Path,
	deadline: float,
) -> CheckResult:
	check_id = f"python.component_{component_index}.deptry"
	if not component.dependency_inputs:
		return CheckResult(
			check_id,
			Status.NOT_APPLICABLE,
			"dependency hygiene is not applicable without dependency inputs",
			recovery_action="declare dependency inputs when the component has dependencies",
		)
	missing_inputs = [path for path in component.dependency_inputs if not path.is_file()]
	if python_executable is None or missing_inputs:
		reason = (
			"the component runtime is unavailable"
			if python_executable is None
			else "dependency metadata is unavailable"
		)
		return CheckResult(
			check_id,
			Status.UNCHECKED,
			reason,
			findings=tuple(
				Finding(
					path=str(path.relative_to(actual_root)).replace("\\", "/"),
					message="declared dependency input does not exist",
					action="restore the declared dependency input",
				)
				for path in missing_inputs
			),
			recovery_action="restore the component runtime and dependency metadata, then retry",
		)
	try:
		inline_suppressions = _dependency_inline_suppressions(actual_root, component)
		if inline_suppressions:
			return CheckResult(
				check_id,
				Status.FAILED,
				"inline deptry suppressions bypass the typed waiver contract",
				findings=inline_suppressions,
				recovery_action="remove every inline deptry suppression and retry",
			)
		command = _dependency_command(
			actual_root, component, prepared, python_executable, report_path
		)
		returncode, output = _run_bounded_subprocess(
			command,
			actual_root,
			_safe_environment(str(report_path.parent)),
			_remaining(deadline, manifest.repository.command_timeout_seconds),
		)
	except (OSError, subprocess.TimeoutExpired, OutputReadError, QualityGateError) as error:
		return CheckResult(
			check_id,
			Status.UNCHECKED,
			"dependency analysis could not run",
			findings=(Finding(message=redact(str(error))),),
			recovery_action="restore deptry and usable dependency metadata, then retry",
		)
	try:
		raw_issues = _read_dependency_report(report_path)
		issues = tuple(_dependency_issue(value) for value in raw_issues)
	except ValueError as error:
		return CheckResult(
			check_id,
			Status.UNCHECKED,
			"dependency analysis report is unavailable or invalid",
			findings=(Finding(message=redact(output.strip() or str(error))),),
			recovery_action="restore deptry and usable dependency metadata, then retry",
		)
	if returncode not in {0, 1} or (returncode == 0 and issues) or (returncode == 1 and not issues):
		return CheckResult(
			check_id,
			Status.UNCHECKED,
			"dependency analysis returned an unusable result",
			findings=(
				Finding(message=redact(output.strip() or f"deptry exited with {returncode}")),
			),
			recovery_action="restore deptry and usable dependency metadata, then retry",
		)
	if not issues:
		return CheckResult(check_id, Status.PASSED, "dependency declarations match imports")
	unwaived = tuple(
		finding for finding, target in issues if manifest.resolve_waiver(check_id, target) is None
	)
	if not unwaived:
		return CheckResult(
			check_id,
			Status.WAIVED,
			"dependency findings have exact current waivers",
			findings=tuple(finding for finding, _target in issues),
			waiver_target=issues[0][1],
		)
	return CheckResult(
		check_id,
		Status.FAILED,
		"dependency declarations do not match imports",
		findings=unwaived,
		recovery_action="fix each dependency finding or add one exact current human waiver",
	)


def _component_commands(
	actual_root: Path,
	manifest: Manifest,
	component: PythonComponent,
	prepared: PreparedEnvironment,
	component_index: int,
	python_executable: Path,
) -> list[tuple[str, list[str], int]]:
	prefix = f"python.component_{component_index}"
	arguments = [str(component.path.relative_to(actual_root))]
	commands: list[tuple[str, list[str], int]] = [
		(
			f"{prefix}.ruff",
			[
				*_tool_command(prepared, python_executable, "ruff"),
				"check",
				"--config",
				str(prepared.policy_root / "quality_gate" / "policy" / "ruff.toml"),
				*arguments,
			],
			manifest.repository.command_timeout_seconds,
		),
		(
			f"{prefix}.format",
			[
				*_tool_command(prepared, python_executable, "ruff"),
				"format",
				"--check",
				"--config",
				str(prepared.policy_root / "quality_gate" / "policy" / "ruff.toml"),
				*arguments,
			],
			manifest.repository.command_timeout_seconds,
		),
	]
	if component.typecheck:
		commands.append(
			(
				f"{prefix}.mypy",
				[
					*_tool_command(prepared, python_executable, "mypy"),
					"--config-file",
					str(prepared.policy_root / "quality_gate" / "policy" / "mypy.ini"),
					*arguments,
				],
				manifest.repository.command_timeout_seconds,
			)
		)
	if component.test_paths:
		test_arguments = [
			*(str(test_path.relative_to(actual_root)) for test_path in component.test_paths),
			"-q",
			"-p",
			"no:cacheprovider",
		]
		commands.append(
			(
				f"{prefix}.pytest",
				[
					*_tool_command(prepared, python_executable, "pytest"),
					*test_arguments,
				],
				manifest.repository.test_timeout_seconds,
			)
		)
		if _has_pinned_coverage(prepared):
			commands.extend(
				(
					(
						f"{prefix}.coverage",
						[
							*_tool_command(prepared, python_executable, "coverage"),
							"run",
							"-m",
							"pytest",
							*test_arguments,
						],
						manifest.repository.test_timeout_seconds,
					),
					(
						f"{prefix}.coverage_report",
						[
							*_tool_command(prepared, python_executable, "coverage"),
							"report",
							"--show-missing",
						],
						manifest.repository.command_timeout_seconds,
					),
				)
			)
	return commands


def _native_tool_configuration(
	check_id: str,
) -> tuple[Literal["ruff", "mypy"], str, str] | None:
	"""Map one native check to its tool, policy file, and cache environment key."""
	if check_id.endswith((".ruff", ".format")):
		return "ruff", "ruff.toml", "RUFF_CACHE_DIR"
	if check_id.endswith(".mypy"):
		return "mypy", "mypy.ini", "MYPY_CACHE_DIR"
	return None


def _native_cache_identity(
	actual_root: Path,
	check_id: str,
	command: list[str],
	prepared: PreparedEnvironment,
	component: PythonComponent,
	component_index: int,
) -> NativeToolCacheIdentity | None:
	"""Build a snapshot-independent key for one exact pinned native tool command."""
	tool_configuration = _native_tool_configuration(check_id)
	if tool_configuration is None:
		return None
	tool_name, configuration_name, _environment_key = tool_configuration

	release_manifest = getattr(prepared, "release_manifest", None)
	tools = getattr(release_manifest, "tools", ())
	tool = next(
		(item for item in tools if getattr(item, "name", "").casefold() == tool_name),
		None,
	)
	runtime_identities = getattr(prepared, "runtime_identities", ())
	if tool is None or len(runtime_identities) < component_index:
		return None
	runtime_identity_value = runtime_identities[component_index - 1]
	if not isinstance(runtime_identity_value, RuntimeIdentity):
		return None
	policy_root = getattr(prepared, "policy_root", None)
	cache_root = getattr(prepared, "cache_root", None)
	if not isinstance(policy_root, Path) or not isinstance(cache_root, Path):
		return None
	configuration = policy_root / "quality_gate" / "policy" / configuration_name
	try:
		policy_digest = hashlib.sha256(configuration.read_bytes()).hexdigest()
		import_root = component.path.relative_to(actual_root).as_posix()
	except OSError:
		return None
	except ValueError:
		return None

	repository_identity = _native_cache_repository_identity(runtime_identity_value.repository)
	cache_runtime_identity = replace(
		runtime_identity_value,
		repository=repository_identity,
	)
	normalized_options = _normalized_native_tool_options(
		command, tool_name, configuration_name, policy_digest
	)

	return NativeToolCacheIdentity(
		repository=repository_identity,
		engine_version=__version__,
		runtime_fingerprint=runtime_fingerprint(cache_runtime_identity),
		tool=tool_name,
		tool_version=tool.version,
		tool_digest=tool.sha256,
		policy_release=prepared.release_manifest.version,
		policy_digest=policy_digest,
		platform=f"{sys.platform}-{platform.machine().casefold()}",
		python_version=runtime_identity_value.python_version,
		effective_options=tuple(normalized_options),
		import_roots=(import_root,),
	)


def _native_cache_repository_identity(repository: str) -> str:
	"""Use a Git common directory to identify linked worktrees as one source repo."""
	repository_root = Path(repository)
	git_path = repository_root / ".git"
	try:
		if git_path.is_dir():
			return str(git_path.resolve())
		if not git_path.is_file():
			return str(repository_root.resolve())

		git_pointer = git_path.read_text(encoding="utf-8").strip()
		git_dir_text = git_pointer.partition(":")
		if git_dir_text[0].casefold() != "gitdir":
			return str(repository_root.resolve())
		git_dir = Path(git_dir_text[2].strip())
		if not git_dir.is_absolute():
			git_dir = repository_root / git_dir
		git_dir = git_dir.resolve()
		common_dir_file = git_dir / "commondir"
		if not common_dir_file.is_file():
			return str(git_dir)
		common_dir = Path(common_dir_file.read_text(encoding="utf-8").strip())
		if not common_dir.is_absolute():
			common_dir = git_dir / common_dir
		return str(common_dir.resolve())
	except (OSError, RuntimeError, UnicodeError, ValueError):
		return str(repository_root.resolve())


_PYTHON_MODULE_COMMAND_LENGTH = 3


def _normalized_native_tool_options(
	command: list[str],
	tool_name: Literal["ruff", "mypy"],
	configuration_name: str,
	policy_digest: str,
) -> list[str]:
	arguments = command[1:]
	if (
		len(command) >= _PYTHON_MODULE_COMMAND_LENGTH
		and command[1] == "-m"
		and command[2] == tool_name
	):
		arguments = command[_PYTHON_MODULE_COMMAND_LENGTH:]
	normalized_options: list[str] = []
	configuration_next = False
	for argument in arguments:
		if configuration_next:
			normalized_options.append(f"<policy:{configuration_name}:{policy_digest}>")
			configuration_next = False
		elif argument in {"--config", "--config-file"}:
			normalized_options.append(argument)
			configuration_next = True
		else:
			normalized_options.append(argument.replace("\\", "/"))
	return normalized_options


def _command_error(
	check_id: str, error: NativeToolCacheError | QualityGateError
) -> QualityGateError:
	if isinstance(error, NativeToolCacheError):
		return QualityGateError(
			f"native tool source workspace is unavailable: {error.reason}",
			check_id=check_id,
			exit_code=EXIT_UNCHECKED,
			recovery_action="restore private tool execution and retry the quality gate",
		)
	error.check_id = check_id
	return error


def _record_command_result(
	check_id: str, run_errors: list[QualityGateError], run_outputs: dict[str, str]
) -> None:
	operation = current_operation()
	if operation is not None:
		operation.record_result(
			_command_results(
				[check_id],
				[error for error in run_errors if error.check_id == check_id],
				run_outputs,
			)[0]
		)


def _execute_commands(
	actual_root: Path,
	environment: dict[str, str],
	commands: list[tuple[str, list[str], int]],
	component_timeout: int,
	deadline: float,
	*,
	prepared: PreparedEnvironment | None = None,
	component: PythonComponent | None = None,
	component_index: int | None = None,
	manifest: Manifest | None = None,
) -> tuple[list[str], list[QualityGateError], dict[str, str]]:
	executed: list[str] = []
	run_errors: list[QualityGateError] = []
	run_outputs: dict[str, str] = {}
	operation = current_operation()
	scope_resolver = _OperationResults(actual_root)
	scope_resolver.manifest = manifest
	for check_id, command, timeout in commands:
		executed.append(check_id)
		stage = (
			nullcontext()
			if operation is None
			else operation.stage(
				check_id,
				scope_resolver.scope_for(check_id),
				check=True,
				process_required=True,
			)
		)
		try:
			with stage:
				try:
					tool_configuration = _native_tool_configuration(check_id)
					tool_environment_key = (
						tool_configuration[2] if tool_configuration is not None else None
					)
					identity = (
						_native_cache_identity(
							actual_root,
							check_id,
							command,
							prepared,
							component,
							component_index,
						)
						if prepared is not None
						and component is not None
						and component_index is not None
						and tool_environment_key is not None
						else None
					)
					selection_context: (
						AbstractContextManager[NativeToolCacheSelection | None] | None
					) = None
					if (
						identity is not None
						and prepared is not None
						and tool_environment_key is not None
					):
						private_path = Path(environment[tool_environment_key])
						selection_context = NativeToolCache(prepared.cache_root).lease(
							identity, private_path, operation=operation
						)
					if selection_context is None:
						selection_context = nullcontext(None)
					with selection_context as selection:
						private_path = (
							Path(environment[tool_environment_key])
							if tool_environment_key is not None
							else actual_root
						)
						source_workspace = (
							native_tool_source_workspace(
								selection,
								actual_root,
								private_path,
								operation=operation,
							)
							if selection is not None
							else nullcontext((None, actual_root))
						)
						with source_workspace as (effective_selection, command_root):
							if effective_selection is not None and operation is not None:
								operation.record_native_cache_selection(effective_selection)
							command_base_environment = dict(environment)
							if effective_selection is not None and tool_environment_key is not None:
								command_base_environment[tool_environment_key] = str(
									effective_selection.path
								)
							command_environment = (
								_ruff_environment(command_base_environment)
								if tool_configuration is not None
								and tool_configuration[0] == "ruff"
								else command_base_environment
							)
							output = run(
								command,
								command_root,
								command_environment,
								timeout=_remaining(deadline, min(timeout, component_timeout)),
							)
					if output and check_id.endswith((".coverage_report", ".pytest")):
						run_outputs[check_id] = output
				except (NativeToolCacheError, QualityGateError) as error:
					run_errors.append(_command_error(check_id, error))
		except OperationTimeoutError as error:
			if not any(
				item.check_id == check_id and item.exit_code != EXIT_UNCHECKED
				for item in run_errors
			):
				run_errors.append(
					QualityGateError(
						str(error),
						check_id=check_id,
						exit_code=EXIT_UNCHECKED,
						recovery_action=(
							"inspect the current and last completed stages; "
							"restore the budget and retry"
						),
					)
				)
			_record_command_result(check_id, run_errors, run_outputs)
			raise
		_record_command_result(check_id, run_errors, run_outputs)
	return executed, run_errors, run_outputs


def _command_results(
	executed: list[str],
	run_errors: list[QualityGateError],
	run_outputs: dict[str, str],
) -> list[CheckResult]:
	results: list[CheckResult] = []
	for check_id in executed:
		errors = [error for error in run_errors if error.check_id == check_id]
		is_coverage_collection = check_id.endswith(".coverage")
		is_coverage_report = check_id.endswith(".coverage_report")
		is_coverage = is_coverage_collection or is_coverage_report
		status = Status.PASSED
		if errors and not is_coverage:
			status = (
				Status.UNCHECKED
				if any(error.exit_code == EXIT_UNCHECKED for error in errors)
				else Status.FAILED
			)
		if is_coverage:
			status = Status.PASSED
		command_output = run_outputs.get(check_id, "")
		summary = "check passed" if status is Status.PASSED else "check requires attention"
		recovery_action = "restore verification and fix the reported finding, then retry"
		if check_id.endswith(".pytest"):
			pytest_output = "\n".join((command_output, *(str(error) for error in errors)))
			pytest_totals = _pytest_outcome_summary(pytest_output)
			if pytest_totals is not None:
				summary = f"pytest totals: {pytest_totals}"
		if is_coverage_collection:
			summary = "coverage collection completed"
		if is_coverage_report:
			report_lines = [line.strip() for line in command_output.splitlines() if line.strip()]
			report_line = next(
				(line for line in reversed(report_lines) if line.startswith("TOTAL")),
				report_lines[-1] if report_lines else "coverage report generated",
			)
			summary = f"coverage report: {redact(report_line)}"
			recovery_action = "coverage is report-only and does not affect the quality verdict"
		if errors and is_coverage:
			summary = "coverage report unavailable; report-only: " + redact(str(errors[0]))
		results.append(
			CheckResult(
				check_id=check_id,
				status=status,
				summary=summary,
				findings=()
				if is_coverage
				else tuple(Finding(message=redact(str(error))) for error in errors),
				recovery_action=None if is_coverage else recovery_action if errors else None,
			)
		)
	return results


def _mark_component_checks_not_run(
	results: _OperationResults,
	component: PythonComponent,
	component_index: int,
	prepared: PreparedEnvironment,
	reason: str,
) -> None:
	prefix = f"python.component_{component_index}"
	component_scope = component.path.relative_to(results.root).as_posix()
	for suffix in ("ruff", "format"):
		results.append_not_run(f"{prefix}.{suffix}", component_scope, reason)
	if component.typecheck:
		results.append_not_run(f"{prefix}.mypy", component_scope, reason)
	if component.tests is not None:
		test_paths = tuple(
			path.relative_to(results.root).as_posix()
			for path in (*component.test_paths, *component.missing_test_paths)
		)
		test_scope = f"{component_scope}; tests={','.join(test_paths)}"
		results.append_not_run(f"{prefix}.pytest", test_scope, reason)
		if _has_pinned_coverage(prepared):
			results.append_not_run(f"{prefix}.coverage", test_scope, reason)
			results.append_not_run(f"{prefix}.coverage_report", test_scope, reason)


@dataclass(frozen=True, slots=True)
class _PythonCheckContext:
	"""Inputs shared by every outcome of one candidate component's checks."""

	root: Path
	manifest: Manifest
	component: PythonComponent
	prepared: PreparedEnvironment
	component_index: int
	report_path: Path
	deadline: float

	def dependency_result(self, python_executable: Path | None = None) -> CheckResult:
		return _dependency_hygiene_result(
			self.root,
			self.manifest,
			self.component,
			self.prepared,
			self.component_index,
			python_executable,
			self.report_path,
			self.deadline,
		)


def _append_missing_component_check(
	results: _OperationResults, context: _PythonCheckContext
) -> None:
	check_id = f"python.component_{context.component_index}.path"
	component_scope = context.component.path.relative_to(context.root).as_posix()
	missing_component = CheckResult(
		check_id=check_id,
		status=Status.UNCHECKED,
		summary="Python component path is unavailable",
		findings=(
			Finding(
				path=component_scope,
				message="declared component path does not exist",
				action="restore the declared component path",
			),
		),
		recovery_action="restore the declared component path and retry the quality gate",
	)
	results.append(
		observe_check(
			check_id,
			component_scope,
			_fixed_check_result(missing_component),
		)
	)
	results.append(context.dependency_result())
	_mark_component_checks_not_run(
		results,
		context.component,
		context.component_index,
		context.prepared,
		"declared component path is unavailable",
	)


def _append_missing_test_path_checks(
	results: _OperationResults, root: Path, component: PythonComponent, component_index: int
) -> None:
	for missing_index, missing_path in enumerate(component.missing_test_paths, start=1):
		check_id = f"python.component_{component_index}.test_path_{missing_index}"
		missing_test_path = CheckResult(
			check_id=check_id,
			status=Status.UNCHECKED,
			summary="declared test path is unavailable",
			findings=(
				Finding(
					path=missing_path.relative_to(root).as_posix(),
					message="declared test path does not exist",
					action="restore the declared test path",
				),
			),
			recovery_action="restore the declared test path and retry the quality gate",
		)
		results.append(
			observe_check(
				check_id,
				missing_path.relative_to(root).as_posix(),
				_fixed_check_result(missing_test_path),
			)
		)


def _append_unavailable_runtime_checks(
	results: _OperationResults,
	context: _PythonCheckContext,
	error: QualityGateError,
) -> None:
	results.append(_error_result(error))
	results.append(context.dependency_result())
	_mark_component_checks_not_run(
		results,
		context.component,
		context.component_index,
		context.prepared,
		"component runtime is unavailable",
	)


def _run_ready_python_component_checks(
	results: _OperationResults,
	context: _PythonCheckContext,
	python_executable: Path,
	environment: dict[str, str],
) -> tuple[list[str], list[QualityGateError], dict[str, str]]:
	results.append(context.dependency_result(python_executable))
	component = context.component
	prefix = f"python.component_{context.component_index}"
	if component.tests is not None and not component.test_paths:
		test_paths = tuple(
			path.relative_to(context.root).as_posix()
			for path in (*component.test_paths, *component.missing_test_paths)
		)
		component_scope = component.path.relative_to(context.root).as_posix()
		test_scope = f"{component_scope}; tests={','.join(test_paths)}"
		reason = "no declared test path is available"
		results.append_not_run(f"{prefix}.pytest", test_scope, reason)
		if _has_pinned_coverage(context.prepared):
			results.append_not_run(f"{prefix}.coverage", test_scope, reason)
			results.append_not_run(f"{prefix}.coverage_report", test_scope, reason)
	return _execute_commands(
		context.root,
		environment,
		_component_commands(
			context.root,
			context.manifest,
			component,
			context.prepared,
			context.component_index,
			python_executable,
		),
		component.timeout_seconds,
		context.deadline,
		prepared=context.prepared,
		component=component,
		component_index=context.component_index,
		manifest=context.manifest,
	)


def _run_python_component_checks(
	results: _OperationResults,
	context: _PythonCheckContext,
	environment: dict[str, str],
) -> tuple[list[str], list[QualityGateError], dict[str, str]]:
	if not context.component.path_exists:
		_append_missing_component_check(results, context)
		return [], [], {}
	_append_missing_test_path_checks(
		results, context.root, context.component, context.component_index
	)
	try:
		python_executable = _runtime_python(context.prepared, context.component_index)
	except QualityGateError as error:
		_append_unavailable_runtime_checks(results, context, error)
		return [], [], {}
	return _run_ready_python_component_checks(results, context, python_executable, environment)


def _append_not_applicable_python_checks(
	results: _OperationResults,
	components: list[PythonComponent],
	prepared: PreparedEnvironment,
) -> None:
	for component_index, component in enumerate(components, start=1):
		tests_not_applicable = component.tests is None
		has_coverage = _has_pinned_coverage(prepared)
		if not component.typecheck:
			results.append(
				CheckResult(
					check_id=f"python.component_{component_index}.mypy",
					status=Status.NOT_APPLICABLE,
					summary="typing is explicitly disabled by the component contract",
					recovery_action="enable typecheck when component typing is required",
				)
			)
		if tests_not_applicable:
			results.append(
				CheckResult(
					check_id=f"python.component_{component_index}.pytest",
					status=Status.NOT_APPLICABLE,
					summary="tests are explicitly not applicable",
					recovery_action="declare test paths if tests apply",
				)
			)
		if tests_not_applicable or not has_coverage:
			summary = (
				"coverage is not applicable without tests"
				if tests_not_applicable and has_coverage
				else "coverage provider is not in the pinned policy inventory"
			)
			recovery_action = (
				"declare test paths before collecting report-only coverage"
				if tests_not_applicable and has_coverage
				else "add a pinned coverage provider only when report-only coverage is required"
			)
			results.append(
				CheckResult(
					check_id=f"python.component_{component_index}.coverage",
					status=Status.NOT_APPLICABLE,
					summary=summary,
					recovery_action=recovery_action,
				)
			)


def _run_python_checks(
	actual_root: Path,
	manifest: Manifest,
	components: list[PythonComponent],
	prepared: PreparedEnvironment,
	*,
	deadline: float,
) -> list[CheckResult]:
	results = _OperationResults(actual_root)
	results.manifest = manifest
	with temporary_directory(actual_root) as temporary_path:
		environment = _safe_environment(temporary_path)
		environment["QUALITY_GATE_POLICY_ROOT"] = str(prepared.policy_root)
		temporary_root = Path(temporary_path)
		run_errors: list[QualityGateError] = []
		executed: list[str] = []
		run_outputs: dict[str, str] = {}
		for component_index, component in enumerate(components, start=1):
			context = _PythonCheckContext(
				actual_root,
				manifest,
				component,
				prepared,
				component_index,
				temporary_root / f"deptry-{component_index}.json",
				deadline,
			)
			component_executed, component_errors, component_outputs = _run_python_component_checks(
				results,
				context,
				environment,
			)
			executed.extend(component_executed)
			run_errors.extend(component_errors)
			run_outputs.update(component_outputs)
	results.extend(_command_results(executed, run_errors, run_outputs))
	_append_not_applicable_python_checks(results, components, prepared)
	return results


def format_paths(root: Path | None, paths: tuple[str, ...]) -> None:
	"""Format only the explicit Python paths with the pinned Ruff release."""
	actual_root = repository_root(root)
	try:
		manifest = load_manifest(actual_root)
		components = load_components(actual_root)
	except ValidationError as error:
		raise _manifest_error(error) from error
	if not components:
		raise QualityGateError(
			"no Python component is declared",
			check_id="python.components",
			exit_code=EXIT_UNCHECKED,
			recovery_action="declare a Python component before formatting Python files",
		)
	prepared = prepare(actual_root)
	deadline = time.monotonic() + manifest.repository.gate_timeout_seconds
	selected: dict[int, list[str]] = {}
	for raw_path in paths:
		candidate = relative_path(actual_root, raw_path, "format path")
		if not candidate.exists():
			raise QualityGateError(
				f"format path does not exist: {raw_path}",
				check_id="python.format",
				exit_code=EXIT_UNCHECKED,
				recovery_action=f"restore {raw_path} and retry format",
			)
		for index, component in enumerate(components):
			if candidate == component.path or component.path in candidate.parents:
				selected.setdefault(index, []).append(str(candidate.relative_to(actual_root)))
				break
		else:
			raise QualityGateError(
				f"format path is outside every Python component: {raw_path}",
				check_id="python.format",
				exit_code=EXIT_UNCHECKED,
				recovery_action="format only paths declared by a Python component",
			)
	with temporary_directory(actual_root) as temporary_path:
		environment = _safe_environment(temporary_path)
		format_errors: list[QualityGateError] = []
		for index, explicit_paths in selected.items():
			python_executable = _runtime_python(prepared, index + 1)
			try:
				run(
					[
						*_tool_command(prepared, python_executable, "ruff"),
						"format",
						"--config",
						str(prepared.policy_root / "quality_gate" / "policy" / "ruff.toml"),
						*explicit_paths,
					],
					actual_root,
					environment,
					timeout=_remaining(deadline, manifest.repository.command_timeout_seconds),
				)
			except QualityGateError as error:
				format_errors.append(error)
		if format_errors:
			raise QualityGateError(
				"\n".join(str(error) for error in format_errors),
				check_id="python.format",
				exit_code=(
					EXIT_UNCHECKED
					if any(error.exit_code == EXIT_UNCHECKED for error in format_errors)
					else 1
				),
				recovery_action="restore formatting verification and retry format",
			)

	emit("format: complete - stage the changes, then run check")


def _load_snapshot_manifest(root: Path, results: _OperationResults) -> Manifest | None:
	try:
		operation = current_operation()
		if operation is None:
			manifest = load_manifest(root)
		else:
			with operation.stage("manifest.contract", "staged quality-gate.toml", check=True):
				manifest = load_manifest(root)
				operation.configure_budget(
					manifest.repository.gate_timeout_seconds,
					manifest.repository.command_timeout_seconds,
				)
	except ValidationError as error:
		operation = current_operation()
		if operation is not None:
			operation.mark_incomplete(
				"manifest.contract",
				"invalid staged manifest prevented the remaining checks",
			)
		results.append(_error_result(_manifest_error(error)))
		return None
	results.manifest = manifest
	return manifest


def _append_snapshot_baseline_checks(
	root: Path,
	manifest: Manifest,
	results: _OperationResults,
	*,
	repository_root: Path | None,
	index_file: Path | None,
	mode: Literal["check", "audit"],
) -> None:
	results.append(
		observe_check(
			"manifest.documents",
			"staged required documents",
			lambda: required_documents_result(root, manifest),
		)
	)
	results.extend(
		[
			*git_integrity_results(
				root,
				manifest,
				repository=repository_root or root,
				index_file=index_file,
			),
			observe_check(
				"repository.workflow",
				"staged GitHub workflow files",
				lambda: workflow_result(
					root,
					manifest,
					repository_root=repository_root or root,
				),
			),
			*documentation_results(root, manifest),
		]
	)
	results.append(
		observe_check(
			"lessons.learning",
			"staged lessons collection",
			lambda: lessons_result(root, is_complete_required=mode == "audit"),
		)
	)
	results.extend(web_budget_results(root, manifest))


def _prepare_snapshot_environment(
	root: Path,
	manifest: Manifest,
	results: _OperationResults,
	*,
	repository_root: Path | None,
	mode: Literal["check", "audit"],
	policy_release: str | None,
) -> tuple[list[PythonComponent], PreparedEnvironment] | None:
	try:
		components = load_components(root)
		operation = current_operation()
		if operation is not None:
			operation.plan_checks(_planned_check_scopes(root, manifest, components, mode))

		def prepare_environment() -> PreparedEnvironment:
			if policy_release is not None:
				return prepare_bootstrap(
					root,
					policy_release=policy_release,
					repository_root=repository_root or root,
				)
			return prepare(root, repository_root=repository_root or root)

		if operation is None:
			prepared = prepare_environment()
		else:
			with operation.stage(
				"policy.cache-validation",
				"selected immutable policy release and component runtime caches",
			):
				prepared = prepare_environment()
		operation = current_operation()
		if operation is not None:
			operation.plan_checks(
				_planned_check_scopes(root, manifest, components, mode, prepared=prepared)
			)
		return components, prepared
	except QualityGateError as error:
		operation = current_operation()
		if operation is not None:
			operation.mark_incomplete(
				"policy.cache-validation",
				"policy or runtime preparation prevented the remaining checks",
			)
		results.append(_error_result(error))
	except (DistributionError, RuntimeUnavailable, OSError) as error:
		operation = current_operation()
		if operation is not None:
			operation.mark_incomplete(
				"policy.cache-validation",
				"policy or runtime preparation prevented the remaining checks",
			)
		results.append(
			_error_result(
				QualityGateError(
					str(error),
					check_id="runtime.policy",
					exit_code=EXIT_UNCHECKED,
					recovery_action="run sync and setup, then retry the quality gate",
				)
			)
		)
	return None


def _append_snapshot_secret_checks(
	root: Path,
	manifest: Manifest,
	prepared: PreparedEnvironment,
	results: _OperationResults,
	*,
	repository_root: Path | None,
	base: str | None,
	head: str | None,
	mode: Literal["check", "audit"],
) -> None:
	operation = current_operation()
	if operation is None:
		history_base, history_head = _ci_history_refs(base, head)
	else:
		with operation.stage("history.refs", "requested CI base and candidate references"):
			history_base, history_head = _ci_history_refs(base, head)
	results.append(
		observe_check(
			"secrets.candidate",
			"staged candidate tree",
			lambda: secret_candidate_result(root, manifest, prepared),
			process_required=True,
		)
	)
	if mode == "audit":
		results.append(
			observe_check(
				"secrets.audit",
				"all reachable Git history",
				lambda: secret_audit_result(repository_root or root, manifest, prepared),
				process_required=True,
			)
		)
		return
	results.append(
		observe_check(
			"secrets.history",
			"requested base-to-head Git range",
			lambda: secret_history_result(
				repository_root or root,
				manifest,
				prepared,
				base=history_base,
				head=history_head,
			),
			process_required=True,
		)
	)


def _append_python_snapshot_checks(
	root: Path,
	manifest: Manifest,
	components: list[PythonComponent],
	prepared: PreparedEnvironment,
	results: _OperationResults,
	deadline: float,
) -> None:
	if not components:
		results.append(
			CheckResult(
				check_id="python.components",
				status=Status.NOT_APPLICABLE,
				summary="no Python component is declared",
				recovery_action="declare a Python component if Python checks apply",
			)
		)
		return
	try:
		run_results = _run_python_snapshot_checks(
			root, manifest, components, prepared, deadline=deadline
		)
	except QualityGateError as error:
		operation = current_operation()
		if operation is not None:
			operation.mark_incomplete(
				"python.checks",
				"Python verification preparation prevented the remaining component checks",
			)
		results.append(_error_result(error))
		return
	except (DistributionError, RuntimeUnavailable, OSError) as error:
		operation = current_operation()
		if operation is not None:
			operation.mark_incomplete(
				"python.checks",
				"Python verification preparation prevented the remaining component checks",
			)
		results.append(
			_error_result(
				QualityGateError(
					str(error),
					check_id="runtime.policy",
					exit_code=EXIT_UNCHECKED,
					recovery_action="run sync and setup, then retry the quality gate",
				)
			)
		)
		return
	results.extend(run_results)


def _check_snapshot(
	actual_root: Path,
	*,
	verbose: bool = False,
	repository_root: Path | None = None,
	index_file: Path | None = None,
	base: str | None = None,
	head: str | None = None,
	mode: Literal["check", "audit"] = "check",
	policy_release: str | None = None,
) -> Verdict:
	actual_root = actual_root.resolve()
	results = _OperationResults(actual_root)
	manifest = _load_snapshot_manifest(actual_root, results)
	if manifest is None:
		return Verdict(tuple(results))
	_append_snapshot_baseline_checks(
		actual_root,
		manifest,
		results,
		repository_root=repository_root,
		index_file=index_file,
		mode=mode,
	)
	prepared_snapshot = _prepare_snapshot_environment(
		actual_root,
		manifest,
		results,
		repository_root=repository_root,
		mode=mode,
		policy_release=policy_release,
	)
	if prepared_snapshot is None:
		return Verdict(tuple(results))
	components, prepared = prepared_snapshot
	_append_snapshot_secret_checks(
		actual_root,
		manifest,
		prepared,
		results,
		repository_root=repository_root,
		base=base,
		head=head,
		mode=mode,
	)
	_progress("repository checks completed; 1 stage remaining.")
	operation = current_operation()
	deadline = (
		operation.deadline
		if operation is not None
		else time.monotonic() + manifest.repository.gate_timeout_seconds
	)
	results.extend(_web_check_results(actual_root, manifest, prepared, results, deadline=deadline))
	_append_python_snapshot_checks(actual_root, manifest, components, prepared, results, deadline)
	_progress("component checks completed; preparing report.")
	return Verdict(tuple(results))


def check(
	root: Path | None = None,
	*,
	verbose: bool = False,
	base: str | None = None,
	head: str | None = None,
) -> Verdict:
	"""Run the complete quality contract against the exact staged candidate."""
	verdict = _run_snapshot(
		root,
		verbose=verbose,
		base=base,
		head=head,
		mode="check",
	)
	emit(render(verdict, verbose=verbose))
	return verdict


def _run_candidate_checks(
	operation: GateOperation,
	snapshot: CandidateSnapshot,
	snapshot_lifetime: ExitStack,
	*,
	verbose: bool,
	base: str | None,
	head: str | None,
	mode: Literal["check", "audit"],
	policy_release: str | None,
) -> None:
	with operation.stage(
		"candidate.checks",
		"checks declared by the staged candidate manifest",
	):
		verdict = _check_snapshot(
			snapshot.root,
			verbose=verbose,
			repository_root=operation.root,
			index_file=getattr(snapshot, "repository_index", None),
			base=base,
			head=head,
			mode=mode,
			policy_release=policy_release,
		)
	for result in verdict.results:
		operation.record_unmeasured_result(result)
	with operation.stage(
		"candidate.snapshot-validation",
		"staged Git index remained stable through checks",
	):
		snapshot_lifetime.close()


def _execute_snapshot_operation(
	operation: GateOperation,
	actual_root: Path | None,
	*,
	verbose: bool,
	base: str | None,
	head: str | None,
	mode: Literal["check", "audit"],
	policy_release: str | None,
) -> None:
	with operation.stage(
		"repository.root",
		"explicit repository path" if actual_root is not None else "Git repository root",
		process_required=actual_root is None,
	):
		resolved_root = repository_root(actual_root)
	operation.set_repository_root(resolved_root)
	with operation.stage(
		"manifest.budget",
		"staged repository timeout defaults",
		process_required=True,
	):
		_configure_operation_budget(resolved_root, operation)
	with ExitStack() as owned_workspaces:
		with operation.stage(
			"temporary.workspace",
			"repository-local or private system temporary workspace",
		):
			owned_workspaces.enter_context(
				temporary_workspace(
					resolved_root,
					workspace_registered=operation.register_workspace,
					cleanup_owner=operation.handoff_workspace,
					deadline=operation.deadline,
				)
			)
		snapshot_lifetime = ExitStack()
		owned_workspaces.enter_context(snapshot_lifetime)
		with operation.stage(
			"candidate.snapshot",
			"staged Git index and blob materialization",
		):
			snapshot = snapshot_lifetime.enter_context(
				candidate_snapshot(
					resolved_root,
					timeout_seconds=operation.command_timeout_seconds,
					deadline=operation.deadline,
					register_cleanup=operation.register_cleanup,
				)
			)
		_progress("preparation completed; 2 stages remaining.")
		operation.candidate = _snapshot_candidate_identity(snapshot)
		operation.scope = "staged Git index; checks declared by the candidate manifest"
		_run_candidate_checks(
			operation,
			snapshot,
			snapshot_lifetime,
			verbose=verbose,
			base=base,
			head=head,
			mode=mode,
			policy_release=policy_release,
		)


def _snapshot_failure_stage(operation: GateOperation) -> str:
	return (
		"candidate.snapshot-validation"
		if operation.last_completed_stage == "candidate.checks"
		else "candidate.snapshot"
	)


def _record_snapshot_failure(
	operation: GateOperation, error: SnapshotError, mode: Literal["check", "audit"]
) -> None:
	stage = _snapshot_failure_stage(operation)
	if time.monotonic() >= operation.deadline:
		operation.current_stage = stage
		operation.interrupted_stage = stage
		try:
			operation.remaining()
		except OperationTimeoutError as timeout:
			raise timeout from error
	operation.mark_incomplete(
		stage,
		"candidate snapshot construction or validation did not complete",
	)
	operation.record_unmeasured_result(
		_error_result(
			QualityGateError(
				error.message,
				check_id="candidate.snapshot",
				exit_code=EXIT_UNCHECKED,
				recovery_action=f"restore a stable supported Git index and run {mode} again",
			)
		),
		scope="staged Git index and blob materialization",
	)


def _record_workspace_failure(operation: GateOperation, error: TemporaryWorkspaceError) -> None:
	if time.monotonic() >= operation.deadline:
		operation.current_stage = "temporary.workspace"
		operation.interrupted_stage = "temporary.workspace"
		try:
			operation.remaining()
		except OperationTimeoutError as timeout:
			raise timeout from error
	operation.mark_incomplete(
		"temporary.workspace",
		"temporary workspace ownership or cleanup transfer did not complete",
	)
	operation.record_unmeasured_result(
		_error_result(
			QualityGateError(
				str(error),
				check_id="temporary.workspace",
				exit_code=EXIT_UNCHECKED,
				recovery_action=(
					"restore a writable repository temp directory and retry the quality gate"
				),
			)
		),
		scope="repository temporary workspace",
	)


def _run_snapshot_operation(
	operation: GateOperation,
	actual_root: Path | None,
	*,
	verbose: bool,
	base: str | None,
	head: str | None,
	mode: Literal["check", "audit"],
	policy_release: str | None,
) -> None:
	try:
		_execute_snapshot_operation(
			operation,
			actual_root,
			verbose=verbose,
			base=base,
			head=head,
			mode=mode,
			policy_release=policy_release,
		)
	except SnapshotError as error:
		_record_snapshot_failure(operation, error, mode)
	except TemporaryWorkspaceError as error:
		_record_workspace_failure(operation, error)
	except QualityGateError as error:
		operation.mark_incomplete(
			operation.current_stage or "preparation",
			"a required preparation stage prevented the quality checks",
		)
		operation.record_unmeasured_result(_error_result(error))


def _record_admission_failure(operation: GateOperation | None, error: AdmissionError) -> Verdict:
	quality_error = QualityGateError(
		str(error),
		check_id="runtime.admission",
		exit_code=EXIT_UNCHECKED,
		recovery_action=error.recovery_action,
	)
	if operation is not None and operation.finalized:
		operation.mark_incomplete(
			"runtime.admission",
			"machine-local Gate admission could not be released cleanly",
		)
		operation.record_unmeasured_result(
			_error_result(quality_error), scope="machine-local Gate admission"
		)
		return operation.verdict()
	return Verdict((_error_result(quality_error),))


def _run_snapshot(
	actual_root: Path | None,
	*,
	verbose: bool = False,
	base: str | None = None,
	head: str | None = None,
	mode: Literal["check", "audit"] = "check",
	policy_release: str | None = None,
) -> Verdict:
	"""Run one candidate snapshot and convert snapshot failures to a verdict."""
	_progress("this check may take a long time; wait for completion without polling.")
	operation: GateOperation | None = None
	try:
		admission = nullcontext() if running_in_ci() else gate_admission()
		with admission:
			operation = GateOperation(actual_root or Path.cwd(), mode)
			with operation:
				_run_snapshot_operation(
					operation,
					actual_root,
					verbose=verbose,
					base=base,
					head=head,
					mode=mode,
					policy_release=policy_release,
				)
	except AdmissionError as error:
		return _record_admission_failure(operation, error)
	if operation is None:
		raise RuntimeError("Gate operation was not initialized")
	return operation.verdict()


def bootstrap_check(
	root: Path,
	*,
	policy_release: str,
	base: str | None = None,
	head: str | None = None,
) -> Verdict:
	"""Run the current checkout against a previous immutable policy release."""
	verdict = _run_snapshot(
		root,
		base=base,
		head=head,
		policy_release=policy_release,
	)
	emit(render(verdict))
	return verdict


def audit(root: Path | None = None, *, verbose: bool = False) -> Verdict:
	"""Run every implemented domain and an explicit full-history secret audit."""
	verdict = _run_snapshot(
		root,
		verbose=verbose,
		mode="audit",
	)
	emit(render(verdict, verbose=verbose))
	return verdict


def bootstrap_audit(
	root: Path,
	*,
	policy_release: str,
	verbose: bool = False,
) -> Verdict:
	"""Audit one candidate using an explicitly selected immutable policy release."""
	verdict = _run_snapshot(
		root,
		verbose=verbose,
		mode="audit",
		policy_release=policy_release,
	)
	emit(render(verdict, verbose=verbose))
	return verdict
