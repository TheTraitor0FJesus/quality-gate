"""Quality Gate's package-specific release adapter."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import zipfile
from collections.abc import Sequence
from pathlib import Path
from urllib.request import Request, urlopen

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(_REPOSITORY_ROOT) not in sys.path:
	sys.path.insert(0, str(_REPOSITORY_ROOT))

from quality_gate.distribution import DistributionError
from quality_gate.launcher import prepare_bootstrap
from quality_gate.release import ReleaseControllerError, verify_release_candidate
from quality_gate.release_contract import (
	UNIFIED_RELEASE_PLATFORM_TOOL_PATHS,
	UNIFIED_RELEASE_POLICY_FILES,
	UNIFIED_RELEASE_TOOLS,
	validate_release_inventory,
)
from quality_gate.runner import bootstrap_check as run_bootstrap_check
from quality_gate.runtime import RuntimeUnavailable

try:
	from .source_release import (
		ArtifactIdentity,
		ReleaseError,
		load_release_timeout,
		read_bounded,
		validate_source_sha,
		validate_version_projections,
	)
except ImportError:  # pragma: no cover - exercised by the workflow script entry point.
	from source_release import (
		ArtifactIdentity,
		ReleaseError,
		load_release_timeout,
		read_bounded,
		validate_source_sha,
		validate_version_projections,
	)


SHA256 = re.compile(r"^[0-9a-f]{64}$")
MAX_DOWNLOAD_BYTES = 512 * 1024 * 1024


def _root() -> Path:
	return Path(__file__).resolve().parents[1]


def _platform(value: str | None = None) -> str:
	selected = value or ("windows" if os.name == "nt" else "linux")
	if selected not in UNIFIED_RELEASE_PLATFORM_TOOL_PATHS:
		raise ReleaseError(f"unsupported release platform: {selected}")
	host = "windows" if os.name == "nt" else "linux"
	if selected != host:
		raise ReleaseError(f"{selected} artifacts must be built on a {selected} runner")
	return selected


def _run(command: Sequence[str], *, cwd: Path, timeout_seconds: float) -> subprocess.CompletedProcess[str]:
	try:
		result = subprocess.run(
			list(command),
			cwd=cwd,
			capture_output=True,
			text=True,
			encoding="utf-8",
			check=False,
			timeout=timeout_seconds,
		)
	except subprocess.TimeoutExpired as error:
		raise ReleaseError(f"release adapter command timed out after {timeout_seconds:g} seconds: {command[0]}") from error
	except (OSError, UnicodeError) as error:
		raise ReleaseError(f"release adapter command is unavailable: {command[0]}") from error
	if result.returncode:
		details = (result.stderr or result.stdout).strip()
		if len(details) > 4000:
			details = details[-4000:]
		suffix = f": {details}" if details else ""
		raise ReleaseError(f"release adapter command failed: {command[0]}{suffix}")
	return result


def _sha256(path: Path) -> str:
	digest = hashlib.sha256()
	with path.open("rb") as stream:
		for block in iter(lambda: stream.read(1024 * 1024), b""):
			digest.update(block)
	return digest.hexdigest()


def _assert_source(root: Path, source_sha: str) -> None:
	validate_source_sha(source_sha)
	result = _run(
		["git", "rev-parse", "HEAD"],
		cwd=root,
		timeout_seconds=load_release_timeout(root, "subprocess_seconds"),
	)
	if result.stdout.strip() != source_sha:
		raise ReleaseError("release adapter is not running against the exact merged source")


def _download(url: str, destination: Path, *, timeout_seconds: float) -> None:
	temporary = destination.with_name(f".{destination.name}.tmp")
	try:
		request = Request(url, headers={"Accept": "application/octet-stream", "User-Agent": "quality-gate-release"})
		with urlopen(request, timeout=timeout_seconds) as response, temporary.open("wb") as stream:
			stream.write(read_bounded(response, MAX_DOWNLOAD_BYTES, f"download {url}"))
		os.replace(temporary, destination)
	except (OSError, UnicodeError, ReleaseError) as error:
		try:
			temporary.unlink(missing_ok=True)
		except OSError:
			pass
		if isinstance(error, ReleaseError):
			raise
		raise ReleaseError(f"release adapter could not download {url}") from error


def _download_biome(root: Path, output: Path, platform: str) -> None:
	policy = tomllib.loads(
		(root / "quality_gate" / "policy" / "biome.toml").read_text(encoding="utf-8")
	)
	platform_key = "windows-x64" if platform == "windows" else "linux-x64"
	entry = policy.get("biome", {}).get("platforms", {}).get(platform_key)
	if not isinstance(entry, dict):
		raise ReleaseError(f"Biome inventory has no {platform_key} entry")
	path = entry.get("path")
	url = entry.get("url")
	digest = entry.get("sha256")
	if not all(isinstance(value, str) and value for value in (path, url, digest)):
		raise ReleaseError("Biome inventory is incomplete")
	target = output / path
	_download(url, target, timeout_seconds=load_release_timeout(root, "external_download_seconds"))
	if _sha256(target) != digest:
		raise ReleaseError("downloaded Biome binary does not match its policy digest")


def _safe_extraction_path(root: Path, member: str) -> Path:
	normalized = member.replace("\\", "/")
	if (
		not normalized
		or normalized.startswith("/")
		or re.match(r"^[A-Za-z]:", normalized)
		or ".." in normalized.split("/")
	):
		raise ReleaseError("gitleaks archive contains an unsafe path")
	parts = [part for part in normalized.split("/") if part not in {"", "."}]
	candidate = (root / Path(*parts)).resolve()
	try:
		candidate.relative_to(root.resolve())
	except ValueError as error:
		raise ReleaseError("gitleaks archive path escapes its extraction root") from error
	return candidate


def _download_gitleaks(root: Path, output: Path, platform: str) -> None:
	policy_path = root / ".release" / "release-tools.toml"
	try:
		policy = tomllib.loads(policy_path.read_text(encoding="utf-8"))
	except (OSError, UnicodeError, tomllib.TOMLDecodeError) as error:
		raise ReleaseError("gitleaks release policy is unreadable") from error
	gitleaks = policy.get("gitleaks") if isinstance(policy, dict) else None
	platforms = gitleaks.get("platforms") if isinstance(gitleaks, dict) else None
	entry = platforms.get(platform) if isinstance(platforms, dict) else None
	version = gitleaks.get("version") if isinstance(gitleaks, dict) else None
	url = entry.get("url") if isinstance(entry, dict) else None
	digest = entry.get("sha256") if isinstance(entry, dict) else None
	if version != UNIFIED_RELEASE_TOOLS["gitleaks"] or not isinstance(url, str) or not isinstance(digest, str):
		raise ReleaseError("gitleaks release policy is incomplete or mismatched")
	if SHA256.fullmatch(digest) is None:
		raise ReleaseError("gitleaks release policy digest is invalid")
	with tempfile.TemporaryDirectory(prefix="quality-gate-gitleaks-") as temporary:
		archive = Path(temporary) / f"gitleaks-{version}-{platform}.archive"
		_download(url, archive, timeout_seconds=load_release_timeout(root, "external_download_seconds"))
		if _sha256(archive) != digest:
			raise ReleaseError(f"downloaded gitleaks {platform} archive does not match its policy digest")
		try:
			if platform == "windows":
				with zipfile.ZipFile(archive) as zipped:
					member = next((item for item in zipped.infolist() if item.filename.casefold().endswith("gitleaks.exe")), None)
					if member is None or member.file_size > MAX_DOWNLOAD_BYTES:
						raise ReleaseError("gitleaks archive has no Windows executable")
					source = _safe_extraction_path(Path(temporary), member.filename)
					source.parent.mkdir(parents=True, exist_ok=True)
					with zipped.open(member) as stream, source.open("wb") as destination:
						destination.write(read_bounded(stream, MAX_DOWNLOAD_BYTES, "gitleaks Windows executable"))
			else:
				with tarfile.open(archive) as tar:
					member = next((item for item in tar.getmembers() if item.name.casefold().endswith("gitleaks")), None)
					if member is None or not member.isfile() or member.size > MAX_DOWNLOAD_BYTES:
						raise ReleaseError("gitleaks archive has no Linux executable")
					source = _safe_extraction_path(Path(temporary), member.name)
					source.parent.mkdir(parents=True, exist_ok=True)
					stream = tar.extractfile(member)
					if stream is None:
						raise ReleaseError("gitleaks Linux executable cannot be read")
					with stream, source.open("wb") as destination:
						destination.write(read_bounded(stream, MAX_DOWNLOAD_BYTES, "gitleaks Linux executable"))
		except (zipfile.BadZipFile, tarfile.TarError) as error:
			raise ReleaseError(f"downloaded gitleaks {platform} archive is malformed") from error
		target = output / UNIFIED_RELEASE_PLATFORM_TOOL_PATHS[platform]["gitleaks"]
		shutil.copy2(source, target)


def _wheel_name(value: str) -> str:
	return re.sub(r"[-_.]+", "_", value).casefold()


def _find_wheel(name: str, version: str, directory: Path) -> Path:
	candidates = []
	for candidate in directory.glob("*.whl"):
		parts = candidate.name[:-4].split("-")
		if len(parts) >= 5 and _wheel_name(parts[0]) == _wheel_name(name) and parts[1] == version:
			candidates.append(candidate)
	candidates.sort()
	if len(candidates) != 1:
		raise ReleaseError(f"release inventory has {len(candidates)} {name} wheels")
	return candidates[0]


def _write_manifest(output: Path, version: str, platform: str) -> None:
	tag = f"v{version}"
	tool_paths = dict(UNIFIED_RELEASE_PLATFORM_TOOL_PATHS[platform])
	tool_entries: list[tuple[str, str, str]] = []
	for name, tool_version in UNIFIED_RELEASE_TOOLS.items():
		path = tool_paths.get(name, f"{name}-{tool_version}-py3-none-any.whl")
		if not (output / path).is_file():
			path = _find_wheel(name, tool_version, output).name
		tool_entries.append((name, tool_version, path))
	tool_paths.update({name: path for name, _version, path in tool_entries})
	all_paths = sorted(
		path.relative_to(output).as_posix()
		for path in output.rglob("*")
		if path.is_file() and path.name != "release.toml"
	)
	files: list[tuple[str, str]] = []
	manifest_lines = ["[release]", f'version = "{tag}"', ""]
	for path in all_paths:
		if path in tool_paths.values():
			continue
		kind = "policy" if path in UNIFIED_RELEASE_POLICY_FILES else "artifact"
		if kind == "artifact" and not path.startswith("quality_gate-"):
			kind = "dependency"
		files.append((path, kind))
		manifest_lines.extend(
			[
				"[[release.files]]",
				f'path = "{path}"',
				f'sha256 = "{_sha256(output / path)}"',
				f'kind = "{kind}"',
				"",
			]
		)
	for name, tool_version, path in tool_entries:
		manifest_lines.extend(
			[
				"[[release.tools]]",
				f'name = "{name}"',
				f'version = "{tool_version}"',
				f'path = "{path}"',
				f'sha256 = "{_sha256(output / path)}"',
				"",
			]
		)
	validate_release_inventory(tag, files, tool_entries, platform=platform, actual_paths=["release.toml", *all_paths])
	temporary = output / ".release.toml.tmp"
	temporary.write_text("\n".join(manifest_lines), encoding="utf-8", newline="\n")
	os.replace(temporary, output / "release.toml")


def _archive(output: Path, version: str, platform: str) -> Path:
	label = "Windows" if platform == "windows" else "Linux"
	archive = output.parent / f"quality-gate-v{version}-{label}.zip"
	with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zipped:
		for path in sorted(item for item in output.rglob("*") if item.is_file()):
			zipped.write(path, path.relative_to(output).as_posix())
	return archive


def build(source_sha: str, output_dir: Path | str, *, platform: str | None = None) -> ArtifactIdentity:
	"""Build one existing Quality Gate platform archive from the exact source SHA."""
	root = _root()
	_assert_source(root, source_sha)
	projection = validate_version_projections(root)
	selected = _platform(platform)
	output = Path(output_dir).resolve()
	if output.exists() and any(output.iterdir()):
		raise ReleaseError("release adapter output directory must be empty")
	output.mkdir(parents=True, exist_ok=True)
	subprocess_timeout = load_release_timeout(root, "subprocess_seconds")
	_run(
		[
			sys.executable,
			"-m",
			"pip",
			"wheel",
			"--no-build-isolation",
			"--no-deps",
			"--wheel-dir",
			str(output),
			str(root),
		],
		cwd=root,
		timeout_seconds=subprocess_timeout,
	)
	requirements = root / ".release" / "release-requirements.txt"
	_run(
		[
			sys.executable,
			"-m",
			"pip",
			"download",
			"--require-hashes",
			"--only-binary=:all:",
			"--no-deps",
			"--dest",
			str(output),
			"-r",
			str(requirements),
		],
		cwd=root,
		timeout_seconds=subprocess_timeout,
	)
	for relative in UNIFIED_RELEASE_POLICY_FILES:
		target = output / relative
		target.parent.mkdir(parents=True, exist_ok=True)
		shutil.copy2(root / relative, target)
	_download_biome(root, output, selected)
	_download_gitleaks(root, output, selected)
	_write_manifest(output, projection.version, selected)
	archive = _archive(output, projection.version, selected)
	return ArtifactIdentity(
		projection.version,
		source_sha,
		selected,
		archive.name,
		_sha256(archive),
		str(archive),
	)


def runtime_check(source_sha: str, artifact: Path | str) -> ArtifactIdentity:
	"""Run the existing install/setup/audit release controller for one artifact."""
	root = _root()
	_assert_source(root, source_sha)
	projection = validate_version_projections(root)
	path = Path(artifact).resolve()
	try:
		verify_release_candidate(root, path, version=f"v{projection.version}")
	except ReleaseControllerError as error:
		raise ReleaseError(f"release controller rejected the artifact: {error}") from error
	return ArtifactIdentity(projection.version, source_sha, _platform(), path.name, _sha256(path), str(path))


def verify(source_sha: str, artifact: Path | str) -> ArtifactIdentity:
	"""Verify package identity, installation, and the isolated runtime/audit path."""
	return runtime_check(source_sha, artifact)


def bootstrap_setup(policy_release: str) -> None:
	"""Prepare the previous immutable policy release for this repository's CI bootstrap."""
	try:
		environment = prepare_bootstrap(_root(), policy_release=policy_release, create_runtimes=True)
	except (DistributionError, OSError, ReleaseControllerError, ReleaseError, RuntimeUnavailable) as error:
		raise ReleaseError(f"self-hosting bootstrap setup is unavailable: {error}") from error
	print(f"bootstrap setup: ready - {environment.release_manifest.version}")


