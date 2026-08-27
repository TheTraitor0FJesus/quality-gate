"""Fail-closed verification for a Quality Gate GitHub release asset."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import tomllib
import zipfile
from collections.abc import Sequence
from pathlib import Path, PurePosixPath
from typing import Any

MAX_ARCHIVE_BYTES = 100 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 10_000
SHA256_DIGEST = re.compile(r"sha256:([0-9a-f]{64})")


class CiReleaseError(RuntimeError):
	"""Report a release that is unsafe to execute in CI."""


def _object(value: object, context: str) -> dict[str, Any]:
	if not isinstance(value, dict):
		raise CiReleaseError(f"{context} must be an object")
	return value


def _archive_digest(path: Path) -> str:
	digest = hashlib.sha256()
	with path.open("rb") as stream:
		for block in iter(lambda: stream.read(1024 * 1024), b""):
			digest.update(block)
	return digest.hexdigest()


def _trusted_asset_digest(
	metadata: dict[str, Any], expected_release: str, expected_name: str, repository: str
) -> str:
	if metadata.get("tag_name") != expected_release or metadata.get("immutable") is not True:
		raise CiReleaseError("Quality Gate release is not immutable")
	assets = metadata.get("assets")
	if not isinstance(assets, list):
		raise CiReleaseError("immutable release has no asset inventory")
	matches = [
		_object(asset, "release asset")
		for asset in assets
		if isinstance(asset, dict) and asset.get("name") == expected_name
	]
	expected_url = (
		f"https://github.com/{repository}/releases/download/{expected_release}/{expected_name}"
	)
	if len(matches) != 1 or matches[0].get("browser_download_url") != expected_url:
		raise CiReleaseError("immutable release has no unique expected archive")
	digest = matches[0].get("digest")
	match = SHA256_DIGEST.fullmatch(digest) if isinstance(digest, str) else None
	if match is None:
		raise CiReleaseError("immutable release archive has no SHA-256 digest")
	return match.group(1)


def _safe_relative_path(value: object, context: str) -> tuple[str, ...]:
	if not isinstance(value, str) or not value:
		raise CiReleaseError(f"{context} must be a safe relative POSIX path")
	path = PurePosixPath(value)
	if (
		"\\" in value
		or path.is_absolute()
		or any(part in {"", ".", ".."} for part in value.split("/"))
		or any(part in {"", ".", ".."} for part in path.parts)
	):
		raise CiReleaseError(f"{context} must be a safe relative POSIX path")
	return path.parts


def _target_path(target: Path, parts: tuple[str, ...], context: str) -> Path:
	target_root = target.resolve()
	resolved = target.joinpath(*parts).resolve()
	try:
		resolved.relative_to(target_root)
	except ValueError as error:
		raise CiReleaseError(f"{context} must stay inside the extraction root") from error
	return resolved


def _validate_members(archive: zipfile.ZipFile, target: Path) -> None:
	members = archive.infolist()
	if len(members) > MAX_ARCHIVE_ENTRIES:
		raise CiReleaseError("release archive exceeds the entry limit")
	total_size = 0
	for member in members:
		total_size += member.file_size
		if total_size > MAX_ARCHIVE_BYTES:
			raise CiReleaseError("release archive exceeds the size limit")
		member_name = member.filename.rstrip("/")
		if not member_name:
			continue
		parts = _safe_relative_path(member_name, "release archive member")
		_target_path(target, parts, "release archive member")


def _manifest_inventory(
	release: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
	files = release.get("files")
	if not isinstance(files, list):
		raise CiReleaseError("release manifest has no file inventory")
	if any(not isinstance(item, dict) for item in files):
		raise CiReleaseError("release manifest contains an invalid file entry")
	wheels = [
		_object(item, "release file")
		for item in files
		if str(item.get("path", "")).endswith(".whl") and item.get("kind", "artifact") == "artifact"
	]
	if len(wheels) != 1:
		raise CiReleaseError("release must contain exactly one policy wheel")
	tools = release.get("tools", [])
	if not isinstance(tools, list):
		raise CiReleaseError("release manifest has no tool inventory")
	if any(not isinstance(item, dict) for item in tools):
		raise CiReleaseError("release manifest contains an invalid tool entry")
	wheel_parts = _safe_relative_path(wheels[0].get("path"), "release wheel path")
	return files, [_object(item, "release tool") for item in tools], "/".join(wheel_parts)


def _extract_verified_archive(
	archive_path: Path, target: Path, expected_release: str
) -> tuple[dict[str, Any], str]:
	try:
		with zipfile.ZipFile(archive_path) as archive:
			_validate_members(archive, target)
			release = _object(
				tomllib.loads(archive.read("release.toml").decode())["release"],
				"release manifest",
			)
			if release.get("version") != expected_release:
				raise CiReleaseError("release manifest version does not match the immutable tag")
			declared_files, declared_tools, wheel_path = _manifest_inventory(release)
			declared_paths: set[str] = set()
			for item in (*declared_files, *declared_tools):
				parts = _safe_relative_path(item.get("path"), "release file path")
				path = "/".join(parts)
				if path in declared_paths:
					raise CiReleaseError("release contains duplicate file paths")
				declared_paths.add(path)
				try:
					content = archive.read(path)
				except KeyError as error:
					raise CiReleaseError(f"release file is missing: {path}") from error
				checksum = item.get("sha256")
				if not isinstance(checksum, str) or not re.fullmatch(r"[0-9a-f]{64}", checksum):
					raise CiReleaseError(f"release file has an invalid SHA-256 digest: {path}")
				if hashlib.sha256(content).hexdigest() != checksum:
					message = (
						"release wheel checksum mismatch"
						if path == wheel_path
						else f"release file checksum mismatch: {path}"
					)
					raise CiReleaseError(message)
			archive.extractall(target)
	except (KeyError, OSError, UnicodeError, tomllib.TOMLDecodeError, zipfile.BadZipFile) as error:
		raise CiReleaseError("release archive is unreadable") from error
	return release, wheel_path


def _prepare_tools(release: dict[str, Any], target: Path) -> None:
	tools = release.get("tools")
	if not isinstance(tools, list):
		raise CiReleaseError("release manifest has no tool inventory")
	for tool in tools:
		tool_data = _object(tool, "release tool")
		parts = _safe_relative_path(tool_data.get("path"), "release tool path")
		tool_path = _target_path(target, parts, "release tool path")
		if not tool_path.is_file() or tool_path.is_symlink():
			raise CiReleaseError("release tool is not an extracted regular file")
		try:
			tool_path.chmod(tool_path.stat().st_mode | 0o111)
		except OSError as error:
			raise CiReleaseError("release tool is unavailable after extraction") from error


def verify_release_asset(
	metadata_path: Path,
	archive_path: Path,
	target: Path,
	*,
	expected_release: str,
	expected_name: str,
	repository: str = "TheTraitor0FJesus/quality-gate",
) -> Path:
	"""Verify and extract one immutable release asset, then return its wheel path."""

	try:
		metadata = _object(json.loads(metadata_path.read_text(encoding="utf-8")), "release")
	except (OSError, UnicodeError, json.JSONDecodeError) as error:
		raise CiReleaseError("release metadata is unreadable") from error
	expected_digest = _trusted_asset_digest(metadata, expected_release, expected_name, repository)
	try:
		actual_digest = _archive_digest(archive_path)
	except OSError as error:
		raise CiReleaseError("release archive is unreadable") from error
	if actual_digest != expected_digest:
		raise CiReleaseError("immutable release archive digest mismatch")
	release, wheel_path = _extract_verified_archive(archive_path, target, expected_release)
	_prepare_tools(release, target)
	return target / wheel_path


def _parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--metadata", type=Path, required=True)
	parser.add_argument("--archive", type=Path, required=True)
	parser.add_argument("--target", type=Path, required=True)
	parser.add_argument("--release", required=True)
	parser.add_argument("--asset-name", required=True)
	return parser


def main(arguments: Sequence[str] | None = None) -> int:
	"""Verify the requested CI release and print the trusted wheel path."""

	options = _parser().parse_args(arguments)
	try:
		wheel = verify_release_asset(
			options.metadata,
			options.archive,
			options.target,
			expected_release=options.release,
			expected_name=options.asset_name,
		)
	except CiReleaseError as error:
		sys.stderr.write(f"quality-gate release: unchecked - {error}\n")
		return 2
	sys.stdout.write(f"{wheel}\n")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
