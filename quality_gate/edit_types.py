"""Read-only whole-scope typing with bounded source-free evidence."""

from __future__ import annotations

import ast
import configparser
import json
import re
import time
import tomllib
from pathlib import Path
from typing import Any

from . import __version__
from .contracts import PythonComponent, ValidationError, parse_manifest
from .distribution import PolicyCache, default_cache_root
from .edit_inputs import Configuration, SourceFile
from .edit_policy import select_release
from .edit_process import run_process
from .edit_protocol import (
	BACKGROUND_SCHEMA,
	MAX_FINDINGS,
	RECOVERY,
	EditError,
	background_identity,
	encode,
)
from .edit_reuse import identity_digest
from .edit_type_environment import (
	TypingEnvironment,
	assert_environment_current,
	process_environment,
	select_environment,
)
from .edit_type_inputs import (
	MAX_SOURCE_BYTES,
	MAX_SOURCES,
	assert_deadline,
	inventory,
	request_files,
)

CONFIGURATION_NAMES = (
	"quality-gate.toml",
	"pyproject.toml",
	"mypy.ini",
	".mypy.ini",
	"setup.cfg",
	"requirements.txt",
	"requirements-dev.txt",
	"uv.lock",
	"poetry.lock",
	"Pipfile",
	"Pipfile.lock",
)
DIRECTORY_CONFIGURATION_NAMES = (
	"pyproject.toml",
	"mypy.ini",
	".mypy.ini",
	"setup.cfg",
	"quality-gate.toml",
)
TYPING_RECOVERY = RECOVERY | {
	"invalid_request": "Send a valid schema 1 typing request with current identities.",
	"tool": "Restore the exact released mypy and its dependencies in the selected environment.",
	"runtime": "Run managed Gate setup or explicitly prepare the project-local .venv.",
	"release": "Synchronize the exact immutable policy release through quality-gate sync.",
	"limits": (
		"Use full Gate for this scope; an oversized component cannot be split into partial success."
	),
	"dependencies": "Restore required declared dependencies and typing stubs, then retry.",
	"tool_output": (
		"Restore the selected mypy installation; raw tool output is intentionally omitted."
	),
}


def typing_unchecked(reason: str) -> dict[str, Any]:
	"""Return the exact early-failure envelope without source or subprocess text."""
	return {
		"schema": 1,
		"check_id": "mypy",
		"engine_version": __version__,
		"status": "unchecked",
		"reason": reason,
		"action": TYPING_RECOVERY[reason],
		"writes_may_have_occurred": False,
		"root": None,
		"root_identity": None,
		"files": [],
		"configuration": [],
		"selections": [],
		"components": [],
		"freshness_sha256": None,
		"process_count": 0,
		"elapsed_ms": 0,
	}


def execute(request: dict[str, Any], deadline: float, workspace: Path) -> dict[str, Any]:
	"""Analyze one working-tree request under the caller's remaining deadline."""
	return _TypingInvocation(request, deadline, workspace).execute()


class _TypingConfiguration(Configuration):
	"""Validate captured settings as data without loading project plugins."""

	def capture(self, relative: str, *, required: bool = False) -> bytes | None:
		content = super().capture(relative, required=required)
		if content is None:
			return None
		name = Path(relative).name
		try:
			if name in {"uv.lock", "poetry.lock", "Pipfile"} or (
				name.endswith(".toml") and name != "quality-gate.toml"
			):
				tomllib.loads(content.decode("utf-8"))
			elif name == "Pipfile.lock":
				settings = json.loads(
					content.decode("utf-8"),
					object_pairs_hook=self._json_object,
					parse_constant=self._invalid_json_constant,
				)
				if not isinstance(settings, dict):
					raise EditError("configuration")
			elif name in {"mypy.ini", ".mypy.ini", "setup.cfg"}:
				configparser.RawConfigParser().read_string(content.decode("utf-8"))
		except (ValueError, UnicodeError, configparser.Error) as error:
			raise EditError("configuration") from error
		return content

	@staticmethod
	def _json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
		settings = dict(pairs)
		if len(settings) != len(pairs):
			raise EditError("configuration")
		return settings

	@staticmethod
	def _invalid_json_constant(_value: str) -> None:
		raise EditError("configuration")


