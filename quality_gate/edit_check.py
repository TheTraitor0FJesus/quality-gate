"""Working-tree Ruff application: exact policy, isolated writes, final identities."""

from __future__ import annotations

import json
import os
import re
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import __version__
from .contracts import Manifest, PythonComponent, ValidationError, parse_manifest
from .distribution import PolicyCache
from .edit_inputs import (
	Configuration,
	ImportContext,
	SourceFile,
	read_bounded,
	regular_path,
	sources,
)
from .edit_policy import file_sha256, ruff_artifact, select_release, sha256, tool_path
from .edit_process import run_process
from .edit_protocol import (
	EXIT_UNCHECKED,
	MAX_FILE_BYTES,
	MAX_FINDINGS,
	SAFE_FIXES,
	EditError,
	encode,
)
from .edit_reuse import ResultReuse, identity_digest
from .runtime import RuntimeManager, runtime_fingerprint, runtime_identity

_PROBE = (
	"import json,sys; print(json.dumps({'version':"
	"f'{sys.version_info.major}.{sys.version_info.minor}', 'executable':sys.executable}))"
)


def environment() -> dict[str, str]:
	"""No project Python paths, Ruff environment overrides, credentials or caches."""
	allowed = {"SYSTEMROOT", "WINDIR", "PATH", "PATHEXT", "LANG", "LC_ALL", "TEMP", "TMP"}
	return {key: value for key, value in os.environ.items() if key.upper() in allowed} | {
		"PYTHONDONTWRITEBYTECODE": "1",
		"PYTHONIOENCODING": "utf-8",
		"PYTHONUTF8": "1",
	}


@dataclass(slots=True)
class Selection:
	tool: Path
	policy: Path
	identity: dict[str, Any]


