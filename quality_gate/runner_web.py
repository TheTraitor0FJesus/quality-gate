"""Web asset budgets and pinned Biome selection."""

from __future__ import annotations

import hashlib
import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

from .contracts import CheckResult, Finding, Manifest, Status, WebComponent
from .launcher import PreparedEnvironment
from .runner_errors import QualityGateError

EXIT_UNCHECKED = 2
BIOME_POLICY = "biome.toml"
BIOME_CONFIG = "biome.json"
BIOME_MAX_FILE_BYTES = 5 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class BiomeBinary:
	version: str
	path: str
	sha256: str


def _is_excluded(path: PurePosixPath, patterns: tuple[str, ...]) -> bool:
	for pattern in patterns:
		if path.match(pattern):
			return True
		prefix = pattern[:-3].rstrip("/") + "/"
		if pattern.endswith("/**") and path.as_posix().startswith(prefix):
			return True
	return False


def _is_link(path: Path) -> bool:
	is_junction = getattr(path, "is_junction", None)
	return path.is_symlink() or (is_junction is not None and is_junction())


def _unsafe_web_path(root: Path, path: Path) -> bool:
	resolved_root = root.resolve(strict=True)
	resolved_path = path.resolve(strict=True)
	if resolved_path != resolved_root and resolved_root not in resolved_path.parents:
		return True
	relative_parts = path.relative_to(root).parts
	path_chain = (
		root.joinpath(*relative_parts[:index]) for index in range(1, len(relative_parts) + 1)
	)
	return any(_is_link(candidate) for candidate in path_chain)


def _web_assets(
	root: Path,
	component: WebComponent,
	patterns: tuple[str, ...],
) -> tuple[tuple[Path, ...], tuple[Finding, ...]]:
	component_root = root / component.root
	assets: set[Path] = set()
	unsafe: list[Finding] = []
	for pattern in patterns:
		for candidate in component_root.glob(pattern):
			relative = PurePosixPath(candidate.relative_to(component_root).as_posix())
			if _is_excluded(relative, component.exclude):
				continue
			try:
				is_unsafe = _unsafe_web_path(root, candidate)
			except OSError:
				is_unsafe = True
			if is_unsafe:
				unsafe.append(
					Finding(
						path=candidate.relative_to(root).as_posix(),
						message="declared web asset is outside or traverses a symbolic link",
						action="replace it with a project-owned regular file",
					)
				)
				continue
			if not candidate.is_file():
				continue
			assets.add(candidate)
	return tuple(sorted(assets, key=lambda path: path.relative_to(root).as_posix())), tuple(unsafe)


