from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import zipfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from quality_gate import runtime_cli
from quality_gate.contracts import load_manifest
from quality_gate.distribution import PolicyCache
from quality_gate.runtime import RuntimeIdentity, RuntimeInspection, RuntimeManager

UNAVAILABLE_EXIT = 2


def _manifest(root: Path, components: tuple[tuple[str, str], ...] = (("app", "app"),)) -> None:
	python_version = f"{sys.version_info.major}.{sys.version_info.minor}"
	component_tables = "\n".join(
		f'''[[python]]
name = "{name}"
path = "{path}"
python_version = "{python_version}"
dependency_inputs = []
test_paths = []
tests_applicable = false
tests_reason = "No tests are declared in this fixture."
timeout_seconds = 300'''
		for name, path in components
	)
	(root / "quality-gate.toml").write_text(
		f"""waivers = []

[quality]
schema = 2
policy_release = "v2.0.0"

[repository]
name = "runtime-cli-test"
domains = ["repository", "python"]
required_documents = ["quality-gate.toml"]

[repository.limits]
max_blob_size_mib = 5

[repository.defaults]
command_timeout_seconds = 120
test_timeout_seconds = 300
gate_timeout_seconds = 600

{component_tables}
""",
		encoding="utf-8",
	)


def _policy_release(source: Path) -> None:
	source.mkdir()
	files = {
		"quality_gate-2.0.0-py3-none-any.whl": b"wheel",
		"policy.txt": b"shared policy\n",
		"ruff.exe": b"ruff",
	}
	for name, content in files.items():
		(source / name).write_bytes(content)
		digest = hashlib.sha256(content).hexdigest()
		if name.endswith(".whl"):
			wheel = f'[[release.files]]\npath = "{name}"\nsha256 = "{digest}"'
		elif name == "policy.txt":
			policy = f'[[release.files]]\npath = "{name}"\nsha256 = "{digest}"\nkind = "policy"'
		else:
			tool = (
				f'[[release.tools]]\nname = "ruff"\nversion = "0.15.12"\n'
				f'path = "{name}"\nsha256 = "{digest}"'
			)
	(source / "release.toml").write_text(
		f'[release]\nversion = "v2.0.0"\n\n{wheel}\n\n{policy}\n\n{tool}\n',
		encoding="utf-8",
	)


def _environment() -> dict[str, str]:
	environment = os.environ.copy()
	project_path = str(Path(__file__).resolve().parents[1])
	prior_path = environment.get("PYTHONPATH", "")
	environment["PYTHONPATH"] = os.pathsep.join(path for path in (project_path, prior_path) if path)
	return environment


