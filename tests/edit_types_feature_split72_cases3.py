"""Feature test partition."""

from __future__ import annotations

from edit_types_feature_split72_support import (
	_MANAGED_RUNTIME_PTH_SHA256 as _MANAGED_RUNTIME_PTH_SHA256,
)
from edit_types_feature_split72_support import (
	BACKGROUND_SCHEMA as BACKGROUND_SCHEMA,
)
from edit_types_feature_split72_support import (
	MEASUREMENT_CASES as MEASUREMENT_CASES,
)
from edit_types_feature_split72_support import (
	MYPY_DEPENDENCIES as MYPY_DEPENDENCIES,
)
from edit_types_feature_split72_support import (
	REPOSITORY as REPOSITORY,
)
from edit_types_feature_split72_support import (
	STANDARD_MANAGED_RUNTIME_PTH as STANDARD_MANAGED_RUNTIME_PTH,
)
from edit_types_feature_split72_support import (
	Any as Any,
)
from edit_types_feature_split72_support import (
	ClassVar as ClassVar,
)
from edit_types_feature_split72_support import (
	Configuration as Configuration,
)
from edit_types_feature_split72_support import (
	EditError as EditError,
)
from edit_types_feature_split72_support import (
	Iterator as Iterator,
)
from edit_types_feature_split72_support import (
	Path as Path,
)
from edit_types_feature_split72_support import (
	PythonComponent as PythonComponent,
)
from edit_types_feature_split72_support import (
	ReleaseManifest as ReleaseManifest,
)
from edit_types_feature_split72_support import (
	RuntimeManager as RuntimeManager,
)
from edit_types_feature_split72_support import (
	SourceFile as SourceFile,
)
from edit_types_feature_split72_support import (
	ValidationError as ValidationError,
)
from edit_types_feature_split72_support import (
	_argus_typing_manifest as _argus_typing_manifest,
)
from edit_types_feature_split72_support import (
	_assert_candidate_stale_mutation as _assert_candidate_stale_mutation,
)
from edit_types_feature_split72_support import (
	_candidate_mypy_dependency_replacements as _candidate_mypy_dependency_replacements,
)
from edit_types_feature_split72_support import (
	_candidate_typing_cache_parent as _candidate_typing_cache_parent,
)
from edit_types_feature_split72_support import (
	_candidate_worker_guard as _candidate_worker_guard,
)
from edit_types_feature_split72_support import (
	_import_context as _import_context,
)
from edit_types_feature_split72_support import (
	_local_stub_roots as _local_stub_roots,
)
from edit_types_feature_split72_support import (
	_local_typed_dependency as _local_typed_dependency,
)
from edit_types_feature_split72_support import (
	_mypy_environment as _mypy_environment,
)
from edit_types_feature_split72_support import (
	_prepare_argus_layout as _prepare_argus_layout,
)
from edit_types_feature_split72_support import (
	_prepare_candidate_runtime as _prepare_candidate_runtime,
)
from edit_types_feature_split72_support import (
	_run_candidate_types as _run_candidate_types,
)
from edit_types_feature_split72_support import (
	_StubSelection as _StubSelection,
)
from edit_types_feature_split72_support import (
	_typed_dependency as _typed_dependency,
)
from edit_types_feature_split72_support import (
	_TypingInvocation as _TypingInvocation,
)
from edit_types_feature_split72_support import (
	_update_candidate_release_digests as _update_candidate_release_digests,
)
from edit_types_feature_split72_support import (
	_validate_site as _validate_site,
)
from edit_types_feature_split72_support import (
	argus_project as argus_project,
)
from edit_types_feature_split72_support import (
	asdict as asdict,
)
from edit_types_feature_split72_support import (
	cache_from_source as cache_from_source,
)
from edit_types_feature_split72_support import (
	candidate_engine_wheel as candidate_engine_wheel,
)
from edit_types_feature_split72_support import (
	candidate_typing_cache as candidate_typing_cache,
)
from edit_types_feature_split72_support import (
	cProfile as cProfile,
)
from edit_types_feature_split72_support import (
	default_cache_root as default_cache_root,
)
from edit_types_feature_split72_support import (
	digest as digest,
)
from edit_types_feature_split72_support import (
	environment_tree as environment_tree,
)
from edit_types_feature_split72_support import (
	execute as execute,
)
from edit_types_feature_split72_support import (
	full_component_diagnostics as full_component_diagnostics,
)
from edit_types_feature_split72_support import (
	hashlib as hashlib,
)
from edit_types_feature_split72_support import (
	inventory as inventory,
)
from edit_types_feature_split72_support import (
	invoke as invoke,
)
from edit_types_feature_split72_support import (
	invoke_candidate_wheel as invoke_candidate_wheel,
)
from edit_types_feature_split72_support import (
	is_link_or_reparse as is_link_or_reparse,
)
from edit_types_feature_split72_support import (
	json as json,
)
from edit_types_feature_split72_support import (
	load_manifest as load_manifest,
)
from edit_types_feature_split72_support import (
	manifest_text as manifest_text,
)
from edit_types_feature_split72_support import (
	os as os,
)
from edit_types_feature_split72_support import (
	parse_manifest as parse_manifest,
)
from edit_types_feature_split72_support import (
	py_compile as py_compile,
)
from edit_types_feature_split72_support import (
	pytest as pytest,
)
from edit_types_feature_split72_support import (
	re as re,
)
from edit_types_feature_split72_support import (
	request as request,
)
from edit_types_feature_split72_support import (
	request_files as request_files,
)
from edit_types_feature_split72_support import (
	runtime_fingerprint as runtime_fingerprint,
)
from edit_types_feature_split72_support import (
	runtime_identity as runtime_identity,
)
from edit_types_feature_split72_support import (
	shutil as shutil,
)
from edit_types_feature_split72_support import (
	subprocess as subprocess,
)
from edit_types_feature_split72_support import (
	sys as sys,
)
from edit_types_feature_split72_support import (
	tempfile as tempfile,
)
from edit_types_feature_split72_support import (
	threading as threading,
)
from edit_types_feature_split72_support import (
	time as time,
)
from edit_types_feature_split72_support import (
	typing_environment as typing_environment,
)
from edit_types_feature_split72_support import (
	typing_policy as typing_policy,
)
from edit_types_feature_split72_support import (
	uuid as uuid,
)
from edit_types_feature_split72_support import (
	verify_release as verify_release,
)
from edit_types_feature_split72_support import (
	zipfile as zipfile,
)