def _web_budget_result(
	root: Path,
	component: WebComponent,
	component_index: int,
	*,
	language: Literal["javascript", "css"],
) -> CheckResult:
	patterns = component.javascript if language == "javascript" else component.css
	label = "JavaScript" if language == "javascript" else "CSS"
	check_id = f"web.component_{component_index}.{language}_budget"
	if not patterns:
		return CheckResult(
			check_id=check_id,
			status=Status.NOT_APPLICABLE,
			summary=f"no {label} assets are declared",
			recovery_action=f"declare {label} asset patterns if this component owns them",
		)
	component_root = root / component.root
	root_parts = component.root.split("/")
	path_chain = tuple(
		root.joinpath(*root_parts[:index]) for index in range(1, len(root_parts) + 1)
	)
	try:
		resolved_root = root.resolve(strict=True)
		resolved_component_root = component_root.resolve(strict=True)
	except OSError:
		return CheckResult(
			check_id=check_id,
			status=Status.UNCHECKED,
			summary=f"{label} asset boundary could not be resolved",
			findings=(
				Finding(
					path=component.root,
					message="declared web component root could not be resolved",
					action="restore a readable project-owned component root",
				),
			),
			recovery_action="restore a readable web component root and retry",
		)
	root_is_unsafe = (
		resolved_component_root != resolved_root
		and resolved_root not in resolved_component_root.parents
	) or any(_is_link(path) for path in path_chain)
	if root_is_unsafe:
		return CheckResult(
			check_id=check_id,
			status=Status.UNCHECKED,
			summary=f"{label} asset boundary is unsafe",
			findings=(
				Finding(
					path=component.root,
					message="declared web component root is or traverses a symbolic link",
					action="use a project-owned regular directory inside the repository",
				),
			),
			recovery_action="declare a regular web component root inside the repository",
		)
	if not component_root.is_dir():
		return CheckResult(
			check_id=check_id,
			status=Status.UNCHECKED,
			summary=f"{label} asset budget is unavailable",
			findings=(
				Finding(
					path=component.root,
					message="declared web component root does not exist",
					action="restore the declared web component root",
				),
			),
			recovery_action="restore the declared web component root and retry the quality gate",
		)
	try:
		assets, unsafe = _web_assets(root, component, patterns)
		sizes = tuple((asset, asset.stat().st_size) for asset in assets)
	except OSError:
		return CheckResult(
			check_id=check_id,
			status=Status.UNCHECKED,
			summary=f"{label} assets could not be measured",
			recovery_action="restore readable project-owned web assets and retry the quality gate",
		)
	if unsafe:
		return CheckResult(
			check_id=check_id,
			status=Status.UNCHECKED,
			summary=f"{label} asset boundary contains an unsafe file",
			findings=unsafe,
			recovery_action="replace linked or external assets with project-owned regular files",
		)
	if not sizes:
		return CheckResult(
			check_id=check_id,
			status=Status.UNCHECKED,
			summary=f"declared {label} patterns match no project-owned assets",
			recovery_action=f"correct the {label} patterns or add the declared assets and retry",
		)
	file_kib = component.javascript_file_kib if language == "javascript" else component.css_file_kib
	total_kib = (
		component.javascript_total_kib if language == "javascript" else component.css_total_kib
	)
	file_limit = file_kib * 1024
	total_limit = total_kib * 1024
	findings = [
		Finding(
			path=asset.relative_to(root).as_posix(),
			message=f"{label} file size {size} bytes exceeds {file_limit} bytes",
			action=f"reduce the file to at most {file_kib} KiB or set a reviewed component limit",
		)
		for asset, size in sizes
		if size > file_limit
	]
	total_size = sum(size for _, size in sizes)
	if total_size > total_limit:
		findings.append(
			Finding(
				path=component.root,
				message=f"total {label} size {total_size} bytes exceeds {total_limit} bytes",
				action=(
					f"reduce component assets to at most {total_kib} KiB or set a reviewed limit"
				),
			)
		)
	if findings:
		return CheckResult(
			check_id=check_id,
			status=Status.FAILED,
			summary=f"{label} assets exceed the configured size budget",
			findings=tuple(findings),
			recovery_action="reduce the staged assets or review and declare a justified limit",
		)
	return CheckResult(
		check_id=check_id,
		status=Status.PASSED,
		summary=f"{len(sizes)} {label} asset(s), {total_size} bytes, within budget",
	)


def web_budget_results(root: Path, manifest: Manifest) -> tuple[CheckResult, ...]:
	return tuple(
		result
		for index, component in enumerate(manifest.web, start=1)
		for result in (
			_web_budget_result(root, component, index, language="javascript"),
			_web_budget_result(root, component, index, language="css"),
		)
	)


def _biome_unchecked_results(
	component_index: int,
	message: str,
	*,
	findings: tuple[Finding, ...] = (),
	recovery_action: str = (
		"restore the Biome policy and declared web assets, then retry the quality gate"
	),
) -> list[CheckResult]:
	return [
		CheckResult(
			check_id=f"web.component_{component_index}.biome_{check}",
			status=Status.UNCHECKED,
			summary=message,
			findings=findings,
			recovery_action=recovery_action,
		)
		for check in ("lint", "format")
	]


def _biome_inventory(policy_root: Path) -> BiomeBinary:
	"""Return the pinned Biome version, platform path, and digest."""
	try:
		raw = tomllib.loads(
			(policy_root / "quality_gate" / "policy" / BIOME_POLICY).read_text(encoding="utf-8")
		)
	except (OSError, UnicodeError, tomllib.TOMLDecodeError) as error:
		raise QualityGateError(
			"Biome inventory is missing or invalid",
			check_id="runtime.policy",
			exit_code=EXIT_UNCHECKED,
			recovery_action="restore the pinned Biome inventory and retry the quality gate",
		) from error
	biome = raw.get("biome")
	platform = "windows-x64" if os.name == "nt" else "linux-x64"
	if not isinstance(biome, dict) or not isinstance(biome.get("version"), str):
		raise QualityGateError(
			"Biome inventory has no pinned version",
			check_id="runtime.policy",
			exit_code=EXIT_UNCHECKED,
			recovery_action="restore the pinned Biome inventory and retry the quality gate",
		)
	platforms = biome.get("platforms")
	entry = platforms.get(platform) if isinstance(platforms, dict) else None
	expected_path = {
		"linux-x64": "biome-linux-x64",
		"windows-x64": "biome-win32-x64.exe",
	}[platform]
	path = entry.get("path") if isinstance(entry, dict) else None
	digest = entry.get("sha256") if isinstance(entry, dict) else None
	if (
		not isinstance(path, str)
		or not path.strip()
		or path != expected_path
		or not isinstance(digest, str)
		or not re.fullmatch(r"[0-9a-f]{64}", digest)
	):
		raise QualityGateError(
			f"Biome inventory has no usable {platform} binary pin",
			check_id="runtime.policy",
			exit_code=EXIT_UNCHECKED,
			recovery_action="restore the pinned Biome inventory and retry the quality gate",
		)
	return BiomeBinary(biome["version"], path, digest)


