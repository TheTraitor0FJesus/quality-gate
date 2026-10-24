"""Offline exact mypy selection and isolated declared-environment inspection."""

from __future__ import annotations

import base64
import csv
import io
import json
import os
import zipfile
from dataclasses import dataclass
from importlib.machinery import PathFinder
from pathlib import Path
from typing import Any

from .contracts import Manifest, PythonComponent
from .distribution import PolicyCache, ReleaseManifest
from .edit_inputs import Configuration, read_bounded
from .edit_policy import file_sha256, select_release, sha256
from .edit_process import run_process
from .edit_protocol import EditError
from .edit_reuse import identity_digest
from .edit_type_inputs import MAX_ENVIRONMENT_BYTES, assert_deadline, environment_tree
from .runtime import RuntimeManager, runtime_fingerprint, runtime_identity

PROBE = (
	"import json,sys; print(json.dumps({'version':"
	"f'{sys.version_info.major}.{sys.version_info.minor}',"
	"'executable':sys.executable,'base_executable':sys._base_executable,"
	"'stdlib':sorted(sys.stdlib_module_names)}))"
)
# Runtime requirements of the only supported tool, mypy 2.3.1, on the released
# CPython 3.12 platform. Pytest/deptry dependencies are not typing prerequisites.
MYPY_DEPENDENCIES = frozenset(
	{"ast_serialize", "librt", "mypy_extensions", "pathspec", "typing_extensions"}
)
RECORD_FIELDS = 3


def process_environment(workspace: Path) -> dict[str, str]:
	allowed = {"PATH", "SYSTEMROOT", "WINDIR", "PATHEXT", "LANG", "LC_ALL"}
	return {key: value for key, value in os.environ.items() if key.upper() in allowed} | {
		"PYTHONDONTWRITEBYTECODE": "1",
		"PYTHONIOENCODING": "utf-8",
		"PYTHONUTF8": "1",
		"HOME": str(workspace),
		"USERPROFILE": str(workspace),
		"TEMP": str(workspace),
		"TMP": str(workspace),
	}


@dataclass
class TypingEnvironment:
	python: Path
	base_python: Path
	site: Path
	policy_root: Path
	release: ReleaseManifest
	provenance: dict[str, Any]
	stdlib: set[str]
	tool_imports: tuple[str, ...]


def select_environment(
	root: Path,
	cache: PolicyCache,
	manifest: Manifest | None,
	component: PythonComponent | None,
	baseline: dict[str, Any] | None,
	configuration: Configuration,
	deadline: float,
	workspace: Path,
) -> TypingEnvironment:
	if manifest is not None and component is not None:
		version = manifest.policy_release
		for dependency in component.dependency_inputs:
			configuration.capture(dependency, required=True)
		identity = runtime_identity(root, manifest, component)
		manager = RuntimeManager(cache)
		inspection = manager.inspect(root, identity)
		if not inspection.current or inspection.python is None:
			raise EditError("runtime")
		python = inspection.python
		runtime_root = inspection.path
		fingerprint = runtime_fingerprint(identity)
		expected_version = component.python_version
	else:
		if baseline is None:
			raise EditError("invalid_request")
		version = baseline["policy_release"]
		runtime_root = root / ".venv"
		python = runtime_root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
		fingerprint = identity_digest({"root": str(root), "baseline": baseline})
		expected_version = None
	policy_root, release = select_release(cache, version)
	tool = next((tool for tool in release.tools if tool.name == "mypy"), None)
	if tool is None or tool.version != "2.3.1":
		raise EditError("tool")
	python_version, base_python, stdlib, site = _inspect_runtime(
		runtime_root, python, expected_version, workspace, deadline
	)
	file_identities: dict[Path, tuple[str, int]] = {}
	environment = environment_tree(
		runtime_root, base_python, deadline, file_identities=file_identities
	)
	verified = _verify_installed_wheel(policy_root / tool.path, site, deadline, file_identities)
	for item in release.files:
		if item.kind == "dependency" and Path(item.path).name.split("-", 1)[0] in MYPY_DEPENDENCIES:
			verified.update(
				_verify_installed_wheel(policy_root / item.path, site, deadline, file_identities)
			)
	tool_imports = _verify_import_origins(site, verified, file_identities)
	return TypingEnvironment(
		python,
		base_python,
		site,
		policy_root,
		release,
		{
			"policy_release": version,
			"release_sha256": sha256((policy_root / "release.toml").read_bytes()),
			"policy_sha256": sha256((policy_root / "quality_gate/policy/mypy.ini").read_bytes()),
			"mypy_version": tool.version,
			"mypy_sha256": tool.sha256,
			"python": str(python),
			"python_version": python_version,
			"python_sha256": file_sha256(python.resolve(), MAX_ENVIRONMENT_BYTES),
			"base_python": str(base_python),
			"base_python_sha256": file_sha256(base_python, MAX_ENVIRONMENT_BYTES),
			"runtime_fingerprint": fingerprint,
			"environment_sha256": environment["sha256"],
			"environment": environment,
		},
		stdlib,
		tool_imports,
	)


