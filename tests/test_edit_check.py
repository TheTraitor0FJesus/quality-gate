"""Behavior at the executable working-tree edit-check boundary."""

from __future__ import annotations

import base64
import hashlib
import importlib.metadata
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from quality_gate import __version__
from quality_gate.contracts import load_manifest
from quality_gate.distribution import default_cache_root
from quality_gate.edit_check import Invocation
from quality_gate.edit_check import execute as execute_edit_check
from quality_gate.edit_inputs import Configuration
from quality_gate.edit_protocol import (
	SUPPORTED_POLICIES as EDIT_SUPPORTED_POLICIES,
)
from quality_gate.edit_protocol import (
	EditError,
	decode_request,
)
from quality_gate.release_contract import (
	UNIFIED_RELEASE_DEPENDENCIES,
	UNIFIED_RELEASE_PLATFORM_TOOL_PATHS,
	UNIFIED_RELEASE_POLICY_FILES,
	UNIFIED_RELEASE_TOOLS,
)
from quality_gate.runtime import RuntimeManager, runtime_identity

REPOSITORY = Path(__file__).resolve().parents[1]
EXIT_UNCHECKED = 2
EXPECTED_SELECTIONS = 2
FRESH_PROCESSES = 8
REUSED_PROCESSES = 3
REUSED_TWO_SELECTION_PROCESSES = 4
STILL_ACTIVE = 259
TIMEOUT_WITH_CLEANUP_SECONDS = 5
RELEASED_EDIT_POLICIES = frozenset(
	{
		"v2.4.2",
		"v2.5.0",
		"v2.6.0",
		"v2.6.1",
		"v2.6.2",
		"v3.0.0",
		"v3.1.0",
		"v3.2.0",
		"v4.0.0",
		"v4.1.0",
		"v4.2.0",
		"v4.2.1",
		"v4.3.0",
		"v4.4.0",
		"v4.5.0",
	}
)


def test_supported_policy_matrix_keeps_all_released_pins() -> None:
	assert EDIT_SUPPORTED_POLICIES == RELEASED_EDIT_POLICIES | {f"v{__version__}"}


def assert_project_files_after_cleanup(root: Path, *names: str) -> None:
	assert {item.name for item in root.iterdir()} == {*names, "temp"}
	assert {item.name for item in (root / "temp").iterdir()} == {".quality-gate.lock"}


@pytest.mark.parametrize(
	"initial,changed",
	[(b"# first\n", b"# changed\n"), (b"# first\n", None), (None, b"# created\n")],
)
def test_configuration_recapture_preserves_first_identity(
	tmp_path: Path, initial: bytes | None, changed: bytes | None
) -> None:
	path = tmp_path / "ruff.toml"
	if initial is not None:
		path.write_bytes(initial)
	configuration = Configuration(tmp_path)
	configuration.capture("ruff.toml")
	if changed is None:
		path.unlink()
	else:
		path.write_bytes(changed)
	with pytest.raises(EditError, match="stale_input"):
		configuration.capture("ruff.toml")


def digest(content: bytes) -> str:
	return hashlib.sha256(content).hexdigest()


@pytest.fixture(scope="module")
def edit_cache() -> Iterator[Path]:
	"""Immutable test infrastructure: real pinned Ruff, synthetic other inventory."""
	# Avoid ensurepip's Windows path limit inside Gate's nested temporary workspace.
	parent = Path(os.environ["SYSTEMROOT"]) / "Temp" if os.name == "nt" else None
	with tempfile.TemporaryDirectory(prefix="quality-gate-edit-", dir=parent) as directory:
		yield prepare_edit_cache(Path(directory))


def prepare_edit_cache(cache: Path, version: str = "v2.4.2") -> Path:
	release = cache / "releases" / version
	release.mkdir(parents=True)
	files = {
		f"quality_gate-{version.removeprefix('v')}-py3-none-any.whl": (
			"artifact",
			b"policy wheel fixture",
		)
	}
	files.update(
		{
			f"{name}-{version}-py3-none-any.whl": ("dependency", b"dependency fixture")
			for name, version in UNIFIED_RELEASE_DEPENDENCIES.items()
		}
	)
	files.update(
		{
			path: ("policy", (REPOSITORY / path).read_bytes())
			for path in UNIFIED_RELEASE_POLICY_FILES
		}
	)
	manifest = ["[release]", f'version = "{version}"']
	for path, (kind, content) in files.items():
		target = release / path
		target.parent.mkdir(parents=True, exist_ok=True)
		target.write_bytes(content)
		manifest.extend(
			[
				"[[release.files]]",
				f'path = "{path}"',
				f'kind = "{kind}"',
				f'sha256 = "{digest(content)}"',
			]
		)
	platform = "windows" if os.name == "nt" else "linux"
	for name, tool_version in UNIFIED_RELEASE_TOOLS.items():
		path = UNIFIED_RELEASE_PLATFORM_TOOL_PATHS[platform].get(
			name, f"{name}-{tool_version}-py3-none-any.whl"
		)
		content = b"tool fixture"
		if name == "ruff":
			wheel = next((default_cache_root() / "releases" / "v2.4.2").glob("ruff-*.whl"), None)
			if wheel is not None:
				content = wheel.read_bytes()
			else:
				distribution = importlib.metadata.distribution("ruff")
				assert distribution.version == "0.16.4", (
					"Prepare the release-pinned Ruff before running this suite"
				)
				binary = next(
					item for item in distribution.files or [] if item.name in {"ruff", "ruff.exe"}
				)
				with zipfile.ZipFile(release / path, "w") as archive:
					binary_content = Path(str(distribution.locate_file(binary))).read_bytes()
					archive.writestr(f"ruff/{binary.name}", binary_content)
					encoded_digest = (
						base64.urlsafe_b64encode(hashlib.sha256(binary_content).digest())
						.decode()
						.rstrip("=")
					)
					archive.writestr(
						f"ruff-{tool_version}.dist-info/RECORD",
						f"ruff/{binary.name},sha256={encoded_digest},{len(binary_content)}\n",
					)
				content = (release / path).read_bytes()
		(release / path).write_bytes(content)
		manifest.extend(
			[
				"[[release.tools]]",
				f'name = "{name}"',
				f'version = "{tool_version}"',
				f'path = "{path}"',
				f'sha256 = "{digest(content)}"',
			]
		)
	(release / "release.toml").write_text("\n".join(manifest) + "\n", encoding="utf-8")
	result = subprocess.run(
		[
			sys.executable,
			"-B",
			"-m",
			"quality_gate.cli",
			"edit-setup",
			"--policy-release",
			version,
			"--cache-dir",
			str(cache),
		],
		cwd=REPOSITORY,
		capture_output=True,
		check=False,
	)
	assert result.returncode == 0, result.stdout + result.stderr
	return cache


@pytest.fixture(scope="module")
def wheel_cache(tmp_path_factory: pytest.TempPathFactory) -> Path:
	cache = tmp_path_factory.mktemp("wheel-cache")
	for version in sorted(EDIT_SUPPORTED_POLICIES):
		prepare_edit_cache(cache, version)
	return cache