def _run(root: Path, cache: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
	return subprocess.run(
		[
			sys.executable,
			"-m",
			"quality_gate",
			"--root",
			str(root),
			"runtimes",
			"--json",
			"--cache-dir",
			str(cache),
			*arguments,
		],
		cwd=root,
		env=_environment(),
		capture_output=True,
		text=True,
		encoding="utf-8",
		check=False,
	)


def _tree_snapshot(root: Path) -> tuple[tuple[str, bytes | None], ...]:
	entries = [
		(
			path.relative_to(root).as_posix(),
			path.read_bytes() if path.is_file() else None,
		)
		for path in root.rglob("*")
	]
	return tuple(sorted(entries))


def _prepare_runtime(root: Path, cache: Path, component_name: str = "app") -> Path:
	manifest = load_manifest(root)
	component = next(item for item in manifest.python if item.name == component_name)
	inspection = RuntimeManager(cache).ensure(
		root,
		manifest,
		component,
		python_executable=Path(sys.executable),
		install=lambda _python, _root, _inputs: None,
	)
	assert inspection.current and inspection.python is not None
	return inspection.path


@pytest.fixture
def short_runtime_cache(tmp_path: Path) -> Iterator[Path]:
	cache = tmp_path / "runtime-cache"
	cache.mkdir()
	yield cache
	shutil.rmtree(cache)


def test_runtimes_json_reports_existing_exact_environment_read_only(
	tmp_path: Path, short_runtime_cache: Path
) -> None:
	root = tmp_path / "project"
	root.mkdir()
	_manifest(root)
	cache = short_runtime_cache
	source = tmp_path / "source"
	_policy_release(source)
	PolicyCache(cache).sync(source)
	_prepare_runtime(root, cache)
	before_project = _tree_snapshot(root)
	before_cache = _tree_snapshot(cache)

	result = _run(root, cache)

	assert result.returncode == 0
	assert result.stderr == ""
	report = json.loads(result.stdout)
	assert report["schema"] == 1
	assert report["engine_version"]
	assert report["root"] == str(root.resolve())
	assert report["policy"] == {
		"release": "v2.0.0",
		"available": True,
		"reason_code": None,
		"reason": None,
	}
	assert report["components"][0]["available"] is True
	assert Path(report["components"][0]["python_executable"]).is_absolute()
	assert report["components"][0]["reason_code"] is None
	assert report["error"] is None
	assert _tree_snapshot(root) == before_project
	assert _tree_snapshot(cache) == before_cache


def test_component_inventory_does_not_execute_cached_interpreter(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	root = tmp_path / "project"
	root.mkdir()
	_manifest(root)
	manifest = load_manifest(root)
	executable = tmp_path / "untrusted-python"
	executable.write_text("untrusted interpreter", encoding="utf-8")
	manager = RuntimeManager(tmp_path / "cache")

	def inspect(
		_manager: RuntimeManager, _root: Path, _identity: RuntimeIdentity
	) -> RuntimeInspection:
		return RuntimeInspection(tmp_path / "runtime", executable, True)

	def reject_execution(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
		raise AssertionError("the inventory executed a cached interpreter")

	monkeypatch.setattr(RuntimeManager, "inspect", inspect)
	monkeypatch.setattr(subprocess, "run", reject_execution)

	component, available = runtime_cli._component_record(
		root, manifest.python[0], manifest, manager
	)

	assert available
	assert component["available"] is True
	assert component["python_executable"] == str(executable)


def test_runtimes_json_reports_missing_runtime_and_missing_policy_without_losing_inventory(
	tmp_path: Path, short_runtime_cache: Path
) -> None:
	root = tmp_path / "project"
	root.mkdir()
	_manifest(root, (("ready", "src/ready"), ("missing", "src/missing")))
	cache = short_runtime_cache
	_policy_release(tmp_path / "source")
	PolicyCache(cache).sync(tmp_path / "source")
	_prepare_runtime(root, cache, "ready")
	shutil.rmtree(cache / "releases" / "v2.0.0")

	result = _run(root, cache)

	assert result.returncode == UNAVAILABLE_EXIT
	report = json.loads(result.stdout)
	assert [item["name"] for item in report["components"]] == ["ready", "missing"]
	assert report["components"][0]["available"] is True
	assert report["components"][1]["reason_code"] == "runtime_missing"
	assert report["policy"]["reason_code"] == "release_unavailable"
	assert "runtimes:" in result.stderr


def test_runtimes_json_reports_stale_runtime_metadata(
	tmp_path: Path, short_runtime_cache: Path
) -> None:
	root = tmp_path / "project"
	root.mkdir()
	_manifest(root)
	cache = short_runtime_cache
	_policy_release(tmp_path / "source")
	PolicyCache(cache).sync(tmp_path / "source")
	runtime = _prepare_runtime(root, cache)
	metadata_path = runtime / "runtime.json"
	metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
	metadata["component"] = "stale"
	metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

	result = _run(root, cache)

	assert result.returncode == UNAVAILABLE_EXIT
	component = json.loads(result.stdout)["components"][0]
	assert component["available"] is False
	assert component["python_executable"] is None
	assert component["reason_code"] == "runtime_stale"


def test_runtimes_json_does_not_substitute_runtime_after_dependency_change(
	tmp_path: Path, short_runtime_cache: Path
) -> None:
	root = tmp_path / "project"
	root.mkdir()
	_manifest(root)
	manifest_path = root / "quality-gate.toml"
	manifest_path.write_text(
		manifest_path.read_text(encoding="utf-8").replace(
			"dependency_inputs = []", 'dependency_inputs = ["requirements.txt"]'
		),
		encoding="utf-8",
	)
	(root / "requirements.txt").write_text("sample-package==1.0\n", encoding="utf-8")
	cache = short_runtime_cache
	_policy_release(tmp_path / "source")
	PolicyCache(cache).sync(tmp_path / "source")
	_prepare_runtime(root, cache)
	(root / "requirements.txt").write_text("sample-package==2.0\n", encoding="utf-8")

	result = _run(root, cache)

	assert result.returncode == UNAVAILABLE_EXIT
	component = json.loads(result.stdout)["components"][0]
	assert component["available"] is False
	assert component["reason_code"] == "runtime_missing"


def test_runtimes_json_invalid_manifest_and_missing_root_are_parseable(tmp_path: Path) -> None:
	root = tmp_path / "project"
	root.mkdir()
	(root / "quality-gate.toml").write_text("[quality\n", encoding="utf-8")
	cache = tmp_path / "cache"

	invalid_manifest = _run(root, cache)
	missing_root = subprocess.run(
		[
			sys.executable,
			"-m",
			"quality_gate",
			"--root",
			str(tmp_path / "missing"),
			"runtimes",
			"--json",
			"--cache-dir",
			str(cache),
		],
		cwd=root,
		env=_environment(),
		capture_output=True,
		text=True,
		encoding="utf-8",
		check=False,
	)

	assert invalid_manifest.returncode == UNAVAILABLE_EXIT
	assert json.loads(invalid_manifest.stdout)["error"]["reason_code"] == "manifest_invalid"
	assert missing_root.returncode == UNAVAILABLE_EXIT
	assert json.loads(missing_root.stdout)["error"]["reason_code"] == "root_not_found"


def test_runtimes_json_reports_unresolvable_current_directory(
	monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
	def unavailable(cls: type[Path]) -> Path:
		del cls
		raise OSError("current directory is unavailable")

	monkeypatch.setattr(Path, "cwd", classmethod(unavailable))

	result = runtime_cli.runtimes(None, None)

	output = capsys.readouterr()
	assert result == UNAVAILABLE_EXIT
	report = json.loads(output.out)
	assert report["schema"] == 1
	assert report["error"]["reason_code"] == "inspection_failed"
	assert "runtimes:" in output.err


def test_runtime_command_help_has_no_side_effects(tmp_path: Path) -> None:
	root = tmp_path / "project"
	root.mkdir()
	cache = tmp_path / "cache"
	result = subprocess.run(
		[sys.executable, "-m", "quality_gate", "--root", str(root), "runtimes", "--help"],
		cwd=root,
		env=_environment(),
		capture_output=True,
		text=True,
		encoding="utf-8",
		check=False,
	)

	assert result.returncode == 0
	assert "--json" in result.stdout
	assert not cache.exists()
	assert list(root.iterdir()) == []


def test_runtimes_json_runs_from_candidate_wheel_on_pythonpath(
	tmp_path: Path, short_runtime_cache: Path
) -> None:
	repository = Path(__file__).resolve().parents[1]
	source = tmp_path / "quality-gate-source"
	shutil.copytree(
		repository,
		source,
		ignore=shutil.ignore_patterns(
			".git", ".venv", ".quality-gate-tmp", "build", "temp", "*.egg-info", "__pycache__"
		),
	)
	wheel_directory = tmp_path / "wheel"
	wheel_directory.mkdir()
	build_python = Path(sys.base_prefix) / ("python.exe" if os.name == "nt" else "bin/python3")
	built = subprocess.run(
		[
			str(build_python),
			"-m",
			"pip",
			"wheel",
			"--no-deps",
			"--no-build-isolation",
			str(source),
			"--wheel-dir",
			str(wheel_directory),
		],
		capture_output=True,
		text=True,
		timeout=300,
		check=False,
	)
	assert built.returncode == 0, built.stderr
	wheel = next(wheel_directory.glob("quality_gate-*.whl"))
	with zipfile.ZipFile(wheel) as archive:
		metadata_path = next(
			path for path in archive.namelist() if path.endswith(".dist-info/METADATA")
		)
		metadata = archive.read(metadata_path).decode("utf-8")
	wheel_version = next(
		line.removeprefix("Version: ")
		for line in metadata.splitlines()
		if line.startswith("Version: ")
	)

	project = tmp_path / "project"
	project.mkdir()
	_manifest(project)
	cache = short_runtime_cache
	policy_source = tmp_path / "policy-source"
	_policy_release(policy_source)
	PolicyCache(cache).sync(policy_source)
	_prepare_runtime(project, cache)
	assert [path.name for path in (cache / "releases").iterdir()] == ["v2.0.0"]
	project_before = _tree_snapshot(project)
	cache_before = _tree_snapshot(cache)
	environment = os.environ.copy()
	environment["PYTHONPATH"] = str(wheel)
	environment["PYTHONDONTWRITEBYTECODE"] = "1"
	result = subprocess.run(
		[
			sys.executable,
			"-m",
			"quality_gate",
			"--root",
			str(project),
			"runtimes",
			"--json",
			"--cache-dir",
			str(cache),
		],
		cwd=tmp_path,
		env=environment,
		capture_output=True,
		text=True,
		encoding="utf-8",
		check=False,
	)

	assert result.returncode == 0, result.stderr
	assert result.stderr == ""
	report = json.loads(result.stdout)
	assert report["engine_version"] == wheel_version
	assert report["components"][0]["available"] is True
	assert Path(report["components"][0]["python_executable"]).is_absolute()
	assert _tree_snapshot(project) == project_before
	assert _tree_snapshot(cache) == cache_before