class Invocation:
	"""One deadline and process accounting for all explicitly selected batches."""

	def __init__(self, request: dict[str, Any], deadline: float, workspace: Path) -> None:
		self.request = request
		self.read_only = request.get("phase") == "validate"
		self.write_only = request.get("phase") == "write"
		root = Path(request["root"])
		if (
			not root.is_absolute()
			or root.is_symlink()
			or not root.is_dir()
			or getattr(root.lstat(), "st_file_attributes", 0)
			& getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
		):
			raise EditError("unsafe_path")
		self.root = root.resolve(strict=True)
		self.deadline = deadline
		self.stage = "inputs"
		self.workspace = workspace
		self.process_count = 2  # Public entry process plus its supervised worker.
		self.cache = PolicyCache(request.get("cache_dir"))
		self.configuration = Configuration(self.root)
		self.imports: ImportContext | None = None
		self.releases: dict[str, tuple[Path, Path, dict[str, Any]]] = {}
		self.selections: dict[tuple[str, str, str], Selection] = {}
		self.artifacts: dict[Path, str] = {}
		self.interpreters: dict[Path, Path] = {}

	def run(self, command: list[str], *, cwd: Path) -> tuple[int, bytes, bytes]:
		self.stage = (
			"selection"
			if "--show-files" in command
			else "fix"
			if "--fix" in command
			else "format_check"
			if "--check" in command
			else "format"
			if command[1] == "format"
			else "lint"
			if command[1] == "check"
			else "runtime"
		)
		self.process_count += 1
		isolated = environment() | {
			"HOME": str(self.workspace),
			"USERPROFILE": str(self.workspace),
			"TEMP": str(self.workspace),
			"TMP": str(self.workspace),
		}
		return run_process(command, root=cwd, environment=isolated, deadline=self.deadline)

	def release(self, version: str) -> tuple[Path, Path, dict[str, Any]]:
		if version in self.releases:
			return self.releases[version]
		root, manifest = select_release(self.cache, version)
		artifact = ruff_artifact(root, manifest)
		tool = tool_path(self.cache, artifact.sha256)
		if (
			not tool.is_file()
			or not os.access(tool, os.X_OK)
			or tool.is_symlink()
			or file_sha256(tool, 64 * 1024 * 1024) != artifact.sha256
		):
			raise EditError("tool")
		policy = root / "quality_gate/policy/ruff.toml"
		identity = {
			"policy_release": version,
			"release_sha256": sha256((root / "release.toml").read_bytes()),
			"policy_sha256": sha256(policy.read_bytes()),
			"ruff_version": artifact.version,
			"ruff_sha256": artifact.sha256,
		}
		self.artifacts.update({root / item.path: item.sha256 for item in manifest.files})
		self.artifacts.update({root / item.path: item.sha256 for item in manifest.tools})
		self.artifacts[root / "release.toml"] = identity["release_sha256"]
		self.artifacts[tool] = identity["ruff_sha256"]
		self.releases[version] = tool, policy, identity
		return self.releases[version]

	def select(self, manifest: Manifest | None, component: PythonComponent | None) -> Selection:
		if manifest is not None and component is not None:
			version = manifest.policy_release
			self.release(version)
			for dependency in component.dependency_inputs:
				self.configuration.capture(dependency, required=True)
			identity = runtime_identity(self.root, manifest, component)
			manager = RuntimeManager(self.cache)
			metadata = manager.path_for(self.root, identity) / "runtime.json"
			if metadata.exists():
				self.artifacts[metadata] = sha256(read_bounded(metadata, 256 * 1024))
			inspection = manager.inspect(self.root, identity)
			if not inspection.current or inspection.python is None:
				raise EditError("runtime")
			python = inspection.python
			python_version = component.python_version
			fingerprint = runtime_fingerprint(identity)
		else:
			baseline = self.request.get("baseline")
			if (
				not isinstance(baseline, dict)
				or set(baseline) != {"policy_release", "python", "python_version"}
				or not all(isinstance(value, str) for value in baseline.values())
			):
				raise EditError("invalid_request")
			version, python_version = baseline["policy_release"], baseline["python_version"]
			python = Path(baseline["python"])
			fingerprint = sha256(json.dumps(baseline, sort_keys=True).encode())
		key = version, str(python), fingerprint
		if key in self.selections:
			return self.selections[key]
		tool, policy, provenance = self.release(version)
		if not python.is_absolute() or not python.is_file():
			raise EditError("runtime")
		python_target = python.resolve(strict=True)
		python_digest = file_sha256(python_target, 64 * 1024 * 1024)
		code, output, _stderr = self.run(
			[str(python), "-I", "-S", "-B", "-c", _PROBE], cwd=self.root
		)
		try:
			probe = json.loads(output)
			if (
				code
				or _stderr
				or probe["version"] != python_version
				or Path(probe["executable"]).resolve() != python_target
			):
				raise EditError("runtime")
		except (ValueError, TypeError, KeyError) as error:
			raise EditError("runtime") from error
		selection = Selection(
			tool,
			policy,
			provenance
			| {
				"python": str(python),
				"python_version": python_version,
				"python_sha256": python_digest,
				"runtime_fingerprint": fingerprint,
			},
		)
		self.selections[key] = selection
		self.interpreters[python] = python_target
		self.artifacts[python_target] = python_digest
		return selection

	def assert_artifacts_current(self) -> None:
		for python, target in self.interpreters.items():
			if python.resolve(strict=True) != target:
				raise EditError("stale_input")

		for path, expected in self.artifacts.items():
			if (
				not path.is_file()
				or path.is_symlink()
				or file_sha256(path, 64 * 1024 * 1024) != expected
			):
				raise EditError("stale_input")

	def assert_configuration_current(self) -> None:
		self.configuration.assert_current()
		if self.imports is not None:
			self.imports.assert_current()

	def check_batch(
		self, selection: Selection, files: list[SourceFile], workspace: Path
	) -> dict[str, dict[str, Any]]:
		arguments = [str(self.root / file.path) for file in files]
		common = ["--config", str(selection.policy), "--no-cache"]
		code, output, _stderr = self.run(
			[
				str(selection.tool),
				"check",
				*common,
				"--force-exclude",
				"--show-files",
				"--",
				*arguments,
			],
			cwd=self.root,
		)
		if code:
			raise EditError("configuration")
		try:
			included = {
				Path(line).resolve().relative_to(self.root).as_posix()
				for line in output.decode("utf-8").splitlines()
			}
		except (UnicodeError, ValueError) as error:
			raise EditError("tool_output") from error
		if not included <= {file.path for file in files}:
			raise EditError("tool_output")
		eligible = [file for file in files if file.path in included]
		results: dict[str, dict[str, Any]] = {
			file.path: {"status": "not_applicable", "format": "not_applicable", "findings": []}
			for file in files
		}
		if not eligible:
			return results
		# Match ordinary Gate's cwd-based first-party import classification without
		# copying or discovering unrelated source files in the isolated workspace.
		common.extend(
			[
				"--config",
				"src = " + json.dumps([str(self.root), str(self.root / "src")], ensure_ascii=False),
			]
		)
		for file in eligible:
			staged = workspace / file.path
			staged.parent.mkdir(parents=True, exist_ok=True)
			staged.write_bytes(file.content)
		paths = [str(workspace / file.path) for file in eligible]
		if self.read_only:
			return self.validate_batch(selection, eligible, common, paths, workspace, included)
		syntax = self.apply_batch(selection, eligible, common, paths, workspace, included)
		if self.write_only:
			return {
				file.path: {
					"status": "passed",
					"format": "not_applicable" if file.path in syntax else "passed",
					"findings": [],
				}
				for file in eligible
			}
		commands = [
			["check", *common, "--no-fix", "--output-format", "json"],
			["format", *common, "--check", "--output-format", "json"],
		]
		lint_code, lint_output, _stderr = self.run(
			[str(selection.tool), *commands[0], "--", *paths], cwd=workspace
		)
		findings = self.findings(lint_code, lint_output, workspace, included)
		check_code, format_output, _stderr = self.run(
			[str(selection.tool), *commands[1], "--", *paths], cwd=workspace
		)
		syntax = {item["path"] for item in findings if item["rule"] == "invalid-syntax"}
		if check_code not in {0, 1, EXIT_UNCHECKED} or (
			check_code == EXIT_UNCHECKED and not syntax
		):
			raise EditError("tool_output")
		unformatted = _format_report(check_code, format_output, workspace, included)
		for file in eligible:
			own_findings = [
				{key: value for key, value in item.items() if key != "path"}
				for item in findings
				if item["path"] == file.path
			]
			results[file.path] = {
				"status": "failed" if own_findings or file.path in unformatted else "passed",
				"format": "failed" if file.path in syntax | unformatted else "passed",
				"findings": own_findings,
			}
		return results

	def apply_batch(
		self,
		selection: Selection,
		files: list[SourceFile],
		common: list[str],
		paths: list[str],
		workspace: Path,
		included: set[str],
	) -> set[str]:
		"""Apply only the approved fixes and formatting to isolated file copies."""
		fix_code, fix_output, _stderr = self.run(
			[
				str(selection.tool),
				"check",
				*common,
				"--fix",
				"--fixable",
				",".join(SAFE_FIXES),
				"--no-unsafe-fixes",
				"--output-format",
				"json",
				"--",
				*paths,
			],
			cwd=workspace,
		)
		findings = self.findings(fix_code, fix_output, workspace, included)
		format_code, _output, _stderr = self.run(
			[str(selection.tool), "format", *common, "--", *paths], cwd=workspace
		)
		syntax = {item["path"] for item in findings if item["rule"] == "invalid-syntax"}
		if format_code not in {0, EXIT_UNCHECKED} or (format_code == EXIT_UNCHECKED and not syntax):
			raise EditError("tool_output")
		return syntax

	def validate_batch(
		self,
		selection: Selection,
		files: list[SourceFile],
		common: list[str],
		paths: list[str],
		workspace: Path,
		included: set[str],
	) -> dict[str, dict[str, Any]]:
		"""Run final lint and format checks on copies without changing project files."""
		lint_code, lint_output, _stderr = self.run(
			[
				str(selection.tool),
				"check",
				*common,
				"--no-fix",
				"--output-format",
				"json",
				"--",
				*paths,
			],
			cwd=workspace,
		)
		findings = self.findings(lint_code, lint_output, workspace, included)
		format_code, format_output, _stderr = self.run(
			[
				str(selection.tool),
				"format",
				*common,
				"--check",
				"--output-format",
				"json",
				"--",
				*paths,
			],
			cwd=workspace,
		)
		syntax = {item["path"] for item in findings if item["rule"] == "invalid-syntax"}
		if format_code not in {0, 1, EXIT_UNCHECKED} or (
			format_code == EXIT_UNCHECKED and not syntax
		):
			raise EditError("tool_output")
		unformatted = _format_report(format_code, format_output, workspace, included)
		results: dict[str, dict[str, Any]] = {}
		for file in files:
			own_findings = [
				{key: value for key, value in item.items() if key != "path"}
				for item in findings
				if item["path"] == file.path
			]
			results[file.path] = {
				"status": "failed" if own_findings or file.path in unformatted else "passed",
				"format": "failed" if file.path in syntax | unformatted else "passed",
				"findings": own_findings,
			}
		return results

	@staticmethod
	def findings(
		code: int, output: bytes, workspace: Path, included: set[str]
	) -> list[dict[str, Any]]:
		try:
			report = json.loads(output)
			if code not in {0, 1} or not isinstance(report, list) or bool(report) != bool(code):
				raise EditError("tool_output")
			if len(report) > MAX_FINDINGS:
				raise EditError("limits")
			findings = []
			for item in report:
				path = Path(item["filename"]).resolve().relative_to(workspace).as_posix()
				rule = item["code"]
				line, column = item["location"]["row"], item["location"]["column"]
				if (
					path not in included
					or not isinstance(rule, str)
					or not re.fullmatch(r"(?:[A-Z]+[0-9]+|invalid-syntax)", rule)
					or type(line) is not int
					or type(column) is not int
					or min(line, column) < 1
				):
					raise EditError("tool_output")
				findings.append(
					{
						"path": path,
						"check_id": "ruff",
						"rule": rule,
						"line": line,
						"column": column,
						"action": f"Resolve Ruff {rule} in the final file.",
					}
				)
			return findings
		except (ValueError, TypeError, KeyError) as error:
			raise EditError("tool_output") from error


