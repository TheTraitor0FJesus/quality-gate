from __future__ import annotations

import hashlib
import os
from collections.abc import Callable
from pathlib import Path

import pytest

import quality_gate.distribution as distribution
from quality_gate.distribution import DistributionError, verify_release
from quality_gate.release_contract import (
	UNIFIED_RELEASE_DEPENDENCIES,
	UNIFIED_RELEASE_POLICY_FILES,
	UNIFIED_RELEASE_TOOLS,
	ReleaseInventoryError,
	validate_release_inventory,
)


def _files() -> list[tuple[str, str]]:
	return [
		("quality_gate-2.0.5-py3-none-any.whl", "artifact"),
		*(
			(f"{name}-{version}-py3-none-any.whl", "dependency")
			for name, version in UNIFIED_RELEASE_DEPENDENCIES.items()
		),
		*((path, "policy") for path in UNIFIED_RELEASE_POLICY_FILES),
	]


def _tools(platform: str = "linux") -> list[tuple[str, str, str]]:
	platform_paths = {
		"linux": {"gitleaks": "gitleaks", "biome": "biome-linux-x64"},
		"windows": {"gitleaks": "gitleaks.exe", "biome": "biome-win32-x64.exe"},
	}[platform]
	return [
		*(
			(name, version, f"{name}-{version}-py3-none-any.whl")
			for name, version in UNIFIED_RELEASE_TOOLS.items()
			if name not in {"gitleaks", "biome"}
		),
		("gitleaks", UNIFIED_RELEASE_TOOLS["gitleaks"], platform_paths["gitleaks"]),
		("biome", UNIFIED_RELEASE_TOOLS["biome"], platform_paths["biome"]),
	]


def _actual_paths(platform: str) -> list[str]:
	return [
		"release.toml",
		*(path for path, _kind in _files()),
		*(path for _name, _version, path in _tools(platform)),
	]


def _write_complete_release(root: Path, platform: str) -> None:
	file_entries = _files()
	tool_entries = _tools(platform)
	manifest = ["[release]", 'version = "v2.0.5"', ""]
	for path, kind in file_entries:
		content = f"payload:{path}".encode()
		file_path = root / path
		file_path.parent.mkdir(parents=True, exist_ok=True)
		file_path.write_bytes(content)
		manifest.extend(
			[
				"[[release.files]]",
				f'path = "{path}"',
				f'sha256 = "{hashlib.sha256(content).hexdigest()}"',
				f'kind = "{kind}"',
				"",
			]
		)
	for name, version, path in tool_entries:
		content = f"tool:{name}".encode()
		tool_path = root / path
		tool_path.parent.mkdir(parents=True, exist_ok=True)
		tool_path.write_bytes(content)
		manifest.extend(
			[
				"[[release.tools]]",
				f'name = "{name}"',
				f'version = "{version}"',
				f'path = "{path}"',
				f'sha256 = "{hashlib.sha256(content).hexdigest()}"',
				"",
			]
		)
	(root / "release.toml").write_text("\n".join(manifest), encoding="utf-8")


@pytest.mark.parametrize("platform", ["linux", "windows"])
def test_unified_release_inventory_is_exact_for_each_platform(platform: str) -> None:
	validate_release_inventory(
		"v2.0.5",
		_files(),
		_tools(platform),
		platform=platform,
		actual_paths=_actual_paths(platform),
	)


def test_unified_release_inventory_rejects_an_unexpected_actual_path() -> None:
	with pytest.raises(ReleaseInventoryError, match="path inventory mismatch"):
		validate_release_inventory(
			"v2.0.5",
			_files(),
			_tools("linux"),
			platform="linux",
			actual_paths=[*_actual_paths("linux"), "unexpected.txt"],
		)


@pytest.mark.parametrize(
	("mutate", "message"),
	[
		(lambda files, _tools: files.pop(), "inventory mismatch"),
		(
			lambda _files, tools: tools.__setitem__(0, ("deptry", "0.25.0", tools[0][2])),
			"version mismatch",
		),
		(
			lambda _files, tools: tools.__setitem__(
				-1, ("biome", UNIFIED_RELEASE_TOOLS["biome"], "wrong-biome")
			),
			"path mismatch",
		),
	],
)
def test_unified_release_inventory_rejects_missing_or_mismatched_entries(
	mutate: Callable[[list[tuple[str, str]], list[tuple[str, str, str]]], object],
	message: str,
) -> None:
	files = _files()
	tools = _tools()
	mutate(files, tools)

	with pytest.raises(ReleaseInventoryError, match=message):
		validate_release_inventory("v2.0.5", files, tools, platform="linux")


def test_distribution_rejects_an_incomplete_unified_release(tmp_path: Path) -> None:
	wheel = tmp_path / "quality_gate-2.0.5-py3-none-any.whl"
	wheel.write_bytes(b"wheel")
	digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
	(tmp_path / "release.toml").write_text(
		f'''[release]
version = "v2.0.5"

[[release.files]]
path = "{wheel.name}"
sha256 = "{digest}"
''',
		encoding="utf-8",
	)

	with pytest.raises(DistributionError, match="inventory mismatch"):
		verify_release(tmp_path)


def test_distribution_rejects_an_unexpected_unified_release_file(tmp_path: Path) -> None:
	platform = "windows" if os.name == "nt" else "linux"
	_write_complete_release(tmp_path, platform)
	(tmp_path / "unexpected.txt").write_bytes(b"unexpected")

	with pytest.raises(DistributionError, match="path inventory mismatch"):
		verify_release(tmp_path)


@pytest.mark.parametrize(
	("host_name", "artifact_platform"), [("nt", "linux"), ("posix", "windows")]
)
def test_distribution_rejects_a_release_for_the_wrong_platform(
	monkeypatch: pytest.MonkeyPatch,
	tmp_path: Path,
	host_name: str,
	artifact_platform: str,
) -> None:
	monkeypatch.setattr(distribution.os, "name", host_name)
	_write_complete_release(tmp_path, artifact_platform)

	with pytest.raises(DistributionError, match="path mismatch"):
		verify_release(tmp_path)
