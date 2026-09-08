"""Canonical inventory contract for the unified policy release."""

from __future__ import annotations

from collections.abc import Sequence

UNIFIED_RELEASE = "v2.0.5"
UNIFIED_RELEASE_PACKAGE_VERSION = "2.0.5"
WHEEL_IDENTITY_PARTS = 3
UNIFIED_RELEASE_TOOLS = {
	"deptry": "0.25.1",
	"mypy": "2.3.1",
	"pytest": "9.1.1",
	"ruff": "0.16.4",
	"gitleaks": "8.30.1",
	"biome": "2.2.6",
}
UNIFIED_RELEASE_DEPENDENCIES = {
	"ast_serialize": "0.10.0",
	"click": "8.5.0",
	"colorama": "0.4.6",
	"iniconfig": "2.3.0",
	"librt": "0.15.0",
	"mypy_extensions": "1.1.0",
	"packaging": "26.3",
	"pathspec": "1.1.1",
	"pluggy": "1.6.0",
	"pygments": "2.21.0",
	"requirements_parser": "0.13.1",
	"tomli": "2.4.1",
	"typing_extensions": "4.16.0",
}
UNIFIED_RELEASE_POLICY_FILES = frozenset(
	{
		"quality_gate/policy/biome.json",
		"quality_gate/policy/biome.toml",
		"quality_gate/policy/mypy.ini",
		"quality_gate/policy/ruff.toml",
	}
)
UNIFIED_RELEASE_PLATFORM_TOOL_PATHS = {
	"linux": {"gitleaks": "gitleaks", "biome": "biome-linux-x64"},
	"windows": {"gitleaks": "gitleaks.exe", "biome": "biome-win32-x64.exe"},
}


class ReleaseInventoryError(ValueError):
	"""A unified release does not contain the exact required inventory."""


def _wheel_identity(path: str) -> tuple[str, str] | None:
	name = path.rsplit("/", 1)[-1]
	if not name.endswith(".whl"):
		return None
	parts = name[:-4].split("-", 2)
	if len(parts) != WHEEL_IDENTITY_PARTS:
		return None
	return parts[0], parts[1]


def _platform_tool_paths(platform: str | None) -> dict[str, str]:
	if platform is None:
		return {}
	try:
		return UNIFIED_RELEASE_PLATFORM_TOOL_PATHS[platform]
	except KeyError as error:
		raise ReleaseInventoryError(f"unsupported release platform: {platform}") from error


def _validate_identity_set(
	actual: set[tuple[str, str]], expected: set[tuple[str, str]], label: str
) -> None:
	if actual == expected:
		return
	missing = expected - actual
	extra = actual - expected
	details = []
	if missing:
		details.append(f"missing {sorted(missing)}")
	if extra:
		details.append(f"unexpected {sorted(extra)}")
	raise ReleaseInventoryError(f"release {label} mismatch: " + "; ".join(details))


def _file_identity(path: str, kind: str) -> tuple[str, str]:
	if path in UNIFIED_RELEASE_POLICY_FILES:
		if kind != "policy":
			raise ReleaseInventoryError(f"release policy file has the wrong kind: {path}")
		return "policy", path
	wheel = _wheel_identity(path)
	if wheel == ("quality_gate", UNIFIED_RELEASE_PACKAGE_VERSION):
		if kind != "artifact":
			raise ReleaseInventoryError(f"release policy wheel has the wrong kind: {path}")
		return "wheel", path
	if wheel is not None and wheel[0] in UNIFIED_RELEASE_DEPENDENCIES:
		expected_version = UNIFIED_RELEASE_DEPENDENCIES[wheel[0]]
		if wheel[1] != expected_version:
			raise ReleaseInventoryError(
				f"release dependency version mismatch: {wheel[0]} {wheel[1]}"
			)
		if kind != "dependency":
			raise ReleaseInventoryError(f"release dependency has the wrong kind: {path}")
		return "dependency", wheel[0]
	raise ReleaseInventoryError(f"release contains an unexpected file: {path}")