@pytest.fixture(scope="module")
def engine_wheel(tmp_path_factory: pytest.TempPathFactory) -> Path:
	"""Build immutable packaging infrastructure once; never extract it for execution."""
	root = tmp_path_factory.mktemp("engine-wheel")
	source = root / "source"
	source.mkdir()
	shutil.copy2(REPOSITORY / "pyproject.toml", source)
	shutil.copytree(
		REPOSITORY / "quality_gate",
		source / "quality_gate",
		ignore=shutil.ignore_patterns("__pycache__"),
	)
	# The release build backend lives in the base build environment, not project runtimes.
	build_python = Path(sys.base_prefix) / ("python.exe" if os.name == "nt" else "bin/python3")
	result = subprocess.run(
		[
			str(build_python),
			"-B",
			"-m",
			"pip",
			"wheel",
			"--no-deps",
			"--no-build-isolation",
			"--no-cache-dir",
			"--wheel-dir",
			str(root),
			str(source),
		],
		cwd=root,
		capture_output=True,
		check=False,
		timeout=60,
	)
	assert result.returncode == 0, result.stdout + result.stderr
	return next(root.glob("quality_gate-*.whl"))


@pytest.mark.parametrize(
	("command", "check_id"),
	[("edit-types", "mypy"), ("edit-integrity", "repository.git.conflict_markers")],
)
def test_wheel_exposes_read_only_edits_without_extraction(
	tmp_path: Path, engine_wheel: Path, command: str, check_id: str
) -> None:
	project = tmp_path / "project"
	project.mkdir()
	payload = request(project, tmp_path / "cache")
	payload["baseline"] = {"policy_release": "v2.4.2"}
	wheel_hash = digest(engine_wheel.read_bytes())
	completed = subprocess.run(
		[sys.executable, "-B", "-m", "quality_gate.cli", command],
		cwd=tmp_path,
		env=os.environ | {"PYTHONPATH": str(engine_wheel)},
		input=json.dumps(payload).encode(),
		capture_output=True,
		check=False,
		timeout=13,
	)
	result = json.loads(completed.stdout)
	assert completed.stderr == b""
	assert (completed.returncode, result["check_id"], result["status"]) == (
		0,
		check_id,
		"not_applicable",
	), result
	assert result["writes_may_have_occurred"] is False
	assert_project_files_after_cleanup(project)
	assert digest(engine_wheel.read_bytes()) == wheel_hash


@pytest.mark.parametrize("policy", sorted(EDIT_SUPPORTED_POLICIES))
@pytest.mark.parametrize("module", ["quality_gate.cli", "quality_gate.edit_cli"])
def test_wheel_on_pythonpath_checks_working_files_without_extraction(
	tmp_path: Path,
	wheel_cache: Path,
	engine_wheel: Path,
	policy: str,
	module: str,
) -> None:
	project = tmp_path / "project with spaces"
	project.mkdir()
	path = project / "example.py"
	path.write_bytes(b"import sys\nimport os\n\nvalue= (os.name, sys.version)\n")
	launcher = tmp_path / "launcher"
	launcher.mkdir()
	payload = request(project, wheel_cache, "example.py")
	payload["baseline"]["policy_release"] = policy
	wheel_hash = digest(engine_wheel.read_bytes())
	for expected_processes in (FRESH_PROCESSES, REUSED_PROCESSES):
		payload["files"][0]["sha256"] = digest(path.read_bytes())
		payload["deadline_unix_ms"] = int(time.time() * 1000) + 10_000
		completed = subprocess.run(
			[
				sys.executable,
				"-B",
				"-m",
				module,
				*(["edit-check"] if module.endswith(".cli") else []),
			],
			cwd=launcher,
			env={
				**os.environ,
				"PYTHONPATH": str(engine_wheel),
				"LOCALAPPDATA": str(tmp_path / "user-cache"),
				"XDG_CACHE_HOME": str(tmp_path / "user-cache"),
			},
			input=json.dumps(payload).encode(),
			capture_output=True,
			check=False,
			timeout=15,
		)
		assert completed.stderr == b"", completed.stderr.decode(errors="replace")
		result = json.loads(completed.stdout)
		assert completed.returncode == 0, result
		assert result["status"] == "passed"
		assert result["engine_version"] == __version__
		assert result["files"][0]["findings"] == []
		assert result["files"][0]["input_sha256"] == payload["files"][0]["sha256"]
		assert result["files"][0]["sha256"] == digest(path.read_bytes())
		assert result["files"][0]["format"] == "passed"
		assert result["selections"][0]["policy_release"] == policy
		assert result["process_count"] == expected_processes
	assert path.read_bytes() == b"import os\nimport sys\n\nvalue = (os.name, sys.version)\n"
	assert_project_files_after_cleanup(project, "example.py")
	assert list(launcher.iterdir()) == []
	assert digest(engine_wheel.read_bytes()) == wheel_hash


def request(root: Path, cache: Path, *paths: str) -> dict[str, Any]:
	return {
		"schema": 1,
		"root": str(root),
		"files": [{"path": path, "sha256": digest((root / path).read_bytes())} for path in paths],
		"cache_dir": str(cache),
		"baseline": {
			"policy_release": "v2.4.2",
			"python": sys.executable,
			"python_version": f"{sys.version_info.major}.{sys.version_info.minor}",
		},
		"deadline_unix_ms": int(time.time() * 1000) + 10_000,
	}


def invoke(payload: object) -> tuple[int, dict[str, Any]]:
	invocation_environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
	if isinstance(payload, dict) and isinstance(payload.get("root"), str):
		project = Path(payload["root"])
		# Keep mutable result reuse local to this test, independently of shared releases.
		cache_home = str(project.parent / f"{project.name}-user-cache")
		invocation_environment.update(LOCALAPPDATA=cache_home, XDG_CACHE_HOME=cache_home)
	result = subprocess.run(
		[sys.executable, "-B", "-m", "quality_gate.cli", "edit-check"],
		input=payload if isinstance(payload, bytes) else json.dumps(payload).encode(),
		cwd=REPOSITORY,
		env=invocation_environment,
		capture_output=True,
		check=False,
		timeout=15,
	)
	assert result.stderr == b"", result.stderr.decode(errors="replace")
	return result.returncode, json.loads(result.stdout)


@pytest.mark.parametrize(
	("initial", "expected_exit", "expected_rules"),
	[
		(b"value=  1\n", 0, set()),
		(b"value= missing_name\n", 1, {"F821"}),
	],
)
def test_unchanged_final_result_reuses_verified_selection(
	tmp_path: Path,
	edit_cache: Path,
	initial: bytes,
	expected_exit: int,
	expected_rules: set[str],
) -> None:
	path = tmp_path / "example.py"
	path.write_bytes(initial)
	first_exit, first = invoke(request(tmp_path, edit_cache, "example.py"))
	assert first_exit == expected_exit, first
	assert first["files"][0]["changed"] is True
	final_bytes = path.read_bytes()
	reused_exit, reused = invoke(request(tmp_path, edit_cache, "example.py"))
	assert reused_exit == expected_exit, reused
	assert reused["status"] == first["status"]
	assert reused["files"][0]["findings"] == first["files"][0]["findings"]
	assert {finding["rule"] for finding in reused["files"][0]["findings"]} == expected_rules
	assert reused["files"][0]["sha256"] == digest(final_bytes)
	assert reused["files"][0]["input_sha256"] == digest(final_bytes)
	assert reused["files"][0]["changed"] is False
	assert reused["selections"] == first["selections"]
	assert reused["inputs"] == first["inputs"]
	assert reused["writes_may_have_occurred"] is False
	assert path.read_bytes() == final_bytes
	assert first["process_count"] == FRESH_PROCESSES  # Entry, worker, probe and five Ruff starts.
	assert (
		reused["process_count"] == REUSED_PROCESSES
	)  # Revalidation still probes the selected Python.