def _biome_command(prepared: PreparedEnvironment) -> list[str]:
	pinned = _biome_inventory(prepared.policy_root)
	release_manifest = getattr(prepared, "release_manifest", None)
	tools = getattr(release_manifest, "tools", ())
	tool = next((item for item in tools if item.name == "biome"), None)
	if tool is None or (
		tool.version != pinned.version
		or tool.path.replace("\\", "/") != pinned.path
		or tool.sha256 != pinned.sha256
	):
		raise QualityGateError(
			"selected release does not contain the pinned Biome binary",
			check_id="runtime.policy",
			exit_code=EXIT_UNCHECKED,
			recovery_action="sync the release with the exact pinned Biome inventory and retry",
		)
	config = prepared.policy_root / "quality_gate" / "policy" / BIOME_CONFIG
	if not config.is_file():
		raise QualityGateError(
			"Biome configuration is unavailable",
			check_id="runtime.policy",
			exit_code=EXIT_UNCHECKED,
			recovery_action="restore the pinned Biome configuration and retry the quality gate",
		)
	binary = prepared.policy_root / tool.path
	try:
		if not binary.is_file() or hashlib.sha256(binary.read_bytes()).hexdigest() != tool.sha256:
			raise OSError("Biome binary is missing or corrupt")
	except OSError as error:
		raise QualityGateError(
			"Biome binary is missing or corrupt",
			check_id="runtime.policy",
			exit_code=EXIT_UNCHECKED,
			recovery_action="sync the exact pinned Biome binary and retry the quality gate",
		) from error
	return [str(binary), "ci", "--config-path", str(config)]


def _biome_asset_paths(root: Path, component: WebComponent, component_index: int) -> list[str]:
	budget_results: list[CheckResult] = []
	if component.javascript:
		budget_results.append(
			_web_budget_result(root, component, component_index, language="javascript")
		)
	if component.css:
		budget_results.append(_web_budget_result(root, component, component_index, language="css"))
	if any(result.status is Status.UNCHECKED for result in budget_results):
		raise QualityGateError(
			"Biome asset inputs are unavailable",
			check_id="runtime.policy",
			exit_code=EXIT_UNCHECKED,
			recovery_action="restore readable declared web assets and retry the quality gate",
		)
	assets: set[Path] = set()
	for patterns in (component.javascript, component.css):
		matched, unsafe = _web_assets(root, component, patterns)
		if unsafe:
			raise QualityGateError(
				"Biome asset boundary contains an unsafe file",
				check_id="runtime.policy",
				exit_code=EXIT_UNCHECKED,
				recovery_action=(
					"replace linked or external assets with project-owned regular files"
				),
			)
		assets.update(matched)
	if not assets:
		raise QualityGateError(
			"declared Biome patterns match no project-owned assets",
			check_id="runtime.policy",
			exit_code=EXIT_UNCHECKED,
			recovery_action="correct the declared web patterns and retry the quality gate",
		)
	if any(asset.stat().st_size > BIOME_MAX_FILE_BYTES for asset in assets):
		raise QualityGateError(
			"declared Biome asset exceeds the policy file-size limit",
			check_id="runtime.policy",
			exit_code=EXIT_UNCHECKED,
			recovery_action=(
				"reduce the declared asset below the Biome policy file-size limit and retry"
			),
		)
	return [asset.relative_to(root).as_posix() for asset in sorted(assets)]


def _biome_component_commands(
	component_index: int,
	command: list[str],
	paths: list[str],
	timeout: int,
) -> tuple[tuple[str, list[str], int], tuple[str, list[str], int]]:
	return (
		(
			f"web.component_{component_index}.biome_lint",
			[
				*command,
				"--formatter-enabled=false",
				"--linter-enabled=true",
				"--assist-enabled=false",
				*paths,
			],
			timeout,
		),
		(
			f"web.component_{component_index}.biome_format",
			[
				*command,
				"--formatter-enabled=true",
				"--linter-enabled=false",
				"--assist-enabled=false",
				*paths,
			],
			timeout,
		),
	)