def _format_report(code: int, output: bytes, workspace: Path, included: set[str]) -> set[str]:
	try:
		report = json.loads(output)
		if not isinstance(report, list) or (code == 0 and report):
			raise EditError("tool_output")
		unformatted = {
			Path(item["filename"]).resolve().relative_to(workspace).as_posix() for item in report
		}
		if not unformatted <= included or (code == 1 and not unformatted):
			raise EditError("tool_output")
		return unformatted
	except (ValueError, TypeError, KeyError) as error:
		raise EditError("tool_output") from error


def _component(manifest: Manifest | None, path: str) -> PythonComponent | None:
	if manifest is None:
		return None
	return next(
		(component for component in manifest.python if Path(path).is_relative_to(component.path)),
		None,
	)


def _publish(
	invocation: Invocation, files: list[SourceFile], workspace: Path, records: list[dict[str, Any]]
) -> None:
	_assert_inputs_current(invocation, files)
	for file, record in zip(files, records, strict=True):
		if not record["changed"]:
			continue
		if time.monotonic() >= invocation.deadline:
			raise EditError("deadline")
		target = regular_path(invocation.root, file.path)
		if sha256(read_bounded(target, MAX_FILE_BYTES)) != file.sha256:
			raise EditError("stale_input")
		staged = workspace / file.path
		staged.chmod(target.stat().st_mode)
		os.replace(staged, target)
	invocation.assert_configuration_current()
	for record in records:
		if (
			sha256(read_bounded(regular_path(invocation.root, record["path"]), MAX_FILE_BYTES))
			!= record["sha256"]
		):
			raise EditError("stale_input")