@pytest.mark.parametrize("module_path", ["zebra.py", "zebra.pyi", "namespace/zebra.py"])
def test_import_layout_change_invalidates_reuse(
	tmp_path: Path, edit_cache: Path, module_path: str
) -> None:
	module_name = module_path.removesuffix(".pyi").removesuffix(".py").replace("/", ".")
	path = tmp_path / "example.py"
	path.write_bytes(
		f"import {module_name}\nimport alpha\n\nvalue = ({module_name}, alpha)\n".encode()
	)
	first_exit, first = invoke(request(tmp_path, edit_cache, "example.py"))
	assert first_exit == 0, first
	warm_exit, warm = invoke(request(tmp_path, edit_cache, "example.py"))
	assert warm_exit == 0 and warm["process_count"] == REUSED_PROCESSES, warm
	module = tmp_path / module_path
	module.parent.mkdir(parents=True, exist_ok=True)
	module.write_bytes(b"# Local module appeared after the previous checks.\n")
	changed_exit, changed = invoke(request(tmp_path, edit_cache, "example.py"))
	assert changed_exit == 0, changed
	assert changed["process_count"] == FRESH_PROCESSES
	assert path.read_bytes() == (
		f"import alpha\n\nimport {module_name}\n\nvalue = ({module_name}, alpha)\n".encode()
	)
	assert changed["files"][0]["sha256"] == digest(path.read_bytes())
	assert changed["files"][0]["findings"] == []
	assert module.read_bytes() == b"# Local module appeared after the previous checks.\n"


def test_policy_selection_change_requires_fresh_ruff(tmp_path: Path, wheel_cache: Path) -> None:
	(tmp_path / "example.py").write_bytes(b"import os\nvalue = os.name\n")
	for policy in sorted(EDIT_SUPPORTED_POLICIES):
		for expected_processes in (FRESH_PROCESSES, REUSED_PROCESSES):
			payload = request(tmp_path, wheel_cache, "example.py")
			payload["baseline"]["policy_release"] = policy
			exit_code, result = invoke(payload)
			assert exit_code == 0, result
			assert result["process_count"] == expected_processes
			assert result["selections"][0]["policy_release"] == policy


@pytest.mark.parametrize(
	"content",
	[
		b"'unterminated\n",
		b"".join(f"variable_{index} = {index}\n".encode() for index in range(260)),
	],
)
def test_unbounded_or_unlexable_import_context_runs_ruff(
	tmp_path: Path, edit_cache: Path, content: bytes
) -> None:
	(tmp_path / "example.py").write_bytes(content)
	for _ in range(2):
		exit_code, result = invoke(request(tmp_path, edit_cache, "example.py"))
		assert exit_code in {0, 1}, result
		assert result["process_count"] == FRESH_PROCESSES


@pytest.mark.parametrize("change", ["file", "ruff.toml", "pyproject.toml", ".gitignore", "waiver"])
def test_working_tree_changes_invalidate_reuse(
	tmp_path: Path, edit_cache: Path, change: str
) -> None:
	path = tmp_path / "example.py"
	path.write_bytes(b"import os\n\nvalue = os.name\n")
	manifest = tmp_path / "quality-gate.toml"
	manifest.write_text(
		'[quality]\nschema = 2\npolicy_release = "v2.4.2"\n'
		'[repository]\nname = "reuse"\ndomains = ["repository"]\n'
		'required_documents = ["quality-gate.toml"]\n',
		encoding="utf-8",
	)
	exit_code, original = invoke(request(tmp_path, edit_cache, "example.py"))
	assert exit_code == 0, original
	if change == "file":
		path.write_bytes(b"import os\n\nvalue=  2\n")
	elif change == "waiver":
		with manifest.open("a", encoding="utf-8") as stream:
			stream.write(
				'[[waivers]]\nkind = "standard"\ncheck_id = "repository.git.tracked_junk"\n'
				'target = "other.py"\nreason = "Controlled reuse invalidation fixture."\n'
				'approved_by = "fixture"\nreviewed_on = "2026-10-18"\nexpires_on = "2027-10-18"\n'
			)
	else:
		(tmp_path / change).write_bytes(b"# Working-tree configuration changed.\n")
	exit_code, changed = invoke(request(tmp_path, edit_cache, "example.py"))
	assert exit_code == 0, changed
	assert changed["process_count"] == FRESH_PROCESSES
	assert changed["files"][0]["findings"] == []
	assert changed["files"][0]["sha256"] == digest(path.read_bytes())
	if change != "file":
		assert changed["inputs"] != original["inputs"]


@pytest.mark.parametrize(
	"failure", ["record", "missing", "truncated", "oversized", "key", "no_key", "locked"]
)
def test_corrupt_reuse_cannot_supply_a_clean_verdict(
	tmp_path: Path, edit_cache: Path, failure: str
) -> None:
	path = tmp_path / "example.py"
	path.write_bytes(b"value = missing_name\n")
	exit_code, first = invoke(request(tmp_path, edit_cache, "example.py"))
	assert exit_code == 1, first
	cache = tmp_path.parent / f"{tmp_path.name}-user-cache" / "quality-gate/edit-results-v1"
	record = next(cache.glob("*.json"))
	if failure == "record":
		forged = json.loads(record.read_bytes())
		forged["payload"]["result"]["status"] = "passed"
		forged["payload"]["result"]["files"][0].update(status="passed", findings=[])
		record.write_text(json.dumps(forged), encoding="utf-8")
	elif failure == "missing":
		record.unlink()
	elif failure == "truncated":
		record.write_bytes(b'{"payload":')
	elif failure == "oversized":
		record.write_bytes(b"x" * (300 * 1024))
	elif failure == "key":
		(cache / "key").write_bytes(b"changed key")
	elif failure == "locked":
		(cache / ".lock").write_bytes(b"held by another invocation\n")
	else:
		(cache / "key").unlink()
	exit_code, checked = invoke(request(tmp_path, edit_cache, "example.py"))
	assert exit_code == 1, checked
	assert checked["process_count"] == FRESH_PROCESSES
	assert {finding["rule"] for finding in checked["files"][0]["findings"]} == {"F821"}
	assert checked["files"][0]["sha256"] == digest(path.read_bytes())
	assert path.read_bytes() == b"value = missing_name\n"


def test_invalid_request_is_unchecked_and_does_not_echo_input() -> None:
	exit_code, result = invoke(b'{"secret":"synthetic-sensitive-value",')
	assert exit_code == EXIT_UNCHECKED
	assert result["status"] == "unchecked"
	assert result["reason"] == "invalid_request"
	assert "synthetic-sensitive-value" not in json.dumps(result)


def test_expired_invocation_is_unchecked_before_file_selection(tmp_path: Path) -> None:
	exit_code, result = invoke(
		{
			"schema": 1,
			"root": str(tmp_path),
			"files": [],
			"deadline_unix_ms": int(time.time() * 1000) - 1,
		}
	)
	assert exit_code == EXIT_UNCHECKED
	assert result["reason"] == "deadline"
	assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
	("field", "value"),
	[
		("schema", True),
		("extra", 1),
		("deadline_unix_ms", 10**1000),
		("deadline_unix_ms", True),
		("cache_dir", None),
	],
)
def test_invalid_envelope_is_source_free(tmp_path: Path, field: str, value: object) -> None:
	payload: dict[str, Any] = {
		"schema": 1,
		"root": str(tmp_path),
		"files": [],
		"deadline_unix_ms": int(time.time() * 1000) + 5000,
	}
	payload[field] = value
	exit_code, result = invoke(payload)
	assert (exit_code, result["reason"]) == (2, "invalid_request")
	assert list(tmp_path.iterdir()) == []