def test_real_managed_component_includes_visible_build_and_imported_types(
	tmp_path: Path, typing_policy: Path, typing_environment: Path
) -> None:
	(root := tmp_path / "app").mkdir()
	(root / "build").mkdir()
	(root / "models.py").write_text("Value = str\n", encoding="utf-8")
	(root / "build/consumer.py").write_text(
		"from models import Value\nvalue: Value = 123\n", encoding="utf-8"
	)
	(tmp_path / "requirements.txt").write_bytes(b"")
	(tmp_path / "requirements-dev.txt").write_bytes(b"# fixture dependencies\n")
	(tmp_path / "quality-gate.toml").write_text(
		f'''[quality]
schema = 2
policy_release = "{typing_policy.name}"
[repository]
name = "typing-managed"
domains = ["repository", "python"]
required_documents = ["quality-gate.toml"]
[[python]]
name = "app"
path = "app"
python_version = "{sys.version_info.major}.{sys.version_info.minor}"
dependency_inputs = ["requirements.txt", "requirements-dev.txt", "requirements.txt"]
test_paths = []
tests_applicable = false
tests_reason = "Disposable typing integration."
''',
		encoding="utf-8",
	)
	manifest = load_manifest(tmp_path)
	identity = runtime_identity(tmp_path, manifest, manifest.python[0])
	runtime = RuntimeManager(typing_policy.parent.parent).path_for(tmp_path, identity)
	try:
		shutil.copytree(typing_environment, runtime, symlinks=True)
		(runtime / "runtime.json").write_text(
			json.dumps(asdict(identity) | {"fingerprint": runtime_fingerprint(identity)}),
			encoding="utf-8",
		)
		code, result = invoke(request(tmp_path, typing_policy, "app/models.py", background=True))
		assert (code, result["status"]) == (1, "failed"), json.dumps(result)
		component = result["components"][0]
		assert component["coverage"] == "managed"
		assert component["dependency_inputs"] == [
			{"path": "requirements-dev.txt", "sha256": digest(b"# fixture dependencies\n")},
			{"path": "requirements.txt", "sha256": digest(b"")},
		]
		assert {item["path"] for item in component["inventory"]} == {
			"app/models.py",
			"app/build/consumer.py",
		}
		assert component["diagnostics"][0]["path"] == "app/build/consumer.py"
		assert component["diagnostics"] == full_component_diagnostics(
			tmp_path,
			typing_policy,
			"app",
			runtime / ("Scripts/python.exe" if os.name == "nt" else "bin/python"),
		)
	finally:
		if runtime.exists():
			shutil.rmtree(runtime)