def _assert_inputs_current(invocation: Invocation, files: list[SourceFile]) -> None:
	invocation.assert_configuration_current()
	invocation.assert_artifacts_current()
	for file in files:
		if (
			sha256(read_bounded(regular_path(invocation.root, file.path), MAX_FILE_BYTES))
			!= file.sha256
		):
			raise EditError("stale_input")


def execute(request: dict[str, Any], deadline: float, workspace: Path) -> dict[str, Any]:
	"""Execute one isolated writer or read-only validation batch; fail closed when incomplete."""
	invocation = Invocation(request, deadline, workspace)
	try:
		return _execute(invocation, workspace)
	except EditError as error:
		error.stage = invocation.stage
		raise


def _reuse_identity(
	invocation: Invocation, records: list[dict[str, Any]], selections: list[Selection]
) -> str | None:
	if invocation.imports is None or invocation.imports.identity is None or not selections:
		return None
	return identity_digest(
		{
			"engine": __version__,
			"phase": "validate"
			if invocation.read_only
			else "write-only"
			if invocation.write_only
			else "legacy",
			"root": str(invocation.root),
			"files": [
				{
					key: record[key]
					for key in ("path", "sha256", "coverage", "component", "selection")
				}
				for record in records
			],
			"selections": [selection.identity for selection in selections],
			"inputs": invocation.configuration.as_list(),
			"imports": invocation.imports.identity if invocation.imports is not None else None,
			"artifacts": sorted(
				(str(path), digest) for path, digest in invocation.artifacts.items()
			),
		}
	)