def test_write_phase_removes_safe_f401_unused_import_from_module(
	tmp_path: Path, edit_cache: Path
) -> None:
	path = tmp_path / "example.py"
	path.write_bytes(b"import os\nvalue=1\n")
	payload = request(tmp_path, edit_cache, "example.py")
	payload["phase"] = "write"

	exit_code, result = invoke(payload)

	assert exit_code == 0, result
	assert result["status"] == "passed"
	assert path.read_bytes() == b"value = 1\n"


def test_write_phase_preserves_explicit_public_reexport_in_init(
	tmp_path: Path, edit_cache: Path
) -> None:
	package = tmp_path / "sample_package"
	package.mkdir()
	path = package / "__init__.py"
	original = b"from .api import public_name as public_name\n"
	path.write_bytes(original)
	(package / "api.py").write_bytes(b"public_name = 1\n")
	payload = request(tmp_path, edit_cache, "sample_package/__init__.py")
	payload["phase"] = "write"

	exit_code, result = invoke(payload)

	assert exit_code == 0, result
	assert result["status"] == "passed"
	assert path.read_bytes() == original


def test_safe_fixes_remove_unused_import_and_report_residual_lint(
	tmp_path: Path, edit_cache: Path
) -> None:
	path = tmp_path / "example.py"
	path.write_text("import sys\nimport os\n\nvalue= missing_name\n", encoding="utf-8")
	unrelated = tmp_path / "unrelated.py"
	unrelated.write_bytes(b"other=   2\n")
	exit_code, result = invoke(request(tmp_path, edit_cache, "example.py"))
	assert exit_code == 1, (result.get("reason"), result.get("stage"))
	assert result["status"] == "failed"
	assert "phase" not in result
	file = result["files"][0]
	assert {finding["rule"] for finding in file["findings"]} == {"F821"}
	assert file["format"] == "passed"
	assert file["coverage"] == "baseline"
	assert file["sha256"] == digest(path.read_bytes())
	assert path.read_text(encoding="utf-8") == "value = missing_name\n"
	assert unrelated.read_bytes() == b"other=   2\n"
	path.write_text("import os\nimport sys\n\nvalue = (os.name, sys.version)\n", encoding="utf-8")
	assert invoke(request(tmp_path, edit_cache, "example.py"))[0] == 0


def test_read_only_validation_reports_final_ruff_findings_without_writing(
	tmp_path: Path, edit_cache: Path
) -> None:
	path = tmp_path / "example.py"
	original = b"import sys\nimport os\n\nvalue=  1\n"
	path.write_bytes(original)
	original_mtime = path.stat().st_mtime_ns
	payload = request(tmp_path, edit_cache, "example.py")
	payload["phase"] = "validate"
	payload["deadline_unix_ms"] = int(time.time() * 1000) + 20_000

	exit_code, result = invoke(payload)

	assert exit_code == 1, result
	assert result["phase"] == "validate"
	assert result["status"] == "failed"
	assert result["writes_may_have_occurred"] is False
	file = result["files"][0]
	assert file["input_sha256"] == digest(original)
	assert file["sha256"] == digest(original)
	assert file["changed"] is False
	assert {finding["rule"] for finding in file["findings"]} >= {"F401", "I001"}
	assert file["format"] == "failed"
	assert path.read_bytes() == original
	assert path.stat().st_mtime_ns == original_mtime
	assert_project_files_after_cleanup(tmp_path, "example.py")


@pytest.mark.parametrize("policy", sorted(EDIT_SUPPORTED_POLICIES))
def test_read_only_validation_supports_every_policy_pin(
	tmp_path: Path, wheel_cache: Path, policy: str
) -> None:
	path = tmp_path / "example.py"
	original = b"value = 1\n"
	path.write_bytes(original)
	original_mtime = path.stat().st_mtime_ns
	payload = request(tmp_path, wheel_cache, "example.py")
	payload["baseline"]["policy_release"] = policy
	payload["phase"] = "validate"
	payload["deadline_unix_ms"] = int(time.time() * 1000) + 25_000

	exit_code, result = invoke(payload)

	assert exit_code == 0, result
	assert result["phase"] == "validate"
	assert result["status"] == "passed"
	assert result["writes_may_have_occurred"] is False
	assert result["selections"][0]["policy_release"] == policy
	file = result["files"][0]
	assert file["input_sha256"] == digest(original)
	assert file["sha256"] == digest(original)
	assert file["changed"] is False
	assert path.read_bytes() == original
	assert path.stat().st_mtime_ns == original_mtime
	assert_project_files_after_cleanup(tmp_path, "example.py")


@pytest.mark.parametrize("changed_input", ["source", "configuration"])
def test_read_only_validation_rejects_inputs_changed_during_checks(
	tmp_path: Path,
	tmp_path_factory: pytest.TempPathFactory,
	edit_cache: Path,
	monkeypatch: pytest.MonkeyPatch,
	changed_input: str,
) -> None:
	path = tmp_path / "example.py"
	original = b"value = 1\n"
	path.write_bytes(original)
	configuration = tmp_path / "ruff.toml"
	if changed_input == "configuration":
		configuration.write_bytes(b"# captured before Ruff starts\n")
	payload = request(tmp_path, edit_cache, "example.py")
	payload["phase"] = "validate"
	original_run = Invocation.run
	changed = False

	def run_then_change(
		self: Invocation, command: list[str], *, cwd: Path
	) -> tuple[int, bytes, bytes]:
		nonlocal changed
		result = original_run(self, command, cwd=cwd)
		if command[1] == "format" and "--check" in command and not changed:
			changed = True
			if changed_input == "source":
				path.write_bytes(b"value = 2\n")
			else:
				configuration.write_bytes(b"# changed during Ruff\n")
		return result

	monkeypatch.setattr(Invocation, "run", run_then_change)
	with tempfile.TemporaryDirectory(
		prefix="quality-gate-validate-test-", dir=tmp_path_factory.getbasetemp()
	) as directory:
		with pytest.raises(EditError, match="stale_input"):
			execute_edit_check(payload, time.monotonic() + 25, Path(directory))

	assert changed
	assert path.read_bytes() == (b"value = 2\n" if changed_input == "source" else original)
	assert {item.name for item in tmp_path.iterdir()} == (
		{"example.py", "ruff.toml"} if changed_input == "configuration" else {"example.py"}
	)


def test_write_phase_applies_only_safe_fixes_and_formatting(
	tmp_path: Path, edit_cache: Path
) -> None:
	path = tmp_path / "example.py"
	original = b"import sys\nimport os\n\nvalue= (os.name, sys.version)\n"
	path.write_bytes(original)
	payload = request(tmp_path, edit_cache, "example.py")
	payload["phase"] = "write"

	exit_code, result = invoke(payload)

	assert exit_code == 0, result
	assert result["phase"] == "write"
	assert result["status"] == "passed"
	assert result["writes_may_have_occurred"] is True
	file = result["files"][0]
	assert file["input_sha256"] == digest(original)
	assert file["sha256"] == digest(path.read_bytes())
	assert file["changed"] is True
	assert file["findings"] == []
	assert file["format"] == "passed"
	assert path.read_bytes() == b"import os\nimport sys\n\nvalue = (os.name, sys.version)\n"
	assert "F401" not in json.dumps(result)
	assert_project_files_after_cleanup(tmp_path, "example.py")