def test_candidate_wheel_checks_argus_sibling_sources_and_local_stubs(
	argus_project: Path,
	candidate_typing_cache: Path,
	candidate_engine_wheel: Path,
) -> None:
	code, result = _run_candidate_types(
		argus_project,
		candidate_typing_cache,
		candidate_engine_wheel,
		"scripts/rendering.py",
		network_guard=True,
	)
	assert (code, result["status"]) == (1, "failed"), json.dumps(result, indent=2)
	assert result["engine_version"] == "4.7.0"
	assert [record["name"] for record in result["components"]] == ["bot", "worker"]
	component = result["components"][0]
	assert {item["path"] for item in component["inventory"]} == {
		"bot/__init__.py",
		"bot/main.py",
		"bot/stubs/local_api.pyi",
		"scripts/__init__.py",
		"scripts/rendering.py",
		"apscheduler/__init__.pyi",
		"apscheduler/schedulers/__init__.pyi",
		"apscheduler/schedulers/asyncio.pyi",
	}
	assert component["diagnostics"] == [
		{"path": "bot/main.py", "line": 5, "column": 21, "code": "arg-type"}
	]
	assert result["selections"][0]["mypy_version"] == "2.3.1"
	policy = candidate_typing_cache / "releases/v4.7.0"
	candidate = verify_release(policy)
	mypy = next(item for item in candidate.tools if item.name == "mypy")
	assert result["selections"][0]["mypy_sha256"] == digest((policy / mypy.path).read_bytes())
	assert "py.typed" in {
		entry["name"]
		for directory in component["directories"]
		if directory["path"] == "apscheduler"
		for entry in directory["entries"]
	}
	assert result["components"][1]["reason"] == "typecheck_disabled"

	(argus_project / "scripts/rendering.py").write_text(
		"def render(value: int) -> str:\n    return str(value)\n", encoding="utf-8"
	)
	code, result = _run_candidate_types(
		argus_project,
		candidate_typing_cache,
		candidate_engine_wheel,
		"scripts/rendering.py",
		network_guard=True,
	)
	assert (code, result["status"]) == (0, "passed"), result
	assert [record["name"] for record in result["components"]] == ["bot", "worker"]
	assert result["components"][0]["diagnostics"] == []
	assert result["components"][1]["reason"] == "typecheck_disabled"
	assert sorted(item.name for item in (candidate_typing_cache / "releases").iterdir()) == [
		"v4.7.0"
	]


def test_candidate_wheel_uses_component_local_stub_with_real_mypy(
	argus_project: Path,
	candidate_typing_cache: Path,
	candidate_engine_wheel: Path,
) -> None:
	(argus_project / "bot/main.py").write_text(
		"from local_api import format_label\nlocal_label: str = format_label(1)\n",
		encoding="utf-8",
	)
	code, result = _run_candidate_types(
		argus_project, candidate_typing_cache, candidate_engine_wheel, "bot/main.py"
	)
	assert (code, result["status"]) == (1, "failed"), (
		result.get("reason"),
		[(item["name"], item["status"], item["diagnostics"]) for item in result["components"]],
	)
	assert result["components"][0]["diagnostics"][0]["code"] == "arg-type"
	assert result["components"][0]["diagnostics"][0]["path"] == "bot/main.py"

	(argus_project / "bot/stubs/local_api.pyi").write_text(
		"def format_label(value: int) -> str: ...\n", encoding="utf-8"
	)
	code, result = _run_candidate_types(
		argus_project, candidate_typing_cache, candidate_engine_wheel, "bot/main.py"
	)
	assert (code, result["status"]) == (0, "passed"), result
	assert result["components"][0]["diagnostics"] == []


