"""Deterministic quality checks used by the local hook and CI."""

from __future__ import annotations

import hashlib
import io
import json
import locale
import logging
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import tokenize
import tomllib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

from quality_gate.contracts import (
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
from quality_gate.distribution import DistributionError
from quality_gate.integrity import documentation_results, git_integrity_results, workflow_result
from quality_gate.launcher import PreparedEnvironment, prepare
from quality_gate.lessons import lessons_result
from quality_gate.reporting import render
from quality_gate.runtime import RuntimeUnavailable
from quality_gate.secrets import secret_audit_result, secret_candidate_result, secret_history_result
from quality_gate.snapshot import SnapshotError, candidate_snapshot

_LOGGER = logging.getLogger(__name__)
MANIFEST_NAME = "quality-gate.toml"
POLICY_DIR = Path(__file__).resolve().parent / "policy"
EXIT_UNCHECKED = 2
MAX_COMMAND_OUTPUT_CHARS = 16_384
MAX_COMMAND_OUTPUT_BYTES = MAX_COMMAND_OUTPUT_CHARS * 4 + 1
MAX_DEPTRY_REPORT_BYTES = 1024 * 1024
MAX_DEPTRY_SOURCE_BYTES = 8 * 1024 * 1024
MAX_DEPTRY_TEXT_CHARS = 1000
PROCESS_CLEANUP_TIMEOUT_SECONDS = 1.0
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


def emit(message: str) -> None:
	sys.stdout.write(f"{message}\n")


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
	result = subprocess.run(
		["git", "rev-parse", "--show-toplevel"],
		capture_output=True,
		text=True,
		encoding="utf-8",
		check=False,
	)
	if result.returncode:
		raise QualityGateError(
			"Run quality-gate inside a Git repository or pass --root.",
			check_id="runtime.repository",
			exit_code=2,
			recovery_action="run the command inside a Git repository or pass --root",
		)
	return Path(result.stdout.strip()).resolve()


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
				typecheck=True,
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


def web_budget_results(root: Path, manifest: Manifest) -> tuple[CheckResult, ...]:
	return tuple(
		result
		for index, component in enumerate(manifest.web, start=1)
		for result in (
			_web_budget_result(root, component, index, language="javascript"),
			_web_budget_result(root, component, index, language="css"),
		)
	)


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
	pre_results: list[CheckResult] = []
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
			pre_results.extend(
				_biome_unchecked_results(
					component_index,
					str(quality_error),
					recovery_action=quality_error.recovery_action,
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
					fallback.append(result)
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
	return value[:MAX_COMMAND_OUTPUT_CHARS] + "\n[output truncated]"


def temporary_directory(root: Path) -> tempfile.TemporaryDirectory[str]:
	return tempfile.TemporaryDirectory(prefix="quality-gate-")


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
	remaining = min(float(timeout), deadline - time.monotonic())
	if remaining <= 0:
		raise QualityGateError(
			"quality gate exceeded its overall time budget",
			check_id="runtime.gate_timeout",
			exit_code=2,
			recovery_action="inspect slow checks and retry within the gate time budget",
		)
	return remaining


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
	return environment


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


def _execute_commands(
	actual_root: Path,
	environment: dict[str, str],
	commands: list[tuple[str, list[str], int]],
	component_timeout: int,
	deadline: float,
) -> tuple[list[str], list[QualityGateError], dict[str, str]]:
	executed: list[str] = []
	run_errors: list[QualityGateError] = []
	run_outputs: dict[str, str] = {}
	for check_id, command, timeout in commands:
		executed.append(check_id)
		try:
			output = run(
				command,
				actual_root,
				environment,
				timeout=_remaining(deadline, max(timeout, component_timeout)),
			)
			if output and check_id.endswith(".coverage_report"):
				run_outputs[check_id] = output
		except QualityGateError as error:
			error.check_id = check_id
			run_errors.append(error)
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
		coverage_output = run_outputs.get(check_id, "")
		summary = "check passed" if status is Status.PASSED else "check requires attention"
		recovery_action = "restore verification and fix the reported finding, then retry"
		if is_coverage_collection:
			summary = "coverage collection completed"
		if is_coverage_report:
			report_lines = [line.strip() for line in coverage_output.splitlines() if line.strip()]
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


def _run_python_checks(
	actual_root: Path,
	manifest: Manifest,
	components: list[PythonComponent],
	prepared: PreparedEnvironment,
	*,
	deadline: float,
) -> list[CheckResult]:
	results: list[CheckResult] = []
	with temporary_directory(actual_root) as temporary_path:
		environment = _safe_environment(temporary_path)
		temporary_root = Path(temporary_path)
		environment["QUALITY_GATE_POLICY_ROOT"] = str(prepared.policy_root)
		run_errors: list[QualityGateError] = []
		executed: list[str] = []
		run_outputs: dict[str, str] = {}
		for component_index, component in enumerate(components, start=1):
			if not component.path_exists:
				results.append(
					CheckResult(
						check_id=f"python.component_{component_index}.path",
						status=Status.UNCHECKED,
						summary="Python component path is unavailable",
						findings=(
							Finding(
								path=str(component.path.relative_to(actual_root)),
								message="declared component path does not exist",
								action="restore the declared component path",
							),
						),
						recovery_action=(
							"restore the declared component path and retry the quality gate"
						),
					)
				)
				results.append(
					_dependency_hygiene_result(
						actual_root,
						manifest,
						component,
						prepared,
						component_index,
						None,
						temporary_root / f"deptry-{component_index}.json",
						deadline,
					)
				)
				continue
			for missing_path_index, missing_path in enumerate(
				component.missing_test_paths, start=1
			):
				results.append(
					CheckResult(
						check_id=f"python.component_{component_index}.test_path_{missing_path_index}",
						status=Status.UNCHECKED,
						summary="declared test path is unavailable",
						findings=(
							Finding(
								path=str(missing_path.relative_to(actual_root)),
								message="declared test path does not exist",
								action="restore the declared test path",
							),
						),
						recovery_action="restore the declared test path and retry the quality gate",
					)
				)
			try:
				python_executable = _runtime_python(prepared, component_index)
			except QualityGateError as error:
				results.append(_error_result(error))
				results.append(
					_dependency_hygiene_result(
						actual_root,
						manifest,
						component,
						prepared,
						component_index,
						None,
						temporary_root / f"deptry-{component_index}.json",
						deadline,
					)
				)
				continue
			results.append(
				_dependency_hygiene_result(
					actual_root,
					manifest,
					component,
					prepared,
					component_index,
					python_executable,
					temporary_root / f"deptry-{component_index}.json",
					deadline,
				)
			)
			component_executed, component_errors, component_outputs = _execute_commands(
				actual_root,
				environment,
				_component_commands(
					actual_root, manifest, component, prepared, component_index, python_executable
				),
				component.timeout_seconds,
				deadline,
			)
			executed.extend(component_executed)
			run_errors.extend(component_errors)
			run_outputs.update(component_outputs)
	results.extend(_command_results(executed, run_errors, run_outputs))
	for component_index, component in enumerate(components, start=1):
		tests_not_applicable = component.tests is None
		has_coverage = _has_pinned_coverage(prepared)
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
			results.append(
				CheckResult(
					check_id=f"python.component_{component_index}.coverage",
					status=Status.NOT_APPLICABLE,
					summary=(
						"coverage is not applicable without tests"
						if tests_not_applicable and has_coverage
						else "coverage provider is not in the pinned policy inventory"
					),
					recovery_action=(
						"declare test paths before collecting report-only coverage"
						if tests_not_applicable and has_coverage
						else (
							"add a pinned coverage provider only when report-only coverage is "
							"required"
						)
					),
				)
			)
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


def _check_snapshot(
	actual_root: Path,
	*,
	verbose: bool = False,
	repository_root: Path | None = None,
	index_file: Path | None = None,
	base: str | None = None,
	head: str | None = None,
	mode: Literal["check", "audit"] = "check",
) -> Verdict:
	actual_root = actual_root.resolve()
	try:
		manifest = load_manifest(actual_root)
	except ValidationError as error:
		verdict = Verdict((_error_result(_manifest_error(error)),))
		return verdict
	contract_result = required_documents_result(actual_root, manifest)
	repository_results = [
		*git_integrity_results(
			actual_root,
			manifest,
			repository=repository_root or actual_root,
			index_file=index_file,
		),
		workflow_result(actual_root, manifest),
		*documentation_results(actual_root, manifest),
	]
	results = [contract_result, *repository_results]
	results.append(lessons_result(actual_root, is_complete_required=mode == "audit"))
	results.extend(web_budget_results(actual_root, manifest))
	try:
		components = load_components(actual_root)
		prepared = prepare(
			actual_root,
			repository_root=repository_root or actual_root,
		)
	except QualityGateError as error:
		return Verdict((*results, _error_result(error)))
	except (DistributionError, RuntimeUnavailable, OSError) as error:
		quality_error = QualityGateError(
			str(error),
			check_id="runtime.policy",
			exit_code=EXIT_UNCHECKED,
			recovery_action="run sync and setup, then retry the quality gate",
		)
		return Verdict((*results, _error_result(quality_error)))
	history_base, history_head = _ci_history_refs(base, head)
	results.append(secret_candidate_result(actual_root, manifest, prepared))
	if mode == "audit":
		results.append(secret_audit_result(repository_root or actual_root, manifest, prepared))
	else:
		results.append(
			secret_history_result(
				repository_root or actual_root,
				manifest,
				prepared,
				base=history_base,
				head=history_head,
			)
		)
	gate_deadline = time.monotonic() + manifest.repository.gate_timeout_seconds
	results.extend(
		_web_check_results(
			actual_root,
			manifest,
			prepared,
			results,
			deadline=gate_deadline,
		)
	)
	if not components:
		results.append(
			CheckResult(
				check_id="python.components",
				status=Status.NOT_APPLICABLE,
				summary="no Python component is declared",
				recovery_action="declare a Python component if Python checks apply",
			)
		)
	else:
		try:
			run_results = _run_python_snapshot_checks(
				actual_root,
				manifest,
				components,
				prepared,
				deadline=gate_deadline,
			)
		except QualityGateError as error:
			return Verdict((*results, _error_result(error)))
		except (DistributionError, RuntimeUnavailable, OSError) as error:
			quality_error = QualityGateError(
				str(error),
				check_id="runtime.policy",
				exit_code=EXIT_UNCHECKED,
				recovery_action="run sync and setup, then retry the quality gate",
			)
			return Verdict((*results, _error_result(quality_error)))
		results.extend(run_results)
	verdict = Verdict(tuple(results))
	return verdict


def check(
	root: Path | None = None,
	*,
	verbose: bool = False,
	base: str | None = None,
	head: str | None = None,
) -> Verdict:
	"""Run the complete quality contract against the exact staged candidate."""

	actual_root = repository_root(root)
	verdict = _run_snapshot(
		actual_root,
		verbose=verbose,
		base=base,
		head=head,
		mode="check",
	)
	emit(render(verdict, verbose=verbose))
	return verdict


def _run_snapshot(
	actual_root: Path,
	*,
	verbose: bool = False,
	base: str | None = None,
	head: str | None = None,
	mode: Literal["check", "audit"] = "check",
) -> Verdict:
	"""Run one candidate snapshot and convert snapshot failures to a verdict."""
	try:
		with candidate_snapshot(actual_root) as snapshot:
			verdict = _check_snapshot(
				snapshot.root,
				verbose=verbose,
				repository_root=actual_root,
				index_file=getattr(snapshot, "repository_index", None),
				base=base,
				head=head,
				mode=mode,
			)
	except SnapshotError as error:
		quality_error = QualityGateError(
			error.message,
			check_id="candidate.snapshot",
			exit_code=EXIT_UNCHECKED,
			recovery_action=f"restore a stable supported Git index and run {mode} again",
		)
		verdict = Verdict((_error_result(quality_error),))
	return verdict


def audit(root: Path | None = None, *, verbose: bool = False) -> Verdict:
	"""Run every implemented domain and an explicit full-history secret audit."""
	actual_root = repository_root(root)
	verdict = _run_snapshot(
		actual_root,
		verbose=verbose,
		mode="audit",
	)
	emit(render(verdict, verbose=verbose))
	return verdict