def test_read_only_validation_keeps_unchecked_prerequisites_incomplete(
	tmp_path: Path, edit_cache: Path
) -> None:
	path = tmp_path / "example.py"
	original = b"value = 1\n"
	path.write_bytes(original)
	payload = request(tmp_path, edit_cache, "example.py")
	payload["phase"] = "validate"
	payload["baseline"]["policy_release"] = "v99.0.0"

	exit_code, result = invoke(payload)

	assert exit_code == EXIT_UNCHECKED
	assert result["status"] == "unchecked"
	assert result["phase"] == "validate"
	assert result["writes_may_have_occurred"] is False
	assert path.read_bytes() == original
	assert_project_files_after_cleanup(tmp_path, "example.py")


def test_unknown_edit_phase_is_rejected_without_falling_back_to_writer(
	tmp_path: Path, edit_cache: Path
) -> None:
	path = tmp_path / "example.py"
	original = b"value=  1\n"
	path.write_bytes(original)
	payload = request(tmp_path, edit_cache, "example.py")
	payload["phase"] = "rewrite"

	exit_code, result = invoke(payload)

	assert exit_code == EXIT_UNCHECKED
	assert result["status"] == "unchecked"
	assert result["reason"] == "invalid_request"
	assert path.read_bytes() == original
	assert {item.name for item in tmp_path.iterdir()} == {"example.py"}


@pytest.mark.parametrize("reader", ["secrets", "types", "integrity"])
def test_ruff_phase_is_rejected_by_other_edit_readers(tmp_path: Path, reader: str) -> None:
	payload = {
		"schema": 1,
		"root": str(tmp_path),
		"files": [],
		"deadline_unix_ms": int(time.time() * 1000) + 5000,
		"phase": "validate",
	}

	with pytest.raises(EditError, match="invalid_request"):
		decode_request(json.dumps(payload).encode(), **{reader: True})


def test_preexisting_rules_production_test_suppression_and_redaction(
	tmp_path: Path, edit_cache: Path
) -> None:
	(tmp_path / "tests").mkdir()
	content = b'password = "synthetic-sensitive-value"\nassert password\n'
	(tmp_path / "production.py").write_bytes(content)
	(tmp_path / "tests/test_example.py").write_bytes(content)
	(tmp_path / "suppressed.py").write_bytes(b"import os  # noqa: F401\n")
	exit_code, result = invoke(
		request(tmp_path, edit_cache, "production.py", "tests/test_example.py", "suppressed.py")
	)
	assert exit_code == 1, (result.get("reason"), result.get("stage"))
	assert {finding["rule"] for finding in result["files"][0]["findings"]} == {"S101", "S105"}
	assert result["files"][1]["status"] == "passed"
	assert result["files"][2]["status"] == "passed"
	assert "synthetic-sensitive-value" not in json.dumps(result)


def test_stale_input_never_rewrites_any_file(tmp_path: Path, edit_cache: Path) -> None:
	path = tmp_path / "example.py"
	path.write_bytes(b"value=  1\n")
	payload = request(tmp_path, edit_cache, "example.py")
	path.write_bytes(b"value=  2\n")
	exit_code, result = invoke(payload)
	assert exit_code == EXIT_UNCHECKED
	assert result["reason"] == "stale_input"
	assert path.read_bytes() == b"value=  2\n"


def managed(root: Path, cache: Path, *, version: str = "v2.4.2", setup: bool = True) -> Path:
	(root / "src").mkdir(exist_ok=True)
	(root / "requirements.txt").write_bytes(b"")
	(root / "quality-gate.toml").write_text(
		f'''[quality]
schema = 2
policy_release = "{version}"
[repository]
name = "edit-fixture"
domains = ["repository", "python"]
required_documents = ["quality-gate.toml"]
[[python]]
name = "app"
path = "src"
python_version = "{sys.version_info.major}.{sys.version_info.minor}"
dependency_inputs = ["requirements.txt"]
test_paths = []
tests_applicable = false
tests_reason = "Disposable lint-only fixture."
''',
		encoding="utf-8",
	)
	if setup:
		manifest = load_manifest(root)
		RuntimeManager(cache).ensure(
			root,
			manifest,
			manifest.python[0],
			python_executable=Path(sys.executable),
			install=lambda *_: None,
		)
	path = root / "src/example.py"
	path.write_bytes(b"value=  1\n")
	return path


def test_managed_old_pin_and_outside_component_baseline_are_distinct(
	tmp_path: Path, edit_cache: Path
) -> None:
	managed(tmp_path, edit_cache)
	(tmp_path / "outside.py").write_bytes(b"other=  2\n")
	exit_code, result = invoke(request(tmp_path, edit_cache, "src/example.py", "outside.py"))
	assert exit_code == 0, result
	assert [file["coverage"] for file in result["files"]] == ["managed", "baseline"]
	assert {selection["policy_release"] for selection in result["selections"]} == {"v2.4.2"}
	assert len(result["selections"]) == EXPECTED_SELECTIONS
	assert result["files"][0]["component"] == "app"
	assert (tmp_path / "outside.py").read_bytes() == b"other = 2\n"
	exit_code, reused = invoke(request(tmp_path, edit_cache, "src/example.py", "outside.py"))
	assert exit_code == 0 and reused["process_count"] == REUSED_TWO_SELECTION_PROCESSES, reused
	manifest = load_manifest(tmp_path)
	manager = RuntimeManager(edit_cache)
	metadata = (
		manager.path_for(tmp_path, runtime_identity(tmp_path, manifest, manifest.python[0]))
		/ "runtime.json"
	)
	original_metadata = metadata.read_bytes()
	try:
		metadata.write_bytes(b"{}")
		exit_code, invalid = invoke(request(tmp_path, edit_cache, "src/example.py", "outside.py"))
		assert (exit_code, invalid["reason"]) == (2, "runtime"), invalid
	finally:
		metadata.write_bytes(original_metadata)
	(tmp_path / "requirements.txt").write_bytes(b"changed-dependency==1\n")
	exit_code, result = invoke(request(tmp_path, edit_cache, "src/example.py"))
	assert (exit_code, result["reason"]) == (2, "runtime")


@pytest.mark.parametrize("failure", ["invalid", "unsupported", "runtime"])
def test_manifest_failures_never_fall_back_to_baseline(
	tmp_path: Path, edit_cache: Path, failure: str
) -> None:
	path = managed(
		tmp_path,
		edit_cache,
		version="v2.3.0" if failure == "unsupported" else "v2.4.2",
		setup=False,
	)
	if failure == "invalid":
		(tmp_path / "quality-gate.toml").write_text(
			'[secret = "synthetic-sensitive-value"', encoding="utf-8"
		)
	exit_code, result = invoke(request(tmp_path, edit_cache, "src/example.py"))
	assert (exit_code, result["reason"]) == (
		2,
		{"invalid": "manifest", "unsupported": "unsupported_policy", "runtime": "runtime"}[failure],
	)
	assert path.read_bytes() == b"value=  1\n"
	assert "synthetic-sensitive-value" not in json.dumps(result)


def test_unrelated_unprepared_component_does_not_block_managed_edit(
	tmp_path: Path, edit_cache: Path
) -> None:
	managed(tmp_path, edit_cache)
	manifest = tmp_path / "quality-gate.toml"
	with manifest.open("a", encoding="utf-8") as stream:
		stream.write("""
[[python]]
name = "unprepared"
path = "other"
python_version = "3.11"
dependency_inputs = []
test_paths = []
tests_applicable = false
tests_reason = "Unrelated component is deliberately not prepared."
""")
	(tmp_path / "other").mkdir()
	exit_code, result = invoke(request(tmp_path, edit_cache, "src/example.py"))
	assert exit_code == 0, result
	assert result["files"][0]["coverage"] == "managed"
	assert len(result["selections"]) == 1