class _TypingInvocation:
	def __init__(self, request: dict[str, Any], deadline: float, workspace: Path) -> None:
		self.request, self.deadline, self.workspace = request, deadline, workspace
		self.root = Path(request["root"]).resolve(strict=True)
		self.files = request_files(self.root, request["files"])
		self.configuration = _TypingConfiguration(self.root)
		for name in CONFIGURATION_NAMES:
			self.configuration.capture(name)
		for item in self.files:
			if Path(item["path"]).name in CONFIGURATION_NAMES:
				self.configuration.capture(item["path"])
		_cache_outside_project(Path(request.get("cache_dir", default_cache_root())), self.root)
		self.cache = PolicyCache(request.get("cache_dir"))
		_cache_outside_project(self.cache.root, self.root)
		metadata = self.root.stat()
		self.result = typing_unchecked("invalid_request") | {
			"status": "not_applicable",
			"reason": None,
			"action": None,
			"root": str(self.root),
			"root_identity": {"device": metadata.st_dev, "inode": metadata.st_ino},
			"files": self.files,
			"process_count": 2,
		}
		self.result.update(background_identity(request))
		content = self.configuration.capture("quality-gate.toml")
		try:
			self.manifest = parse_manifest(content.decode("utf-8")) if content is not None else None
		except (ValidationError, UnicodeError) as error:
			raise EditError("manifest") from error
		if self.manifest is not None and self.files:
			select_release(self.cache, self.manifest.policy_release)
		self.snapshot = workspace / "source"
		self.snapshot.mkdir()
		self.selections: list[TypingEnvironment] = []
		self.scopes: list[tuple[str, bool, list[SourceFile], list[dict[str, Any]]]] = []

	def execute(self) -> dict[str, Any]:
		for component in self._affected():
			self._check_component(component)
		self._assert_current()
		self.result["configuration"] = self.configuration.as_list()
		statuses = {record["status"] for record in self.result["components"]}
		self.result["status"] = (
			"failed"
			if "failed" in statuses
			else "passed"
			if "passed" in statuses
			else "not_applicable"
		)
		self.result["freshness_sha256"] = _freshness_digest(self.result)
		assert_deadline(self.deadline)
		encode(self.result)
		return self.result

	def _affected(self) -> list[PythonComponent | None]:
		if self.manifest is not None:
			return [
				component
				for component in self.manifest.python
				if any(
					_affects(item["path"], component.path, component.dependency_inputs)
					for item in self.files
				)
			]
		if any(
			Path(item["path"]).suffix in {".py", ".pyi"}
			or Path(item["path"]).name in CONFIGURATION_NAMES
			for item in self.files
		):
			return [None]
		return []

	def _check_component(self, component: PythonComponent | None) -> None:
		managed = component is not None
		path = component.path if component is not None else "."
		record = _component_record(
			component.name if component is not None else "project", path, managed
		)
		if component is not None:
			for dependency in component.dependency_inputs:
				self.configuration.capture(dependency)
		if component is not None and not component.typecheck:
			record["reason"] = "typecheck_disabled"
			self.result["components"].append(record)
			return
		sources, directories = inventory(self.root, path, managed=managed, deadline=self.deadline)
		if not sources:
			raise EditError("configuration")
		_capture_configuration(self.configuration, directories, path, managed)
		selection = select_environment(
			self.root,
			self.cache,
			self.manifest,
			component,
			self.request.get("baseline"),
			self.configuration,
			self.deadline,
			self.workspace,
		)
		self.result["process_count"] += 1
		record.update(self._selection_record(component, sources, directories, selection))
		self._snapshot_sources(sources, path)
		record["import_count"] = _import_context(sources, path, selection)
		started = time.monotonic()
		record["diagnostics"] = _analyze(
			self.snapshot, path, record["inventory"], selection, self.workspace, self.deadline
		)
		record["analysis_ms"] = round((time.monotonic() - started) * 1000)
		self.result["process_count"] += 1
		record["status"] = "failed" if record["diagnostics"] else "passed"
		self.result["components"].append(record)
		self.scopes.append((path, managed, sources, directories))
		self._assert_limits()

	def _selection_record(
		self,
		component: PythonComponent | None,
		sources: list[SourceFile],
		directories: list[dict[str, Any]],
		selection: TypingEnvironment,
	) -> dict[str, Any]:
		index = len(self.selections)
		self.selections.append(selection)
		self.result["selections"].append(selection.provenance)
		record = {
			"selection": index,
			"inventory": [{"path": source.path, "sha256": source.sha256} for source in sources],
			"directories": directories,
			"source_count": len(sources),
			"dependency_inputs": [
				{"path": relative, "sha256": self.configuration.identities[relative]}
				for relative in sorted(
					set(
						component.dependency_inputs
						if component is not None
						else CONFIGURATION_NAMES
					)
				)
				if self.configuration.identities.get(relative) is not None
			],
		}
		record["scope_sha256"] = identity_digest(
			{"inventory": record["inventory"], "directories": directories}
		)
		return record

	def _snapshot_sources(self, sources: list[SourceFile], path: str) -> None:
		for source in sources:
			target = self.snapshot / source.path
			target.parent.mkdir(parents=True, exist_ok=True)
			target.write_bytes(source.content)
		_copy_ancestors(self.configuration, self.snapshot, path)

	def _assert_current(self) -> None:
		for path, managed, sources, directories in self.scopes:
			current_sources, current_directories = inventory(
				self.root, path, managed=managed, deadline=self.deadline
			)
			if current_sources != sources or current_directories != directories:
				raise EditError("stale_input")
		for selection in self.selections:
			assert_environment_current(selection, self.deadline)
		self.configuration.assert_current()
		metadata = self.root.stat()
		if request_files(self.root, self.request["files"]) != self.files or self.result[
			"root_identity"
		] != {"device": metadata.st_dev, "inode": metadata.st_ino}:
			raise EditError("stale_input")

	def _assert_limits(self) -> None:
		source_count = sum(len(sources) for _path, _managed, sources, _directories in self.scopes)
		source_bytes = sum(
			len(source.content)
			for _path, _managed, sources, _directories in self.scopes
			for source in sources
		)
		diagnostic_count = sum(len(record["diagnostics"]) for record in self.result["components"])
		if (
			source_count > MAX_SOURCES
			or source_bytes > MAX_SOURCE_BYTES
			or diagnostic_count > MAX_FINDINGS
		):
			raise EditError("limits")