def _reused_result(
	invocation: Invocation,
	files: list[SourceFile],
	records: list[dict[str, Any]],
	selections: list[Selection],
	workspace: Path,
) -> dict[str, Any] | None:
	cached = ResultReuse(invocation.deadline).lookup(
		_reuse_identity(invocation, records, selections)
	)
	if cached is None:
		return None
	for record in cached["files"]:
		record.update(input_sha256=record["sha256"], changed=False)
	cached.update(process_count=invocation.process_count, writes_may_have_occurred=False)
	if invocation.read_only:
		_assert_inputs_current(invocation, files)
	else:
		_publish(invocation, files, workspace, cached["files"])
	return cached


def _working_manifest(configuration: Configuration) -> Manifest | None:
	manifest_content = configuration.capture("quality-gate.toml")
	try:
		return (
			parse_manifest(manifest_content.decode("utf-8"))
			if manifest_content is not None
			else None
		)
	except (ValidationError, UnicodeError) as error:
		raise EditError("manifest") from error


def _source_batches(
	invocation: Invocation, files: list[SourceFile], manifest: Manifest | None
) -> tuple[list[dict[str, Any]], list[Selection], dict[int, list[SourceFile]]]:
	"""Build result records and the exact policy batches for these source files."""
	batches: dict[int, list[SourceFile]] = {}
	selections: list[Selection] = []
	records: list[dict[str, Any]] = []
	for file in files:
		component = _component(manifest, file.path)
		record: dict[str, Any] = {
			"path": file.path,
			"status": "not_applicable",
			"coverage": "managed" if component else "baseline",
			"component": component.name if component else None,
			"input_sha256": file.sha256,
			"sha256": file.sha256,
			"changed": False,
			"selection": None,
			"findings": [],
			"format": "not_applicable",
		}
		if Path(file.path).suffix in {".py", ".pyi"}:
			selection = invocation.select(manifest, component)
			if selection not in selections:
				selections.append(selection)
			index = selections.index(selection)
			record["selection"] = index
			batches.setdefault(index, []).append(file)
		records.append(record)
	return records, selections, batches


def _apply_batches(
	invocation: Invocation,
	workspace: Path,
	files: list[SourceFile],
	selections: list[Selection],
	batches: dict[int, list[SourceFile]],
	records: list[dict[str, Any]],
) -> None:
	for index, batch in batches.items():
		results = invocation.check_batch(selections[index], batch, workspace)
		for record in records:
			if record["path"] not in results:
				continue
			record.update(results[record["path"]])
			if record["status"] == "not_applicable":
				continue
			staged = workspace / record["path"]
			record["sha256"] = sha256(read_bounded(staged, MAX_FILE_BYTES))
			record["changed"] = record["sha256"] != record["input_sha256"]


def _result_status(records: list[dict[str, Any]]) -> str:
	statuses = {record["status"] for record in records}
	if "failed" in statuses:
		return "failed"
	if "passed" in statuses:
		return "passed"
	return "not_applicable"


def _execute(invocation: Invocation, workspace: Path) -> dict[str, Any]:
	request = invocation.request
	files = sources(invocation.root, request["files"])
	invocation.imports = ImportContext(invocation.root, files)
	configuration = invocation.configuration
	manifest = _working_manifest(configuration)
	configuration.for_sources(files)
	records, selections, batches = _source_batches(invocation, files, manifest)
	cached = _reused_result(invocation, files, records, selections, workspace)
	if cached is not None:
		return cached
	_apply_batches(invocation, workspace, files, selections, batches, records)
	if sum(len(record["findings"]) for record in records) > MAX_FINDINGS:
		raise EditError("limits")
	result = {
		"schema": 1,
		"engine_version": __version__,
		"root": str(invocation.root),
		"status": _result_status(records),
		"reason": None,
		"files": records,
		"selections": [selection.identity for selection in selections],
		"inputs": configuration.as_list(),
		"process_count": invocation.process_count,
		"writes_may_have_occurred": any(record["changed"] for record in records),
	}
	if invocation.read_only:
		result["phase"] = "validate"
	encode(result)
	if invocation.read_only:
		_assert_inputs_current(invocation, files)
	else:
		_publish(invocation, files, workspace, records)
	ResultReuse(invocation.deadline).remember(
		_reuse_identity(invocation, records, selections), result
	)
	return result