def test_version_probe_never_runs_installed_startup_hooks(tmp_path: Path, edit_cache: Path) -> None:
	managed(tmp_path, edit_cache)
	manifest = load_manifest(tmp_path)
	manager = RuntimeManager(edit_cache)
	runtime = manager.path_for(tmp_path, runtime_identity(tmp_path, manifest, manifest.python[0]))
	site = runtime / (
		"Lib/site-packages"
		if os.name == "nt"
		else f"lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages"
	)
	marker = tmp_path / "startup-executed.txt"
	(site / "edit_probe_side_effect.pth").write_text(
		f"import pathlib; pathlib.Path({str(marker)!r}).write_bytes(b'executed')\n",
		encoding="utf-8",
	)
	exit_code, result = invoke(request(tmp_path, edit_cache, "src/example.py"))
	assert exit_code == 0, result
	assert not marker.exists(), "Version probing must not execute installed project startup hooks"


@pytest.mark.skipif(os.name == "nt", reason="POSIX Python executable symlink layout")
def test_baseline_interpreter_symlink_is_supported(tmp_path: Path, edit_cache: Path) -> None:
	(tmp_path / "example.py").write_bytes(b"value=  1\n")
	python = tmp_path / "python-link"
	python.symlink_to(sys.executable)
	payload = request(tmp_path, edit_cache, "example.py")
	payload["baseline"]["python"] = str(python)
	exit_code, result = invoke(payload)
	assert exit_code == 0, result
	assert result["selections"][0]["python"] == str(python)
	assert result["selections"][0]["python_sha256"] == digest(Path(sys.executable).read_bytes())


def test_interpreter_change_during_probe_invalidates_result(
	tmp_path: Path, edit_cache: Path
) -> None:
	(tmp_path / "example.py").write_bytes(b"value=  1\n")
	python = tmp_path / ("runtime.cmd" if os.name == "nt" else "runtime")
	response = json.dumps(
		{"version": f"{sys.version_info.major}.{sys.version_info.minor}", "executable": str(python)}
	)
	runtime_wrapper(
		tmp_path,
		f"from pathlib import Path\nPath({str(python)!r}).write_bytes(b'changed\\n')\n"
		f"print({response!r})\n",
	)
	payload = request(tmp_path, edit_cache, "example.py")
	payload["baseline"]["python"] = str(python)
	exit_code, result = invoke(payload)
	assert (exit_code, result["reason"]) == (2, "stale_input"), result
	assert (tmp_path / "example.py").read_bytes() == b"value=  1\n"


def test_working_tree_is_checked_without_reading_or_rewriting_index(
	tmp_path: Path, edit_cache: Path
) -> None:
	path = tmp_path / "example.py"
	staged = b"value = 1\n"
	path.write_bytes(staged)
	for arguments in (["init"], ["add", "example.py"]):
		process = subprocess.run(
			["git", "-C", str(tmp_path), *arguments], capture_output=True, check=False
		)
		assert process.returncode == 0
	index_before = (tmp_path / ".git/index").read_bytes()
	path.write_bytes(b"import os\nvalue=  2\n")
	exit_code, result = invoke(request(tmp_path, edit_cache, "example.py"))
	assert exit_code == 0, result
	assert result["files"][0]["findings"] == []
	assert path.read_bytes() == b"value = 2\n"
	assert (tmp_path / ".git/index").read_bytes() == index_before
	process = subprocess.run(
		["git", "-C", str(tmp_path), "show", ":example.py"], capture_output=True, check=False
	)
	assert process.returncode == 0
	assert process.stdout == staged


def test_empty_non_python_duplicate_and_policy_excluded_inputs(
	tmp_path: Path, edit_cache: Path
) -> None:
	assert invoke(request(tmp_path, edit_cache))[1]["status"] == "not_applicable"
	(tmp_path / "text.txt").write_bytes(b"ordinary text\n")
	assert invoke(request(tmp_path, edit_cache, "text.txt"))[1]["status"] == "not_applicable"
	(tmp_path / ".venv").mkdir()
	path = tmp_path / ".venv/example.py"
	path.write_bytes(b"value=  1\n")
	exit_code, result = invoke(
		request(tmp_path, edit_cache, ".venv/example.py", ".venv/example.py")
	)
	assert exit_code == 0
	assert result["status"] == "not_applicable"
	assert len(result["files"]) == 1
	assert path.read_bytes() == b"value=  1\n"


@pytest.mark.parametrize(
	"failure", ["escape", "directory", "deleted", "file_size", "count", "configuration", "findings"]
)
def test_bounded_input_failures_never_publish(
	tmp_path: Path, edit_cache: Path, failure: str
) -> None:
	path = tmp_path / "example.py"
	path.write_bytes(b"value=  1\n")
	payload = request(tmp_path, edit_cache, "example.py")
	expected = "limits"
	if failure == "escape":
		payload["files"] = [{"path": "../escape.py", "sha256": "0" * 64}]
		expected = "unsafe_path"
	elif failure == "directory":
		payload["files"] = [{"path": "directory", "sha256": "0" * 64}]
		(tmp_path / "directory").mkdir()
		expected = "unsafe_path"
	elif failure == "deleted":
		path.unlink()
		expected = "io"
	elif failure == "file_size":
		path.write_bytes(b"#" * (128 * 1024 + 1))
		payload = request(tmp_path, edit_cache, "example.py")
	elif failure == "count":
		payload["files"] *= 11
	elif failure == "configuration":
		(tmp_path / "pyproject.toml").write_bytes(b"#" * (256 * 1024 + 1))
	elif failure == "findings":
		path.write_bytes(b"assert True\n" * 201)
		payload = request(tmp_path, edit_cache, "example.py")
	before = path.read_bytes() if path.exists() else None
	exit_code, result = invoke(payload)
	assert (exit_code, result["reason"]) == (2, expected), result
	assert (path.read_bytes() if path.exists() else None) == before


def test_syntax_error_is_redacted_and_does_not_prevent_other_file_formatting(
	tmp_path: Path, edit_cache: Path
) -> None:
	(tmp_path / "broken.py").write_bytes(b'value = "synthetic-sensitive-value\n')
	(tmp_path / "valid.py").write_bytes(b"value=  1\n")
	exit_code, result = invoke(request(tmp_path, edit_cache, "broken.py", "valid.py"))
	assert exit_code == 1, (result.get("reason"), result.get("stage"))
	assert result["files"][0]["status"] == "failed"
	assert result["files"][1]["format"] == "passed"
	assert (tmp_path / "valid.py").read_bytes() == b"value = 1\n"
	assert "synthetic-sensitive-value" not in json.dumps(result)


