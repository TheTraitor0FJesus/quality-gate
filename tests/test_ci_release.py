"""Executable contract tests for immutable CI release verification."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import zipfile
from collections.abc import Callable
from pathlib import Path

import pytest

from quality_gate import ci_release, runner

RELEASE = "v2.0.0"
ASSET = f"quality-gate-{RELEASE}.zip"
REPOSITORY = "TheTraitor0FJesus/quality-gate"
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _build_wheel(tmp_path: Path) -> tuple[str, bytes]:
	destination = tmp_path / "wheel"
	destination.mkdir()
	result = subprocess.run(
		[
			sys.executable,
			"-m",
			"pip",
			"wheel",
			"--no-deps",
			"--no-build-isolation",
			str(PROJECT_ROOT),
			"--wheel-dir",
			str(destination),
		],
		capture_output=True,
		text=True,
		check=False,
	)
	assert result.returncode == 0, result.stderr
	wheel_path = next(destination.glob("quality_gate-*.whl"))
	return wheel_path.name, wheel_path.read_bytes()


def _archive(
	path: Path,
	wheel_name: str,
	wheel: bytes,
	*,
	manifest_version: str = RELEASE,
	tool_path: str = "gitleaks.exe",
	unsafe_member: bool = False,
	invalid_file_entry: bool = False,
) -> None:
	tool = b"scanner"
	file_inventory = (
		'files = ["invalid"]'
		if invalid_file_entry
		else f'''[[release.files]]
path = "{wheel_name}"
sha256 = "{hashlib.sha256(wheel).hexdigest()}"'''
	)
	release_manifest = f'''[release]
version = "{manifest_version}"

{file_inventory}

[[release.tools]]
name = "gitleaks"
version = "8.30.1"
path = "{tool_path}"
sha256 = "{hashlib.sha256(tool).hexdigest()}"
'''
	with zipfile.ZipFile(path, "w") as archive:
		archive.writestr("release.toml", release_manifest)
		archive.writestr(wheel_name, wheel)
		archive.writestr("gitleaks.exe", tool)
		if unsafe_member:
			archive.writestr("../escaped.txt", b"unsafe")


def _metadata(archive: Path) -> dict[str, object]:
	return {
		"tag_name": RELEASE,
		"immutable": True,
		"assets": [
			{
				"name": ASSET,
				"browser_download_url": (
					f"https://github.com/{REPOSITORY}/releases/download/{RELEASE}/{ASSET}"
				),
				"digest": f"sha256:{hashlib.sha256(archive.read_bytes()).hexdigest()}",
			}
		],
	}


def _write_metadata(path: Path, value: dict[str, object]) -> None:
	path.write_text(json.dumps(value), encoding="utf-8")


def test_ci_release_verifier_extracts_only_a_trusted_immutable_asset(tmp_path: Path) -> None:
	"""Verify a matching GitHub digest authorizes extraction of the release."""

	archive = tmp_path / ASSET
	metadata = tmp_path / "release.json"
	target = tmp_path / "verified"
	wheel_name, wheel_content = _build_wheel(tmp_path)
	_archive(archive, wheel_name, wheel_content)
	_write_metadata(metadata, _metadata(archive))

	wheel = ci_release.verify_release_asset(
		metadata,
		archive,
		target,
		expected_release=RELEASE,
		expected_name=ASSET,
	)

	assert wheel == target / wheel_name
	assert wheel.read_bytes() == wheel_content
	assert (target / "gitleaks.exe").read_bytes() == b"scanner"


def _mutable(value: dict[str, object]) -> None:
	value["immutable"] = False


def _missing_digest(value: dict[str, object]) -> None:
	assets = value["assets"]
	assert isinstance(assets, list)
	asset = assets[0]
	assert isinstance(asset, dict)
	asset.pop("digest")


def _mismatched_digest(value: dict[str, object]) -> None:
	assets = value["assets"]
	assert isinstance(assets, list)
	asset = assets[0]
	assert isinstance(asset, dict)
	asset["digest"] = f"sha256:{'0' * 64}"


@pytest.mark.parametrize("mutate", [_mutable, _missing_digest, _mismatched_digest])
def test_ci_release_verifier_rejects_untrusted_metadata_before_extraction(
	mutate: Callable[[dict[str, object]], None],
	tmp_path: Path,
	capsys: pytest.CaptureFixture[str],
) -> None:
	"""Verify mutable or mismatched GitHub metadata fails closed before extraction."""

	archive = tmp_path / ASSET
	metadata = tmp_path / "release.json"
	target = tmp_path / "untrusted"
	wheel_name, wheel_content = _build_wheel(tmp_path)
	_archive(archive, wheel_name, wheel_content)
	value = _metadata(archive)
	mutate(value)
	_write_metadata(metadata, value)

	result = ci_release.main(
		[
			"--metadata",
			str(metadata),
			"--archive",
			str(archive),
			"--target",
			str(target),
			"--release",
			RELEASE,
			"--asset-name",
			ASSET,
		]
	)

	assert result == runner.EXIT_UNCHECKED
	assert "unchecked" in capsys.readouterr().err
	assert not target.exists()


def test_ci_release_verifier_rejects_manifest_version_before_extraction(tmp_path: Path) -> None:
	archive = tmp_path / ASSET
	metadata = tmp_path / "release.json"
	target = tmp_path / "wrong-version"
	wheel_name, wheel_content = _build_wheel(tmp_path)
	_archive(archive, wheel_name, wheel_content, manifest_version="v9.9.9")
	_write_metadata(metadata, _metadata(archive))

	result = ci_release.main(
		[
			"--metadata",
			str(metadata),
			"--archive",
			str(archive),
			"--target",
			str(target),
			"--release",
			RELEASE,
			"--asset-name",
			ASSET,
		]
	)

	assert result == runner.EXIT_UNCHECKED
	assert not target.exists()


def test_ci_release_verifier_rejects_unsafe_archive_member_before_extraction(
	tmp_path: Path,
) -> None:
	archive = tmp_path / ASSET
	metadata = tmp_path / "release.json"
	target = tmp_path / "unsafe-member"
	wheel_name, wheel_content = _build_wheel(tmp_path)
	_archive(archive, wheel_name, wheel_content, unsafe_member=True)
	_write_metadata(metadata, _metadata(archive))

	result = ci_release.main(
		[
			"--metadata",
			str(metadata),
			"--archive",
			str(archive),
			"--target",
			str(target),
			"--release",
			RELEASE,
			"--asset-name",
			ASSET,
		]
	)

	assert result == runner.EXIT_UNCHECKED
	assert not target.exists()


def test_ci_release_verifier_rejects_unsafe_tool_path_without_chmod_outside_target(
	tmp_path: Path,
) -> None:
	archive = tmp_path / ASSET
	metadata = tmp_path / "release.json"
	target = tmp_path / "unsafe-tool"
	external = tmp_path / "external-tool"
	external.write_bytes(b"must not be changed")
	initial_mode = external.stat().st_mode
	wheel_name, wheel_content = _build_wheel(tmp_path)
	_archive(archive, wheel_name, wheel_content, tool_path="../external-tool")
	_write_metadata(metadata, _metadata(archive))

	result = ci_release.main(
		[
			"--metadata",
			str(metadata),
			"--archive",
			str(archive),
			"--target",
			str(target),
			"--release",
			RELEASE,
			"--asset-name",
			ASSET,
		]
	)

	assert result == runner.EXIT_UNCHECKED
	assert external.stat().st_mode == initial_mode


def test_ci_release_verifier_rejects_invalid_manifest_entries_before_extraction(
	tmp_path: Path,
) -> None:
	archive = tmp_path / ASSET
	metadata = tmp_path / "release.json"
	target = tmp_path / "invalid-manifest"
	wheel_name, wheel_content = _build_wheel(tmp_path)
	_archive(archive, wheel_name, wheel_content, invalid_file_entry=True)
	_write_metadata(metadata, _metadata(archive))

	result = ci_release.main(
		[
			"--metadata",
			str(metadata),
			"--archive",
			str(archive),
			"--target",
			str(target),
			"--release",
			RELEASE,
			"--asset-name",
			ASSET,
		]
	)

	assert result == runner.EXIT_UNCHECKED
	assert not target.exists()