def _inspect_runtime(
	runtime_root: Path,
	python: Path,
	expected_version: str | None,
	workspace: Path,
	deadline: float,
) -> tuple[str, Path, set[str], Path]:
	if not runtime_root.is_dir() or runtime_root.resolve() != runtime_root or not python.is_file():
		raise EditError("runtime")
	try:
		venv = read_bounded(runtime_root / "pyvenv.cfg", 64 * 1024).decode("utf-8")
	except (OSError, UnicodeError) as error:
		raise EditError("runtime") from error
	settings = {
		key.strip().lower(): value.strip().lower()
		for line in venv.splitlines()
		if "=" in line
		for key, value in [line.split("=", 1)]
	}
	if settings.get("include-system-site-packages") != "false":
		raise EditError("runtime")
	code, output, stderr = run_process(
		[str(python), "-I", "-S", "-B", "-c", PROBE],
		root=workspace,
		environment=process_environment(workspace),
		deadline=deadline,
	)
	try:
		probe = json.loads(output)
		if code or stderr or Path(probe["executable"]).resolve() != python.resolve():
			raise EditError("runtime")
		python_version = probe["version"]
		if expected_version is not None and python_version != expected_version:
			raise EditError("runtime")
		base_python = Path(probe["base_executable"]).resolve(strict=True)
		stdlib = set(probe["stdlib"])
	except (ValueError, TypeError, KeyError) as error:
		raise EditError("runtime") from error
	site = runtime_root / (
		"Lib/site-packages" if os.name == "nt" else f"lib/python{python_version}/site-packages"
	)
	if not site.is_dir() or site.is_symlink():
		raise EditError("dependencies")
	_validate_site(site)
	return python_version, base_python, stdlib, site


def _validate_site(site: Path) -> None:
	for path in site.iterdir():
		if path.suffix == ".egg-link":
			raise EditError("dependencies")
		if path.suffix == ".pth" and any(
			line.strip() and not line.lstrip().startswith(b"#")
			for line in read_bounded(path, 256 * 1024).splitlines()
		):
			raise EditError("dependencies")
		if path.name.endswith(".dist-info") and (path / "direct_url.json").exists():
			metadata = json.loads(read_bounded(path / "direct_url.json", 256 * 1024))
			if metadata.get("dir_info", {}).get("editable"):
				raise EditError("dependencies")


def _verify_installed_wheel(
	wheel: Path, site: Path, deadline: float, file_identities: dict[Path, tuple[str, int]]
) -> set[Path]:
	"""Bind executable package bytes to the integrity-verified release RECORD."""
	verified: set[Path] = set()
	with zipfile.ZipFile(wheel) as archive:
		records = [name for name in archive.namelist() if name.endswith(".dist-info/RECORD")]
		if len(records) != 1 or archive.getinfo(records[0]).file_size > 1024 * 1024:
			raise EditError("tool")
		for row in csv.reader(io.StringIO(archive.read(records[0]).decode("utf-8"))):
			assert_deadline(deadline)
			if len(row) != RECORD_FIELDS or not row[1]:
				continue
			parts = row[0].split("/")
			if any(part in {"", ".", ".."} for part in parts) or not row[1].startswith("sha256="):
				raise EditError("tool")
			path = site.joinpath(*parts)
			expected = base64.urlsafe_b64decode(row[1][7:] + "=").hex()
			# Reuse bytes just hashed for the environment certificate, then rehash
			# the complete environment before returning. RECORD stays authoritative.
			if file_identities.get(path) != (expected, int(row[2])):
				raise EditError("tool")
			verified.add(path)
	return verified


def _verify_import_origins(
	site: Path, verified: set[Path], file_identities: dict[Path, tuple[str, int]]
) -> tuple[str, ...]:
	"""Resolve tool modules without executing packages or accepting shadow copies."""
	names = sorted(
		{
			path.relative_to(site).parts[0].split(".")[0]
			for path in verified
			if not path.relative_to(site).parts[0].endswith((".dist-info", ".data"))
		}
	)
	for name in names:
		spec = PathFinder.find_spec(name, [str(site)])
		if spec is None:
			raise EditError("tool")
		if spec.origin is None:
			if [Path(path) for path in spec.submodule_search_locations or []] != [site / name]:
				raise EditError("tool")
		elif Path(spec.origin) not in verified:
			raise EditError("tool")
		package = site / name
		if package.is_dir() and any(
			path.is_relative_to(package) and path not in verified for path in file_identities
		):
			raise EditError("tool")
	return tuple(names)


def assert_environment_current(selection: TypingEnvironment, deadline: float) -> None:
	provenance = selection.provenance
	if (
		environment_tree(Path(provenance["environment"]["path"]), selection.base_python, deadline)
		!= provenance["environment"]
		or file_sha256(selection.python.resolve(), MAX_ENVIRONMENT_BYTES)
		!= provenance["python_sha256"]
	):
		raise EditError("stale_input")
	root, release = select_release(
		PolicyCache(selection.policy_root.parent.parent), provenance["policy_release"]
	)
	if (
		release != selection.release
		or sha256((root / "release.toml").read_bytes()) != provenance["release_sha256"]
	):
		raise EditError("stale_input")