def test_candidate_wheel_routes_added_and_removed_py_typed_markers(
	argus_project: Path,
	candidate_typing_cache: Path,
	candidate_engine_wheel: Path,
) -> None:
	(argus_project / "scripts/rendering.py").write_text(
		"def render(value: int) -> str:\n    return str(value)\n", encoding="utf-8"
	)
	marker = argus_project / "apscheduler/py.typed"
	marker.unlink()
	code, result = _run_candidate_types(
		argus_project, candidate_typing_cache, candidate_engine_wheel, "apscheduler/py.typed"
	)
	assert (code, result["status"]) == (0, "passed"), (
		result.get("reason"),
		[
			(item["name"], item["status"], item.get("reason"), item["diagnostics"])
			for item in result["components"]
		],
	)
	assert [record["name"] for record in result["components"]] == ["bot"]
	files = {item["path"]: item for item in result["files"]}
	assert files["apscheduler/py.typed"]["input_sha256"] is None
	assert files["apscheduler/py.typed"]["final_sha256"] is None

	marker.write_bytes(b"partial\n")
	code, result = _run_candidate_types(
		argus_project, candidate_typing_cache, candidate_engine_wheel, "apscheduler/py.typed"
	)
	assert (code, result["status"]) == (0, "passed"), result
	assert [record["name"] for record in result["components"]] == ["bot"]
	configuration = {item["path"]: item["sha256"] for item in result["configuration"]}
	assert configuration["apscheduler/py.typed"] == digest(b"partial\n")

	nested_marker = argus_project / "apscheduler/schedulers/py.typed"
	nested_marker.write_bytes(b"partial\n")
	code, result = _run_candidate_types(
		argus_project,
		candidate_typing_cache,
		candidate_engine_wheel,
		"apscheduler/schedulers/py.typed",
	)
	assert (code, result["status"]) == (0, "passed"), result
	assert [record["name"] for record in result["components"]] == ["bot"]
	configuration = {item["path"]: item["sha256"] for item in result["configuration"]}
	assert configuration["apscheduler/schedulers/py.typed"] == digest(b"partial\n")


@pytest.mark.parametrize("phase", ["write", "validate"])
def test_candidate_wheel_runs_ruff_writer_and_validator_with_candidate_only_cache(
	tmp_path: Path,
	candidate_typing_cache: Path,
	candidate_engine_wheel: Path,
	phase: str,
) -> None:
	project = tmp_path / "ruff-candidate"
	project.mkdir()
	path = project / "example.py"
	original = b"import os\nimport sys\n\nvalue = (os.name, sys.version)\n"
	path.write_bytes(original)
	policy = candidate_typing_cache / "releases/v4.7.0"
	payload = request(project, policy, "example.py")
	payload["baseline"] = {
		"policy_release": "v4.7.0",
		"python": sys.executable,
		"python_version": f"{sys.version_info.major}.{sys.version_info.minor}",
	}
	payload["phase"] = phase
	code, result = invoke_candidate_wheel(payload, candidate_engine_wheel, command="edit-check")
	assert (code, result["status"], result["engine_version"]) == (0, "passed", "4.7.0"), result
	assert result["selections"][0]["policy_release"] == "v4.7.0"
	assert result["files"][0]["format"] == "passed"
	assert result["files"][0]["sha256"] == digest(original)
	assert path.read_bytes() == original
	assert sorted(item.name for item in (candidate_typing_cache / "releases").iterdir()) == [
		"v4.7.0"
	]


