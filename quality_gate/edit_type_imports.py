"""Typing import closure and affected-source selection."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

from .contracts import PythonComponent
from .edit_inputs import SourceFile
from .edit_protocol import EditError
from .edit_type_environment import TypingEnvironment

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


def _affects(
	path: str,
	component: str,
	dependencies: tuple[str, ...],
	source_roots: tuple[str, ...] = (),
) -> bool:
	changed_path = Path(path)
	managed_roots = (Path(component), *(Path(root) for root in source_roots))
	package_initializer = changed_path.name in {"__init__.py", "__init__.pyi"} and any(
		root.is_relative_to(changed_path.parent) for root in managed_roots
	)
	typed_marker = changed_path.name == "py.typed" and any(
		changed_path.is_relative_to(root) or root.is_relative_to(changed_path.parent)
		for root in managed_roots
	)
	return (
		path in dependencies
		or changed_path.name in CONFIGURATION_NAMES
		or package_initializer
		or typed_marker
		or (
			changed_path.suffix in {".py", ".pyi"}
			and (
				changed_path.is_relative_to(component)
				or any(changed_path.is_relative_to(root) for root in source_roots)
			)
		)
	)


def _import_context(
	sources: list[SourceFile],
	scope: str,
	selection: TypingEnvironment,
	stub_roots: tuple[str, ...] = (),
	source_roots: tuple[str, ...] = (),
	*,
	repository_root: Path | None = None,
) -> int:
	local_modules = _local_modules(sources, (scope, *source_roots))
	local_roots: tuple[str | Path, ...] = stub_roots
	if source_roots:
		if repository_root is None:
			raise EditError("configuration")
		local_roots = (*local_roots, *(repository_root / root for root in source_roots))
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
					and not any(_local_typed_dependency(Path(root), name) for root in local_roots)
				):
					raise EditError("dependencies")
	return count


def _local_modules(sources: list[SourceFile], scopes: tuple[str, ...]) -> set[str]:
	local_modules: set[str] = set()
	for source in sources:
		paths = [Path(source.path)]
		for scope in scopes:
			if Path(source.path).is_relative_to(scope):
				paths.append(Path(source.path).relative_to(scope))
		for path in paths:
			parts = list(path.with_suffix("").parts)
			if parts[-1] == "__init__":
				parts.pop()
			for length in range(1, len(parts) + 1):
				local_modules.add(".".join(parts[:length]))
	return local_modules


def _module_name(path: Path) -> str | None:
	if path.suffix not in {".py", ".pyi"}:
		return None
	parts = list(path.with_suffix("").parts)
	if parts[-1] == "__init__":
		parts.pop()
	return ".".join(parts) or None


def _source_module_targets(
	sources: list[SourceFile], scopes: tuple[str, ...], stub_roots: tuple[str, ...]
) -> tuple[str, ...]:
	local_modules = _local_modules(sources, scopes)
	imports = {
		name
		for source in sources
		for names in _source_imports(source, local_modules)
		for name in names
	}
	module_paths: dict[str, str] = {}
	for source in sources:
		path = Path(source.path)
		if any(path.is_relative_to(root) for root in map(Path, stub_roots)):
			continue
		candidates = {name for name in (_module_name(path),) if name is not None}
		for scope in scopes:
			root = Path(scope)
			if path.is_relative_to(root):
				name = _module_name(path.relative_to(root))
				if name is not None:
					candidates.add(name)
		matching = {
			candidate
			for candidate in candidates
			if any(name == candidate or name.startswith(f"{candidate}.") for name in imports)
		}
		module = next(iter(matching), _module_name(path)) if len(matching) <= 1 else None
		if module is None:
			raise EditError("dependencies")
		previous_path = module_paths.setdefault(module, source.path)
		if previous_path != source.path:
			previous = Path(previous_path)
			if previous.with_suffix("") != path.with_suffix("") or {
				previous.suffix,
				path.suffix,
			} != {".py", ".pyi"}:
				raise EditError("dependencies")
	return tuple(sorted(module_paths))


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
				imports.append([".".join(parts)])
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


def _local_typed_dependency(root: Path, name: str) -> bool:
	module = root.joinpath(*name.split("."))
	return (
		module.with_suffix(".pyi").is_file()
		or (module / "__init__.pyi").is_file()
		or (module / "py.typed").is_file()
	)


def _local_stub_roots(
	root: Path, component: PythonComponent | None, directories: list[dict[str, Any]]
) -> tuple[str, ...]:
	"""Resolve manifest stub roots inside the captured component tree."""
	if component is None:
		return ()
	component_path = Path(component.path)
	captured_directories = {record["path"] for record in directories}
	roots: list[str] = []
	for value in component.stub_roots:
		candidate = Path(value)
		if (
			candidate.is_absolute()
			or "\\" in value
			or ":" in value
			or any(part in {"", ".", ".."} for part in value.split("/"))
		):
			raise EditError("unsafe_path")
		relative = (component_path / candidate).as_posix()
		if not Path(relative).is_relative_to(component_path):
			raise EditError("unsafe_path")
		if relative not in captured_directories or not (root / relative).is_dir():
			raise EditError("dependencies")
		roots.append(relative)
	return tuple(sorted(set(roots)))