def _validate_files(files: Sequence[tuple[str, str]]) -> None:
	seen: set[tuple[str, str]] = set()
	policy_wheels: list[str] = []
	for path, kind in files:
		identity = _file_identity(path, kind)
		if identity[0] == "wheel":
			policy_wheels.append(path)
		if identity in seen:
			raise ReleaseInventoryError(f"release contains duplicate inventory entry: {path}")
		seen.add(identity)
	if len(policy_wheels) != 1:
		raise ReleaseInventoryError("release must contain exactly one unified policy wheel")
	expected = {
		*(("policy", path) for path in UNIFIED_RELEASE_POLICY_FILES),
		*(("dependency", name) for name in UNIFIED_RELEASE_DEPENDENCIES),
		("wheel", policy_wheels[0]),
	}
	_validate_identity_set(seen, expected, "file inventory")


def _validate_tool_path(name: str, version: str, path: str, platform: str | None) -> None:
	if name in {"deptry", "mypy", "pytest", "ruff"}:
		if _wheel_identity(path) != (name, version):
			raise ReleaseInventoryError(f"release tool path mismatch: {name}")
		return
	platform_paths = _platform_tool_paths(platform)
	if platform_paths:
		if platform_paths[name] != path:
			raise ReleaseInventoryError(f"release tool path mismatch: {name}")
		return
	all_platform_paths = {
		*UNIFIED_RELEASE_PLATFORM_TOOL_PATHS["linux"].values(),
		*UNIFIED_RELEASE_PLATFORM_TOOL_PATHS["windows"].values(),
	}
	if path not in all_platform_paths:
		raise ReleaseInventoryError(f"release tool path mismatch: {name}")


def _validate_tools(tools: Sequence[tuple[str, str, str]], platform: str | None) -> None:
	seen: set[str] = set()
	for name, version, path in tools:
		if name not in UNIFIED_RELEASE_TOOLS:
			raise ReleaseInventoryError(f"release contains an unexpected tool: {name}")
		if name in seen:
			raise ReleaseInventoryError(f"release contains duplicate tool: {name}")
		expected_version = UNIFIED_RELEASE_TOOLS[name]
		if version != expected_version:
			raise ReleaseInventoryError(f"release tool version mismatch: {name} {version}")
		_validate_tool_path(name, version, path, platform)
		seen.add(name)
	missing = set(UNIFIED_RELEASE_TOOLS) - seen
	if missing:
		raise ReleaseInventoryError(f"release tool inventory is incomplete: {sorted(missing)}")


def _validate_paths(
	files: Sequence[tuple[str, str]],
	tools: Sequence[tuple[str, str, str]],
	actual_paths: Sequence[str],
) -> None:
	declared_paths = [path for path, _kind in files]
	declared_paths.extend(path for _name, _version, path in tools)
	if len(declared_paths) != len(set(declared_paths)):
		raise ReleaseInventoryError("release contains duplicate file paths")
	actual = set(actual_paths)
	if len(actual) != len(actual_paths):
		raise ReleaseInventoryError("release archive contains duplicate file paths")
	expected = {"release.toml", *declared_paths}
	if actual == expected:
		return
	missing = expected - actual
	extra = actual - expected
	details = []
	if missing:
		details.append(f"missing {sorted(missing)}")
	if extra:
		details.append(f"unexpected {sorted(extra)}")
	raise ReleaseInventoryError("release path inventory mismatch: " + "; ".join(details))


def validate_release_inventory(
	version: str,
	files: Sequence[tuple[str, str]],
	tools: Sequence[tuple[str, str, str]],
	*,
	platform: str | None = None,
	actual_paths: Sequence[str] | None = None,
) -> None:
	"""Require the exact cross-platform inventory for the unified release."""
	if version != UNIFIED_RELEASE:
		return
	_validate_files(files)
	_validate_tools(tools, platform)
	if actual_paths is not None:
		_validate_paths(files, tools, actual_paths)