def test_candidate_wheel_runs_secrets_with_candidate_only_cache(
	tmp_path: Path,
	candidate_typing_cache: Path,
	candidate_engine_wheel: Path,
) -> None:
	project = tmp_path / "secrets-candidate"
	project.mkdir()
	(project / "settings.py").write_text("token = 'ordinary value'\n", encoding="utf-8")
	policy = candidate_typing_cache / "releases/v4.7.0"
	code, result = invoke_candidate_wheel(
		request(project, policy, "settings.py"), candidate_engine_wheel, command="edit-secrets"
	)
	assert (code, result["status"], result["engine_version"]) == (0, "passed", "4.7.0"), result
	assert result["selections"][0]["policy_release"] == "v4.7.0"
	assert result["selections"][0]["scanner_version"] == "8.30.1"
	assert result["files"][0]["findings"] == []
	assert sorted(item.name for item in (candidate_typing_cache / "releases").iterdir()) == [
		"v4.7.0"
	]


def test_candidate_wheel_runs_integrity_with_candidate_only_cache(
	tmp_path: Path,
	candidate_typing_cache: Path,
	candidate_engine_wheel: Path,
) -> None:
	project = tmp_path / "integrity-candidate"
	project.mkdir()
	(project / "clean.py").write_text("value: int = 1\n", encoding="utf-8")
	policy = candidate_typing_cache / "releases/v4.7.0"
	code, result = invoke_candidate_wheel(
		request(project, policy, "clean.py"), candidate_engine_wheel, command="edit-integrity"
	)
	assert (code, result["status"], result["engine_version"]) == (0, "passed", "4.7.0"), result
	assert result["selections"][0]["policy_release"] == "v4.7.0"
	assert result["files"][0]["status"] == "passed"
	assert sorted(item.name for item in (candidate_typing_cache / "releases").iterdir()) == [
		"v4.7.0"
	]


def test_candidate_wheel_routes_shared_tree_additions_renames_and_configuration(
	argus_project: Path,
	candidate_typing_cache: Path,
	candidate_engine_wheel: Path,
) -> None:
	(argus_project / "scripts/rendering.py").write_text(
		"def render(value: int) -> str:\n    return str(value)\n", encoding="utf-8"
	)
	added = argus_project / "scripts/temporary.py"
	added.write_text("value: int = 1\n", encoding="utf-8")
	code, result = _run_candidate_types(
		argus_project, candidate_typing_cache, candidate_engine_wheel, "scripts/temporary.py"
	)
	assert (code, result["status"]) == (0, "passed"), result
	assert [record["name"] for record in result["components"]] == ["bot", "worker"]
	assert "scripts/temporary.py" in {item["path"] for item in result["components"][0]["inventory"]}

	renamed = argus_project / "scripts/renamed.py"
	added.replace(renamed)
	code, result = _run_candidate_types(
		argus_project,
		candidate_typing_cache,
		candidate_engine_wheel,
		"scripts/temporary.py",
		"scripts/renamed.py",
	)
	assert (code, result["status"]) == (0, "passed"), result
	paths = {item["path"] for item in result["components"][0]["inventory"]}
	assert "scripts/renamed.py" in paths and "scripts/temporary.py" not in paths
	renamed.unlink()
	code, result = _run_candidate_types(
		argus_project, candidate_typing_cache, candidate_engine_wheel, "scripts/renamed.py"
	)
	assert (code, result["status"]) == (0, "passed"), result
	assert "scripts/renamed.py" not in {
		item["path"] for item in result["components"][0]["inventory"]
	}

	initializer = argus_project / "scripts/__init__.py"
	initializer.write_text("# changed package initializer\n", encoding="utf-8")
	code, result = _run_candidate_types(
		argus_project, candidate_typing_cache, candidate_engine_wheel, "scripts/__init__.py"
	)
	assert (code, result["status"]) == (0, "passed"), result
	assert "scripts/__init__.py" in {item["path"] for item in result["components"][0]["inventory"]}
	configuration = argus_project / "scripts/pyproject.toml"
	configuration.write_text("[tool.mypy]\n", encoding="utf-8")
	code, result = _run_candidate_types(
		argus_project, candidate_typing_cache, candidate_engine_wheel, "scripts/pyproject.toml"
	)
	assert (code, result["status"]) == (0, "passed"), result
	assert "scripts/pyproject.toml" in {item["path"] for item in result["configuration"]}