def _freshness_digest(result: dict[str, Any]) -> str:
	identity = {
		key: result[key]
		for key in ("root", "root_identity", "files", "configuration", "selections")
	}
	identity["components"] = [
		{key: value for key, value in record.items() if key != "analysis_ms"}
		for record in result["components"]
	]
	if result["schema"] == BACKGROUND_SCHEMA:
		identity.update({key: result[key] for key in ("schema", "mode", "event_id")})
	return identity_digest(identity)


def _cache_outside_project(cache: Path, root: Path) -> None:
	if not cache.is_absolute() or cache.resolve().is_relative_to(root):
		raise EditError("unsafe_path")
	for path in (cache, *cache.parents):
		if path.is_symlink() or (
			path.exists() and getattr(path.lstat(), "st_file_attributes", 0) & 0x400
		):
			raise EditError("unsafe_path")


def _affects(path: str, component: str, dependencies: tuple[str, ...]) -> bool:
	return (
		path in dependencies
		or Path(path).name in CONFIGURATION_NAMES
		or (Path(path).suffix in {".py", ".pyi"} and Path(path).is_relative_to(component))
	)


def _component_record(name: str, path: str, managed: bool) -> dict[str, Any]:
	return {
		"name": name,
		"path": path,
		"coverage": "managed" if managed else "baseline",
		"status": "not_applicable",
		"reason": None,
		"selection": None,
		"inventory": [],
		"directories": [],
		"scope_sha256": identity_digest({"inventory": [], "directories": []}),
		"dependency_inputs": [],
		"diagnostics": [],
		"source_count": 0,
		"import_count": 0,
		"analysis_ms": 0,
	}


def _capture_configuration(
	configuration: Configuration, directories: list[dict[str, Any]], path: str, managed: bool
) -> None:
	for directory in directories:
		for ancestor in (Path(directory["path"]), *Path(directory["path"]).parents):
			for name in DIRECTORY_CONFIGURATION_NAMES:
				relative = (ancestor / name).as_posix()
				content = configuration.capture(relative)
				if (
					not managed
					and name in {"pyproject.toml", "quality-gate.toml"}
					and ancestor != Path(".")
					and content is not None
				):
					raise EditError("configuration")
	if managed:
		for ancestor in Path(path).parents:
			for name in ("__init__.py", "__init__.pyi"):
				configuration.capture((ancestor / name).as_posix())


def _copy_ancestors(configuration: Configuration, snapshot: Path, path: str) -> None:
	if path == ".":
		return
	for ancestor in Path(path).parents:
		for name in ("__init__.py", "__init__.pyi"):
			relative = (ancestor / name).as_posix()
			content = configuration.capture(relative)
			if content is not None:
				target = snapshot / relative
				target.parent.mkdir(parents=True, exist_ok=True)
				target.write_bytes(content)


def _import_context(sources: list[SourceFile], scope: str, selection: TypingEnvironment) -> int:
	local_modules = _local_modules(sources, scope)
	count = 0
	for source in sources:
		for names in _source_imports(source, local_modules):
			count += 1
			for name in names:
				if (
					name not in local_modules
					and name not in selection.stdlib
					and name != "os.path"  # The stdlib re-exports the platform path module.
					and not _typed_dependency(selection.site, name)
				):
					raise EditError("dependencies")
	return count


def _local_modules(sources: list[SourceFile], scope: str) -> set[str]:
	local_modules: set[str] = set()
	for source in sources:
		for path in (Path(source.path), Path(source.path).relative_to(scope)):
			parts = list(path.with_suffix("").parts)
			if parts[-1] == "__init__":
				parts.pop()
			for length in range(1, len(parts) + 1):
				local_modules.add(".".join(parts[:length]))
	return local_modules