def bootstrap_check(
	policy_release: str,
	*,
	base: str | None = None,
	head: str | None = None,
) -> int:
	return run_bootstrap_check(_root(), policy_release=policy_release, base=base, head=head).exit_code


def _parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(description=__doc__)
	subcommands = parser.add_subparsers(dest="command", required=True)
	for name in ("build", "verify", "runtime-check"):
		command = subcommands.add_parser(name)
		command.add_argument("--source-sha", required=True)
		command.add_argument("--output", type=Path)
		command.add_argument("--artifact", type=Path)
		command.add_argument("--platform", choices=("linux", "windows"))
	bootstrap_setup_parser = subcommands.add_parser("bootstrap-setup")
	bootstrap_setup_parser.add_argument("--policy-release", required=True)
	bootstrap_check_parser = subcommands.add_parser("bootstrap-check")
	bootstrap_check_parser.add_argument("--policy-release", required=True)
	bootstrap_check_parser.add_argument("--base")
	bootstrap_check_parser.add_argument("--head")
	return parser


def main(arguments: Sequence[str] | None = None) -> int:
	options = _parser().parse_args(arguments)
	try:
		if options.command == "bootstrap-setup":
			bootstrap_setup(options.policy_release)
			return 0
		if options.command == "bootstrap-check":
			return bootstrap_check(options.policy_release, base=options.base, head=options.head)
		if options.command == "build":
			if options.output is None:
				raise ReleaseError("build requires --output")
			result = build(options.source_sha, options.output, platform=options.platform)
		else:
			if options.artifact is None:
				raise ReleaseError(f"{options.command} requires --artifact")
			result = (
				runtime_check(options.source_sha, options.artifact)
				if options.command == "runtime-check"
				else verify(options.source_sha, options.artifact)
			)
	except (OSError, ReleaseError) as error:
		print(f"release adapter: unchecked - {error}")
		return 2
	print(json.dumps(result.as_dict()))
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