@pytest.mark.parametrize(
	"failure",
	["release", "tool", "python", "version", "missing_baseline", "config", "missing_release"],
)
def test_missing_and_corrupt_prerequisites_are_unchecked(
	tmp_path: Path, edit_cache: Path, failure: str
) -> None:
	(tmp_path / "example.py").write_bytes(b"value=  1\n")
	payload = request(tmp_path, edit_cache, "example.py")
	expected = "runtime"
	cache = tmp_path / "cache"
	if failure in {"release", "tool"}:
		shutil.copytree(edit_cache / "releases", cache / "releases")
		shutil.copytree(edit_cache / "edit-tools", cache / "edit-tools")
		payload["cache_dir"] = str(cache)
	exit_code, fresh = invoke(payload)
	assert exit_code == 0 and fresh["process_count"] == FRESH_PROCESSES, fresh
	payload["files"][0]["sha256"] = digest((tmp_path / "example.py").read_bytes())
	payload["deadline_unix_ms"] = int(time.time() * 1000) + 10_000
	exit_code, reused = invoke(payload)
	assert exit_code == 0 and reused["process_count"] == REUSED_PROCESSES, reused
	payload["deadline_unix_ms"] = int(time.time() * 1000) + 10_000
	if failure == "missing_release":
		payload["cache_dir"] = str(tmp_path / "missing-cache")
		expected = "release"
	elif failure in {"release", "tool"}:
		if failure == "release":
			(cache / "releases/v2.4.2/quality_gate/policy/ruff.toml").write_bytes(b"corrupt")
		else:
			next((cache / "edit-tools").glob("*/*")).write_bytes(b"corrupt")
		expected = failure
	elif failure == "python":
		payload["baseline"]["python"] = str(tmp_path / "missing-python")
	elif failure == "version":
		payload["baseline"]["python_version"] = "3.99"
	elif failure == "missing_baseline":
		del payload["baseline"]
		expected = "invalid_request"
	elif failure == "config":
		(tmp_path / "pyproject.toml").write_text(
			'[[secret = "synthetic-sensitive-value"', encoding="utf-8"
		)
		expected = "configuration"
	exit_code, result = invoke(payload)
	assert (exit_code, result["reason"]) == (2, expected), result
	assert (tmp_path / "example.py").read_bytes() == b"value = 1\n"
	assert "synthetic-sensitive-value" not in json.dumps(result)


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable permission bits")
def test_nonexecutable_tool_invalidates_a_cached_verdict(tmp_path: Path, edit_cache: Path) -> None:
	cache = tmp_path / "cache"
	shutil.copytree(edit_cache / "releases", cache / "releases")
	shutil.copytree(edit_cache / "edit-tools", cache / "edit-tools")
	path = tmp_path / "example.py"
	path.write_bytes(b"value = 1\n")
	for expected_processes in (FRESH_PROCESSES, REUSED_PROCESSES):
		exit_code, result = invoke(request(tmp_path, cache, "example.py"))
		assert exit_code == 0 and result["process_count"] == expected_processes, result
	next((cache / "edit-tools").glob("*/*")).chmod(0o600)
	exit_code, result = invoke(request(tmp_path, cache, "example.py"))
	assert (exit_code, result["reason"]) == (2, "tool"), result
	assert path.read_bytes() == b"value = 1\n"


def runtime_wrapper(root: Path, body: str) -> Path:
	wrapper = root / ("runtime.cmd" if os.name == "nt" else "runtime")
	helper = root / "runtime_helper.py"
	helper.write_text(body, encoding="utf-8")
	if os.name == "nt":
		wrapper.write_text(f'@"{sys.executable}" "{helper}"\n', encoding="utf-8")
	else:
		wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{helper}"\n', encoding="utf-8")
		wrapper.chmod(0o700)
	return wrapper


def process_alive(pid: int) -> bool:
	if os.name == "nt":
		import ctypes
		from ctypes import wintypes

		kernel = ctypes.WinDLL("kernel32", use_last_error=True)
		kernel.OpenProcess.restype = wintypes.HANDLE
		kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
		kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
		kernel.CloseHandle.argtypes = [wintypes.HANDLE]
		handle = kernel.OpenProcess(0x1000, False, pid)
		if not handle:
			return False
		try:
			code = wintypes.DWORD()
			assert kernel.GetExitCodeProcess(handle, ctypes.byref(code))
			return code.value == STILL_ACTIVE
		finally:
			kernel.CloseHandle(handle)
	try:
		os.kill(pid, 0)
	except ProcessLookupError:
		return False
	return True


def test_deadline_terminates_owned_runtime_descendants(tmp_path: Path, edit_cache: Path) -> None:
	(tmp_path / "example.py").write_bytes(b"value=  1\n")
	marker = tmp_path / "child.pid"
	wrapper = runtime_wrapper(
		tmp_path,
		f"""import subprocess, sys, time
from pathlib import Path
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
Path({str(marker)!r}).write_text(str(child.pid))
time.sleep(30)
""",
	)
	payload = request(tmp_path, edit_cache, "example.py")
	payload["baseline"]["python"] = str(wrapper)
	deadline_seconds = 5
	payload["deadline_unix_ms"] = int(time.time() * 1000) + deadline_seconds * 1000
	started = time.monotonic()
	exit_code, result = invoke(payload)
	assert marker.exists(), ("The owned descendant must have started before timeout", result)
	pid = int(marker.read_text())
	try:
		assert (exit_code, result["reason"]) == (2, "deadline")
		assert time.monotonic() - started < deadline_seconds + TIMEOUT_WITH_CLEANUP_SECONDS
		assert not process_alive(pid)
		assert (tmp_path / "example.py").read_bytes() == b"value=  1\n"
		assert not list(tmp_path.glob("temp/qg-*"))
	finally:
		if process_alive(pid):
			os.kill(pid, signal.SIGTERM)


@pytest.mark.parametrize(
	"failure", ["json", "output_limit", "source_changed", "configuration_changed"]
)
def test_runtime_boundary_and_freshness_fail_closed(
	tmp_path: Path, edit_cache: Path, failure: str
) -> None:
	(tmp_path / "example.py").write_bytes(b"value=  1\n")
	(tmp_path / "pyproject.toml").write_bytes(b"# original\n")
	wrapper_name = "runtime.cmd" if os.name == "nt" else "runtime"
	response = json.dumps(
		{
			"version": f"{sys.version_info.major}.{sys.version_info.minor}",
			"executable": str(tmp_path / wrapper_name),
		}
	)
	body = f"print({response!r})\n"
	expected = "stale_input"
	if failure == "json":
		body = "print('synthetic-sensitive-value')\n"
		expected = "runtime"
	elif failure == "output_limit":
		body = "print('x' * (512 * 1024 + 1))\n"
		expected = "limits"
	else:
		target = "example.py" if failure == "source_changed" else "pyproject.toml"
		body = (
			f"from pathlib import Path\nPath({str(tmp_path / target)!r})"
			".write_bytes(b'# concurrent change\\n')\n" + body
		)
	wrapper = runtime_wrapper(tmp_path, body)
	payload = request(tmp_path, edit_cache, "example.py")
	payload["baseline"]["python"] = str(wrapper)
	exit_code, result = invoke(payload)
	assert (exit_code, result["reason"]) == (2, expected), result
	assert "synthetic-sensitive-value" not in json.dumps(result)
	assert (tmp_path / "example.py").read_bytes() == (
		b"# concurrent change\n" if failure == "source_changed" else b"value=  1\n"
	)


def test_policy_change_during_check_invalidates_result(tmp_path: Path, edit_cache: Path) -> None:
	cache = tmp_path / "cache"
	shutil.copytree(edit_cache / "releases", cache / "releases")
	shutil.copytree(edit_cache / "edit-tools", cache / "edit-tools")
	(tmp_path / "example.py").write_bytes(b"value=  1\n")
	policy = cache / "releases/v2.4.2/quality_gate/policy/ruff.toml"
	wrapper = tmp_path / ("runtime.cmd" if os.name == "nt" else "runtime")
	response = json.dumps(
		{
			"version": f"{sys.version_info.major}.{sys.version_info.minor}",
			"executable": str(wrapper),
		}
	)
	runtime_wrapper(
		tmp_path,
		f"from pathlib import Path\npolicy = Path({str(policy)!r})\n"
		"policy.write_bytes(policy.read_bytes() + b'\\n# changed concurrently\\n')\n"
		f"print({response!r})\n",
	)
	payload = request(tmp_path, cache, "example.py")
	payload["baseline"]["python"] = str(wrapper)
	exit_code, result = invoke(payload)
	assert (exit_code, result["reason"]) == (2, "stale_input"), result
	assert (tmp_path / "example.py").read_bytes() == b"value=  1\n"


