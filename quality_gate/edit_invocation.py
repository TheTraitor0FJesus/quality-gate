"""One deadline, artifact selection and Ruff process accounting."""

from __future__ import annotations

import json
import os
import re
import stat
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import __version__
from .admission import running_in_ci
from .contracts import Manifest, PythonComponent
from .distribution import PolicyCache
from .edit_inputs import (
	Configuration,
	ImportContext,
	SourceFile,
	read_bounded,
)
from .edit_policy import file_sha256, ruff_artifact, select_release, sha256, tool_path
from .edit_process import run_process
from .edit_protocol import (
	EXIT_UNCHECKED,
	MAX_FINDINGS,
	SAFE_FIXES,
	EditError,
)
from .edit_reuse import ResultReuse
from .edit_ruff_support import (
	Selection,
	_assert_inputs_current,
	_file_local_identity,
	_format_report,
)
from .runtime import RuntimeManager, runtime_fingerprint, runtime_identity
from .verification_evidence import (
	EvidenceHit,
	EvidenceProducer,
	engine_identity,
	valid_ruff_file_outcome,
)

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
		self.started_monotonic = time.monotonic()
		self.started_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
		self.engine_identity = engine_identity(Path(__file__).parent, version=__version__)
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
		self.result_reuse = ResultReuse(deadline)
		self.evidence_store = self.result_reuse.store
		self.reuse_enabled = not running_in_ci()

	def evidence_producer(self, operation: str) -> EvidenceProducer | None:
		if self.engine_identity is None:
			return None
		return EvidenceProducer(
			operation=operation,
			engine=self.engine_identity,
			started_at=self.started_at,
			completed_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
			duration_ms=max(0, int((time.monotonic() - self.started_monotonic) * 1000)),
		)

	def assert_engine_current(self) -> None:
		if (
			self.engine_identity is not None
			and engine_identity(Path(__file__).parent, version=__version__) != self.engine_identity
		):
			raise EditError("stale_input")

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
		self.assert_engine_current()
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
		self.assert_engine_current()
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
		_assert_inputs_current(self, files)
		eligible = [file for file in files if file.path in included]
		eligible_paths = [str(workspace / file.path) for file in eligible]
		identities, cached, misses = self._lookup_local_evidence(eligible, selection)

		import_code, import_output, _stderr = self.run(
			[
				str(selection.tool),
				"check",
				*common,
				"--force-exclude",
				"--select",
				"I",
				"--no-fix",
				"--output-format",
				"json",
				"--",
				*eligible_paths,
			],
			cwd=workspace,
		)
		eligible_set = {file.path for file in eligible}
		import_findings = self.findings(import_code, import_output, workspace, eligible_set)
		imports_by_path: dict[str, list[dict[str, Any]]] = {}
		for finding in import_findings:
			if finding["rule"].startswith("I"):
				imports_by_path.setdefault(finding["path"], []).append(finding)
			elif finding["rule"] != "invalid-syntax":
				raise EditError("tool_output")

		fresh = self._validate_missing(selection, misses, workspace) if misses else {}

		_assert_inputs_current(self, files)
		if self.reuse_enabled:
			producer = self.evidence_producer("edit-check.validate")
			for file in misses:
				identity = identities.get(file.path)
				outcome = fresh.get(file.path)
				if (
					identity is not None
					and producer is not None
					and outcome is not None
					and valid_ruff_file_outcome(outcome, path=file.path)
				):
					self.evidence_store.publish(
						identity,
						producer=producer,
						scope=identity["scope"],
						outcome=outcome,
					)

		results: dict[str, dict[str, Any]] = {
			file.path: {
				"status": "not_applicable",
				"format": "not_applicable",
				"findings": [],
				"evidence": "not_applicable",
			}
			for file in files
			if file.path not in eligible_set
		}
		for file in files:
			if file.path not in eligible_set:
				continue
			local = cached.get(file.path, fresh.get(file.path))
			if local is None:
				raise EditError("tool_output")
			combined = [
				*local["findings"],
				*[
					{
						"path": item["path"],
						"rule": item["rule"],
						"line": item["line"],
						"column": item["column"],
					}
					for item in imports_by_path.get(file.path, [])
				],
			]
			own_findings = [
				{
					"check_id": "ruff",
					"rule": item["rule"],
					"line": item["line"],
					"column": item["column"],
					"action": f"Resolve Ruff {item['rule']} in the final file.",
				}
				for item in combined
			]
			results[file.path] = {
				"status": "failed"
				if own_findings or local["status"] == "failed" or local["format_status"] == "failed"
				else "passed",
				"format": local["format_status"],
				"findings": own_findings,
				"evidence": "reused" if file.path in cached else "executed",
			}
		return results

	def _lookup_local_evidence(
		self, eligible: list[SourceFile], selection: Selection
	) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], list[SourceFile]]:
		identities: dict[str, dict[str, Any]] = {}
		cached: dict[str, dict[str, Any]] = {}
		misses: list[SourceFile] = []
		for file in eligible:
			identity = _file_local_identity(self, file, selection)
			if not self.reuse_enabled or identity is None or self.engine_identity is None:
				misses.append(file)
				continue
			identities[file.path] = identity
			hit: EvidenceHit | None = self.evidence_store.lookup(
				identity, expected_engine=self.engine_identity
			)
			if hit is not None and valid_ruff_file_outcome(hit.outcome, path=file.path):
				cached[file.path] = hit.outcome
			else:
				misses.append(file)

		return identities, cached, misses

	def _validate_missing(
		self, selection: Selection, misses: list[SourceFile], workspace: Path
	) -> dict[str, dict[str, Any]]:
		fresh: dict[str, dict[str, Any]] = {}
		missed_paths = [str(workspace / file.path) for file in misses]
		missed_included = {file.path for file in misses}
		local_common = ["--config", str(selection.policy), "--no-cache"]
		lint_code, lint_output, _stderr = self.run(
			[
				str(selection.tool),
				"check",
				*local_common,
				"--force-exclude",
				"--no-fix",
				"--extend-ignore",
				"I",
				"--output-format",
				"json",
				"--",
				*missed_paths,
			],
			cwd=workspace,
		)
		local_findings = self.findings(lint_code, lint_output, workspace, missed_included)
		format_code, format_output, _stderr = self.run(
			[
				str(selection.tool),
				"format",
				*local_common,
				"--force-exclude",
				"--check",
				"--output-format",
				"json",
				"--",
				*missed_paths,
			],
			cwd=workspace,
		)
		syntax = {item["path"] for item in local_findings if item["rule"] == "invalid-syntax"}
		if format_code not in {0, 1, EXIT_UNCHECKED} or (
			format_code == EXIT_UNCHECKED and not syntax
		):
			raise EditError("tool_output")
		unformatted = _format_report(format_code, format_output, workspace, missed_included)
		for file in misses:
			own_local = [item for item in local_findings if item["path"] == file.path]
			file_syntax = file.path in syntax
			local_diagnostics = [
				{
					"path": item["path"],
					"rule": item["rule"],
					"line": item["line"],
					"column": item["column"],
				}
				for item in own_local
			]
			lint_status = "failed" if local_diagnostics else "passed"
			file_format = "failed" if file_syntax or file.path in unformatted else "passed"
			fresh[file.path] = {
				"status": "failed"
				if lint_status == "failed" or file_format == "failed"
				else "passed",
				"lint_status": lint_status,
				"format_status": file_format,
				"findings": local_diagnostics,
			}

		return fresh

	@staticmethod
	def findings(
		code: int, output: bytes, workspace: Path, included: set[str]
	) -> list[dict[str, Any]]:
		try:
			report = json.loads(output)
			if (
				code not in {0, 1, EXIT_UNCHECKED}
				or not isinstance(report, list)
				or (code == 0 and report)
				or (code in {1, EXIT_UNCHECKED} and not report)
			):
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
			if code == EXIT_UNCHECKED and not any(
				finding["rule"] == "invalid-syntax" for finding in findings
			):
				raise EditError("tool_output")
			return findings
		except (ValueError, TypeError, KeyError) as error:
			raise EditError("tool_output") from error
