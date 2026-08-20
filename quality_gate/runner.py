"""Deterministic quality checks used by the local hook and CI."""

from __future__ import annotations

import locale
import os
import subprocess
import sys
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path

MANIFEST_NAME = "quality-gate.toml"
POLICY_DIR = Path(__file__).resolve().parent / "policy"


class QualityGateError(RuntimeError):
	"""A configuration or quality check prevents completion."""


def emit(message: str) -> None:
	sys.stdout.write(f"{message}\n")


def decode_subprocess_output(output: bytes | str | None) -> str:
	"""Decode subprocess output across UTF-8 and Windows console encodings."""
	if output is None:
		return ""
	if isinstance(output, str):
		return output
	encodings = ["utf-8", locale.getpreferredencoding(False)]
	for encoding in dict.fromkeys(encodings):
		try:
			return output.decode(encoding)
		except UnicodeDecodeError:
			continue
	return output.decode("utf-8", errors="replace")


@dataclass(frozen=True)
class PythonComponent:
	path: Path
	tests: Path | None
	requirements: Path | None
	typecheck: bool


def repository_root(root: Path | None) -> Path:
	if root is not None:
		return root.resolve()
	result = subprocess.run(
		["git", "rev-parse", "--show-toplevel"],
		capture_output=True,
		text=True,
		encoding="utf-8",
		check=False,
	)
	if result.returncode:
		raise QualityGateError("Run quality-gate inside a Git repository or pass --root.")
	return Path(result.stdout.strip()).resolve()


def relative_path(root: Path, value: object, field: str) -> Path:
	if not isinstance(value, str) or not value:
		raise QualityGateError(f"{field} must be a non-empty relative path.")
	path = (root / value).resolve()
	if root not in path.parents and path != root:
		raise QualityGateError(f"{field} must stay inside the repository.")
	return path


def load_components(root: Path) -> list[PythonComponent]:
	manifest_path = root / MANIFEST_NAME
	if not manifest_path.is_file():
		raise QualityGateError(f"Missing {MANIFEST_NAME}. Run $setup-repo for this repository.")
	try:
		data = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
	except tomllib.TOMLDecodeError as error:
		raise QualityGateError(f"Invalid {MANIFEST_NAME}: {error}") from error
	quality = data.get("quality")
	if not isinstance(quality, dict) or quality.get("schema") != 1:
		raise QualityGateError(f"{MANIFEST_NAME} must declare [quality] schema = 1.")
	components_data = data.get("python")
	if not isinstance(components_data, list):
		raise QualityGateError(f"{MANIFEST_NAME} must declare python = [] or [[python]] entries.")
	components: list[PythonComponent] = []
	for index, item in enumerate(components_data, start=1):
		if not isinstance(item, dict):
			raise QualityGateError(f"python entry {index} must be a table.")
		path = relative_path(root, item.get("path"), f"python entry {index}.path")
		if not path.is_dir():
			raise QualityGateError(f"Python component path does not exist: {path}")
		tests_value = item.get("tests")
		tests = (
			relative_path(root, tests_value, f"python entry {index}.tests") if tests_value else None
		)
		requirements_value = item.get("requirements")
		requirements = (
			relative_path(root, requirements_value, f"python entry {index}.requirements")
			if requirements_value
			else None
		)
		typecheck = item.get("typecheck", True)
		if not isinstance(typecheck, bool):
			raise QualityGateError(f"python entry {index}.typecheck must be true or false.")
		components.append(PythonComponent(path, tests, requirements, typecheck))
	return components


def validate(root: Path | None = None) -> None:
	actual_root = repository_root(root)
	load_components(actual_root)
	emit(f"QUALITY GATE VALID: {actual_root / MANIFEST_NAME}")


def run(command: list[str], root: Path, environment: dict[str, str]) -> None:
	result = subprocess.run(
		command,
		cwd=root,
		env=environment,
		capture_output=True,
		check=False,
	)
	if result.returncode:
		detail = (
			decode_subprocess_output(result.stdout) + decode_subprocess_output(result.stderr)
		).strip()
		if not detail:
			detail = f"Command exited with {result.returncode}."
		raise QualityGateError(f"{' '.join(command)}\n\n{detail}")


def staged_python_files(root: Path) -> set[Path]:
	result = subprocess.run(
		["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"],
		cwd=root,
		capture_output=True,
		text=True,
		encoding="utf-8",
		check=False,
	)
	if result.returncode:
		raise QualityGateError("Could not read staged files from Git.")
	return {
		(root / value).resolve() for value in result.stdout.splitlines() if value.endswith(".py")
	}


def component_targets(component: PythonComponent, changed_files: set[Path] | None) -> list[Path]:
	if changed_files is None:
		return [component.path]
	return [
		path for path in changed_files if component.path in path.parents or path == component.path
	]


def temporary_directory(root: Path) -> tempfile.TemporaryDirectory[str]:
	parent = root / ".quality-gate-tmp"
	parent.mkdir(exist_ok=True)
	return tempfile.TemporaryDirectory(prefix="run-", dir=parent)


def check(root: Path | None = None, *, changed: bool = False) -> None:
	actual_root = repository_root(root)
	components = load_components(actual_root)
	changed_files = staged_python_files(actual_root) if changed else None
	with temporary_directory(actual_root) as temporary_path:
		environment = dict(os.environ)
		environment["TMP"] = temporary_path
		environment["TEMP"] = temporary_path
		for component in components:
			targets = component_targets(component, changed_files)
			if not targets:
				continue
			arguments = [str(path.relative_to(actual_root)) for path in targets]
			run(
				[
					sys.executable,
					"-m",
					"ruff",
					"check",
					"--config",
					str(POLICY_DIR / "ruff.toml"),
					*arguments,
				],
				actual_root,
				environment,
			)
			run(
				[
					sys.executable,
					"-m",
					"ruff",
					"format",
					"--check",
					"--config",
					str(POLICY_DIR / "ruff.toml"),
					*arguments,
				],
				actual_root,
				environment,
			)
			if component.typecheck:
				run(
					[
						sys.executable,
						"-m",
						"mypy",
						"--config-file",
						str(POLICY_DIR / "mypy.ini"),
						*arguments,
					],
					actual_root,
					environment,
				)
			if component.tests is not None and component.tests.is_dir():
				run(
					[
						sys.executable,
						"-m",
						"pytest",
						str(component.tests.relative_to(actual_root)),
						"-q",
						"-p",
						"no:cacheprovider",
					],
					actual_root,
					environment,
				)
	emit("QUALITY GATE PASSED")


def install_dependencies(root: Path | None = None) -> None:
	actual_root = repository_root(root)
	for component in load_components(actual_root):
		if component.requirements is None:
			continue
		if not component.requirements.is_file():
			raise QualityGateError(
				f"Development requirements file does not exist: {component.requirements}"
			)
		environment = dict(os.environ)
		environment["PYTHONIOENCODING"] = "utf-8"
		environment["PYTHONUTF8"] = "1"
		run(
			[sys.executable, "-m", "pip", "install", "-r", str(component.requirements)],
			actual_root,
			environment,
		)