@pytest.mark.parametrize("damage", ["missing", "wrong_digest"])
def test_setup_verifies_the_pinned_wheel_native_record(
	tmp_path: Path, edit_cache: Path, damage: str
) -> None:
	cache = tmp_path / "cache"
	shutil.copytree(edit_cache / "releases", cache / "releases")
	wheel = next((cache / "releases/v2.4.2").glob("ruff-*.whl"))
	before = digest(wheel.read_bytes())
	with zipfile.ZipFile(wheel) as archive:
		members = {item.filename: archive.read(item) for item in archive.infolist()}
	record = next(name for name in members if name.endswith(".dist-info/RECORD"))
	if damage == "missing":
		del members[record]
	else:
		lines = members[record].decode().splitlines()
		members[record] = (
			"\n".join(
				line.split(",")[0] + ",sha256=" + "A" * 43 + "," + line.split(",")[2]
				if line.split(",")[0].endswith(("/ruff", "/ruff.exe"))
				else line
				for line in lines
			)
			+ "\n"
		).encode()
	with zipfile.ZipFile(wheel, "w", compression=zipfile.ZIP_DEFLATED) as archive:
		for name, content in members.items():
			archive.writestr(name, content)
	manifest = cache / "releases/v2.4.2/release.toml"
	manifest.write_text(
		manifest.read_text().replace(before, digest(wheel.read_bytes())), encoding="utf-8"
	)
	process = subprocess.run(
		[
			sys.executable,
			"-B",
			"-m",
			"quality_gate.cli",
			"edit-setup",
			"--policy-release",
			"v2.4.2",
			"--cache-dir",
			str(cache),
		],
		cwd=REPOSITORY,
		capture_output=True,
		check=False,
		timeout=15,
	)
	assert process.returncode == EXIT_UNCHECKED
	assert b"unchecked" in process.stdout
	assert process.stderr == b""


def test_result_delivery_obeys_the_same_deadline(tmp_path: Path, edit_cache: Path) -> None:
	(tmp_path / "example.py").write_bytes(b"assert True\n" * 180)
	payload = request(tmp_path, edit_cache, "example.py")
	payload["deadline_unix_ms"] = int(time.time() * 1000) + 4000
	process = subprocess.Popen(
		[sys.executable, "-B", "-m", "quality_gate.cli", "edit-check"],
		cwd=REPOSITORY,
		stdin=subprocess.PIPE,
		stdout=subprocess.PIPE,
		stderr=subprocess.PIPE,
		pipesize=4096 if sys.platform == "linux" else -1,
	)
	assert process.stdin is not None
	process.stdin.write(json.dumps(payload).encode())
	process.stdin.close()
	try:
		try:
			exit_code = process.wait(timeout=6)
		except subprocess.TimeoutExpired:
			pytest.fail("Result delivery blocked beyond the invocation deadline")
		assert exit_code == EXIT_UNCHECKED
		assert not list(tmp_path.glob("temp/qg-*"))
	finally:
		if process.poll() is None:
			process.kill()
		process.wait(timeout=2)
		if process.stdout is not None:
			process.stdout.close()
		if process.stderr is not None:
			process.stderr.close()


def test_unicode_spaces_duplicate_and_noop_preserve_content_identity(
	tmp_path: Path, edit_cache: Path
) -> None:
	root = tmp_path / "проект с пробелами"
	root.mkdir()
	path = root / "example.py"
	path.write_bytes(b"value = 1\n")
	before = path.stat().st_mtime_ns
	exit_code, result = invoke(request(root, edit_cache, "example.py", "example.py"))
	assert exit_code == 0, result
	assert len(result["files"]) == 1
	assert result["files"][0]["changed"] is False
	assert result["files"][0]["sha256"] == digest(b"value = 1\n")
	assert path.stat().st_mtime_ns == before


def test_gitignore_explicit_file_semantics_and_working_tree_identity(
	tmp_path: Path, edit_cache: Path
) -> None:
	process = subprocess.run(["git", "init", str(tmp_path)], capture_output=True, check=False)
	assert process.returncode == 0
	path = tmp_path / "example.py"
	path.write_bytes(b"value=  1\n")
	ignore = tmp_path / ".gitignore"
	ignore.write_bytes(b"example.py\n")
	tool = next((edit_cache / "edit-tools").glob("*/ruff.exe" if os.name == "nt" else "*/ruff"))
	discovery = subprocess.run(
		[
			str(tool),
			"check",
			"--config",
			str(edit_cache / "releases/v2.4.2/quality_gate/policy/ruff.toml"),
			"--no-cache",
			"--force-exclude",
			"--show-files",
			"--",
			str(path),
		],
		cwd=tmp_path,
		capture_output=True,
		check=False,
		timeout=10,
	)
	assert discovery.returncode == 0
	assert Path(discovery.stdout.decode().strip()).resolve() == path.resolve()
	exit_code, result = invoke(request(tmp_path, edit_cache, "example.py"))
	assert exit_code == 0
	assert result["status"] == "passed"
	assert path.read_bytes() == b"value = 1\n"
	first_identity = next(
		item["sha256"] for item in result["inputs"] if item["path"] == ".gitignore"
	)
	ignore.write_bytes(b"# no ignored source\n")
	exit_code, result = invoke(request(tmp_path, edit_cache, "example.py"))
	assert exit_code == 0, result
	assert result["status"] == "passed"
	assert (
		next(item["sha256"] for item in result["inputs"] if item["path"] == ".gitignore")
		!= first_identity
	)
	assert path.read_bytes() == b"value = 1\n"


@pytest.mark.parametrize("baseline", [None, {}, {"policy_release": "v2.4.2"}])
def test_malformed_optional_baseline_is_rejected(tmp_path: Path, baseline: object) -> None:
	payload = {
		"schema": 1,
		"root": str(tmp_path),
		"files": [],
		"deadline_unix_ms": int(time.time() * 1000) + 5000,
		"baseline": baseline,
	}
	exit_code, result = invoke(payload)
	assert (exit_code, result["reason"]) == (2, "invalid_request")


def test_junction_or_symlink_escape_is_rejected(tmp_path: Path, edit_cache: Path) -> None:
	outside = tmp_path / "outside"
	outside.mkdir()
	(outside / "example.py").write_bytes(b"value=  1\n")
	root = tmp_path / "project"
	root.mkdir()
	link = root / "link"
	if os.name == "nt":
		junction_result = subprocess.run(
			["cmd", "/c", "mklink", "/J", str(link), str(outside)], capture_output=True, check=False
		)
		assert junction_result.returncode == 0
	else:
		link.symlink_to(outside, target_is_directory=True)
	try:
		payload = request(root, edit_cache, "link/example.py")
		exit_code, result = invoke(payload)
		assert (exit_code, result["reason"]) == (2, "unsafe_path")
		assert (outside / "example.py").read_bytes() == b"value=  1\n"
	finally:
		if os.name == "nt":
			link.rmdir()
		else:
			link.unlink()