def _source_imports(source: SourceFile, local_modules: set[str]) -> list[list[str]]:
	try:
		nodes = ast.walk(ast.parse(source.content))
	except SyntaxError:
		return []  # The released checker emits its source-free syntax location.
	imports = []
	for node in nodes:
		if isinstance(node, ast.Import):
			imports.append([alias.name for alias in node.names])
		elif isinstance(node, ast.ImportFrom):
			if node.level:
				package = Path(source.path).parent.parts
				if node.level > len(package):
					raise EditError("dependencies")
				parts = (
					*package[: len(package) - node.level + 1],
					*(node.module.split(".") if node.module else ()),
				)
				if ".".join(parts) not in local_modules:
					raise EditError("dependencies")
				imports.append([])
			else:
				imports.append([node.module] if node.module else [])
	return imports


def _typed_dependency(site: Path, name: str) -> bool:
	parts = name.split(".")
	package = site / parts[0]
	stub = site / (parts[0] + "-stubs")
	stub_module = stub.joinpath(*parts[1:])
	stdlib_module = (site / "mypy/typeshed/stdlib").joinpath(*parts)
	return (
		stdlib_module.with_suffix(".pyi").is_file()
		or (stdlib_module / "__init__.pyi").is_file()
		or stub_module.with_suffix(".pyi").is_file()
		or (stub_module / "__init__.pyi").is_file()
		or (len(parts) == 1 and (site / parts[0]).with_suffix(".pyi").is_file())
		or (
			(package / "py.typed").is_file()
			and (
				site.joinpath(*parts).is_dir()
				or site.joinpath(*parts).with_suffix(".py").is_file()
				or site.joinpath(*parts).with_suffix(".pyi").is_file()
			)
		)
	)


def _analyze(
	snapshot: Path,
	path: str,
	inventory_records: list[dict[str, Any]],
	selection: TypingEnvironment,
	workspace: Path,
	deadline: float,
) -> list[dict[str, Any]]:
	runtime = (
		selection.site.parent.parent
		if selection.site.parent.name == "Lib"
		else selection.site.parents[2]
	)
	entry = (
		"import sys,runpy\nfrom pathlib import Path\n"
		"from importlib.machinery import PathFinder\n"
		f"sys.prefix=sys.exec_prefix={str(runtime)!r}\n"
		f"site=Path({str(selection.site)!r})\n"
		f"allowed={selection.tool_imports!r}\n"
		"class ToolImports:\n"
		" def find_spec(self,fullname,path=None,target=None):\n"
		"  spec=PathFinder.find_spec(fullname,path)\n"
		"  if spec and spec.origin and Path(spec.origin).is_relative_to(site):\n"
		"   if fullname.split('.')[0] not in allowed: raise ModuleNotFoundError(fullname)\n"
		"  return None\n"
		"sys.meta_path.insert(0,ToolImports())\nsys.path.append(str(site))\n"
		"sys.argv=['mypy',*sys.argv[1:]]\nrunpy.run_module('mypy',run_name='__main__')"
	)
	code, output, stderr = run_process(
		[
			str(selection.python),
			"-I",
			"-S",
			"-B",
			"-X",
			f"pycache_prefix={workspace / 'bytecode'}",
			"-c",
			entry,
			"--config-file",
			str(selection.policy_root / "quality_gate/policy/mypy.ini"),
			"--show-column-numbers",
			"--show-error-codes",
			"--no-pretty",
			"--no-error-summary",
			"--cache-dir",
			str(workspace / "mypy-cache"),
			path,
		],
		root=snapshot,
		environment=process_environment(workspace),
		deadline=deadline,
	)
	if stderr or code not in {0, 1}:
		raise EditError("tool_output")
	allowed = {record["path"] for record in inventory_records}
	diagnostics = []
	for line in output.decode("utf-8").splitlines():
		match = re.fullmatch(r"(.+?):(\d+):(\d+): (error|note): (.*?)(?:  \[([a-z0-9-]+)\])?", line)
		if match is None:
			raise EditError("tool_output")
		if match[4] == "note":
			continue
		relative = Path(match[1]).as_posix()
		if relative not in allowed or match[6] is None or min(int(match[2]), int(match[3])) < 1:
			raise EditError("tool_output")
		diagnostics.append(
			{"path": relative, "line": int(match[2]), "column": int(match[3]), "code": match[6]}
		)
		if len(diagnostics) > MAX_FINDINGS:
			raise EditError("limits")
	if (code == 1) != bool(diagnostics):
		raise EditError("tool_output")
	return sorted(
		diagnostics,
		key=lambda diagnostic: (
			diagnostic["path"],
			diagnostic["line"],
			diagnostic["column"],
			diagnostic["code"],
		),
	)
