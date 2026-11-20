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


@pytest.mark.parametrize("namespace", [False, True], ids=["package", "namespace"])
def test_candidate_wheel_checks_import_names_for_nested_source_roots(
	tmp_path: Path,
	typing_environment: Path,
	candidate_typing_cache: Path,
	candidate_engine_wheel: Path,
	namespace: bool,
) -> None:
	project = tmp_path / "argus-nested-source"
	project.mkdir()
	_prepare_argus_layout(project)
	manifest = project / "quality-gate.toml"
	manifest.write_text(
		manifest.read_text(encoding="utf-8").replace(
			'source_roots = ["scripts", "apscheduler"]',
			'source_roots = ["shared/scripts", "apscheduler"]',
		),
		encoding="utf-8",
	)
	shared_scripts = project / "shared/scripts"
	shared_scripts.parent.mkdir()
	(project / "scripts").replace(shared_scripts)
	if namespace:
		(shared_scripts / "__init__.py").unlink()
	else:
		(project / "shared/__init__.py").write_bytes(b"# package parent\n")
	(project / "bot/main.py").write_text(
		"from apscheduler.schedulers.asyncio import AsyncIOScheduler\n"
		"from rendering import render\n"
		"scheduler = AsyncIOScheduler()\n"
		"scheduler.start()\n"
		"label: str = render(1)\n",
		encoding="utf-8",
	)
	runtime = _prepare_candidate_runtime(
		project,
		candidate_typing_cache,
		typing_environment,
	)
	try:
		code, result = _run_candidate_types(
			project,
			candidate_typing_cache,
			candidate_engine_wheel,
			"shared/scripts/rendering.py",
		)
		assert (code, result["status"]) == (1, "failed"), (
			result.get("reason"),
			[(item["name"], item["status"], item["diagnostics"]) for item in result["components"]],
		)
		assert result["components"][0]["diagnostics"] == [
			{"path": "bot/main.py", "line": 5, "column": 21, "code": "arg-type"}
		]
		(shared_scripts / "rendering.py").write_text(
			"def render(value: int) -> str:\n    return str(value)\n", encoding="utf-8"
		)
		code, result = _run_candidate_types(
			project,
			candidate_typing_cache,
			candidate_engine_wheel,
			"shared/scripts/rendering.py",
		)
	finally:
		if runtime.exists():
			shutil.rmtree(runtime)
	assert (code, result["status"]) == (0, "passed"), (
		result.get("reason"),
		[(item["name"], item["status"], item["diagnostics"]) for item in result["components"]],
	)
	assert [record["name"] for record in result["components"]] == ["bot"]
	configuration = {item["path"]: item["sha256"] for item in result["configuration"]}
	assert configuration["shared/__init__.py"] == (
		digest(b"# package parent\n") if not namespace else None
	)


@pytest.mark.parametrize(
	("relative", "replacement", "snapshot_relative"),
	[
		(
			"scripts/rendering.py",
			b"def render(value: object) -> str:\n    return str(value)\n",
			None,
		),
		(
			"apscheduler/schedulers/asyncio.pyi",
			b"class AsyncIOScheduler:\n    def start(self, value: int) -> None: ...\n",
			None,
		),
		(
			"bot/stubs/local_api.pyi",
			b"def format_label(value: int) -> str: ...\n",
			None,
		),
		("apscheduler/py.typed", b"partial\n", None),
		(
			"apscheduler/schedulers/py.typed",
			b"partial\n",
			"apscheduler/py.typed",
		),
		(
			"bot/stubs/added.pyi",
			b"class Added: ...\n",
			"bot/stubs/local_api.pyi",
		),
		(
			"apscheduler/schedulers/added.pyi",
			b"class Added: ...\n",
			"apscheduler/schedulers/asyncio.pyi",
		),
		("apscheduler/schedulers/asyncio.pyi", None, None),
		("bot/stubs/local_api.pyi", None, None),
		("apscheduler/py.typed", None, None),
	],
)
def test_candidate_wheel_rejects_in_flight_shared_and_stub_mutations(
	argus_project: Path,
	candidate_typing_cache: Path,
	candidate_engine_wheel: Path,
	relative: str,
	replacement: bytes | None,
	snapshot_relative: str | None,
) -> None:
	_assert_candidate_stale_mutation(
		argus_project,
		candidate_typing_cache,
		candidate_engine_wheel,
		relative=relative,
		replacement=replacement,
		snapshot_relative=snapshot_relative,
	)


@pytest.mark.parametrize("unsafe", [False, True])
def test_candidate_cli_fails_closed_for_missing_or_linked_source_roots(
	tmp_path: Path,
	candidate_typing_cache: Path,
	candidate_engine_wheel: Path,
	unsafe: bool,
) -> None:
	project = tmp_path / "argus-invalid-root"
	(project / "bot").mkdir(parents=True)
	(project / "bot/main.py").write_text("value: int = 1\n", encoding="utf-8")
	(project / "bot/requirements.txt").write_text("APScheduler==3.10.4\n", encoding="utf-8")
	(project / "bot/requirements-dev.txt").write_text(
		"-r requirements.txt\npytest\n", encoding="utf-8"
	)
	(project / "quality-gate.toml").write_text(_argus_typing_manifest(), encoding="utf-8")
	if unsafe:
		(project / "outside").mkdir()
		try:
			(project / "scripts").symlink_to(project / "outside", target_is_directory=True)
		except OSError as error:
			pytest.skip(f"Directory symlinks are unavailable: {error}")
		(project / "apscheduler").mkdir()
	cache_policy = candidate_typing_cache / "releases/v4.7.0"
	code, result = invoke_candidate_wheel(
		request(project, cache_policy, "bot/main.py", background=True), candidate_engine_wheel
	)
	expected = "unsafe_path" if unsafe else "dependencies"
	assert (code, result["status"], result["reason"]) == (2, "unchecked", expected), result
	assert result["components"] == []


@pytest.mark.parametrize(
	("stub_roots", "expected"),
	[('["missing"]', "dependencies"), ('["../outside"]', "manifest")],
)
def test_candidate_cli_fails_closed_for_missing_or_unsafe_component_stub_roots(
	tmp_path: Path,
	candidate_typing_cache: Path,
	candidate_engine_wheel: Path,
	stub_roots: str,
	expected: str,
) -> None:
	project = tmp_path / "argus-invalid-stub-root"
	project.mkdir()
	_prepare_argus_layout(project)
	manifest = project / "quality-gate.toml"
	manifest.write_text(
		manifest.read_text(encoding="utf-8").replace(
			'stub_roots = ["stubs"]', f"stub_roots = {stub_roots}"
		),
		encoding="utf-8",
	)
	policy = candidate_typing_cache / "releases/v4.7.0"
	code, result = invoke_candidate_wheel(
		request(project, policy, "bot/main.py", background=True), candidate_engine_wheel
	)
	assert (code, result["status"], result["reason"]) == (2, "unchecked", expected), result
	assert result["components"] == []


def test_candidate_cli_does_not_import_undeclared_working_tree_modules(
	argus_project: Path,
	candidate_typing_cache: Path,
	candidate_engine_wheel: Path,
) -> None:
	(argus_project / "unlisted.py").write_text("def helper() -> int: ...\n", encoding="utf-8")
	(argus_project / "bot/main.py").write_text(
		"from unlisted import helper\nvalue: int = helper()\n",
		encoding="utf-8",
	)
	policy = candidate_typing_cache / "releases/v4.7.0"
	code, result = invoke_candidate_wheel(
		request(argus_project, policy, "bot/main.py", background=True),
		candidate_engine_wheel,
	)
	assert (code, result["status"], result["reason"]) == (2, "unchecked", "dependencies"), result
	assert result["components"] == []
	assert sorted(item.name for item in (candidate_typing_cache / "releases").iterdir()) == [
		"v4.7.0"
	]


def test_typing_measurements(tmp_path: Path, typing_policy: Path, typing_environment: Path) -> None:
	"""Opt-in measurement; completed cases and incomplete cases remain distinct."""
	if os.environ.get("QUALITY_GATE_TYPING_BENCHMARK") != "1":
		pytest.skip("Opt-in real-tool latency measurement, separate from behavior verification.")
	shutil.copytree(typing_environment, tmp_path / ".venv", symlinks=True)
	(tmp_path / "api.py").write_text(
		"def accept(value: str) -> str:\n    return value\n", encoding="utf-8"
	)
	(tmp_path / "consumer.py").write_text(
		"from api import accept\nresult = accept(123)\n", encoding="utf-8"
	)
	records = []
	for label in ("cold", "warm-unchanged-1", "warm-unchanged-2", "invalidation"):
		if label == "invalidation":
			(tmp_path / "api.py").write_text(
				"def accept(value: int) -> int:\n    return value\n", encoding="utf-8"
			)
		started = time.monotonic()
		payload = request(tmp_path, typing_policy, "api.py", background=True)
		code, result = invoke(payload)
		wall_ms = round((time.monotonic() - started) * 1000)
		assert (code, result["status"]) == (
			(0, "passed") if label == "invalidation" else (1, "failed")
		), result
		selection = result["selections"][0]
		validation_started = time.monotonic()
		# The host owns its independent implementation. This measures the agreed
		# filesystem work, not installed host delivery or aggregate hook latency.
		assert (
			environment_tree(
				Path(selection["environment"]["path"]),
				Path(selection["base_python"]),
				time.monotonic() + 10,
			)
			== selection["environment"]
		)
		inventory(tmp_path, ".", managed=False, deadline=time.monotonic() + 10)
		for record in result["configuration"]:
			path = tmp_path / record["path"]
			assert (digest(path.read_bytes()) if path.is_file() else None) == record["sha256"]
		assert request_files(tmp_path, payload["files"]) == result["files"]
		validation_ms = round((time.monotonic() - validation_started) * 1000)
		records.append(
			{
				"case": label,
				"exit_code": code,
				"status": result["status"],
				"reason": result["reason"],
				"wall_ms": wall_ms,
				"equivalent_delivery_rehash_ms": validation_ms,
				"provider_plus_rehash_ms": wall_ms + validation_ms,
				"provider_ms": result["elapsed_ms"],
				"selections": result["selections"],
				"components": [
					{
						key: component[key]
						for key in ("name", "status", "source_count", "import_count", "analysis_ms")
					}
					for component in result["components"]
				],
			}
		)
	python = tmp_path / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
	startup = time.monotonic()
	version = subprocess.run(
		[str(python), "-I", "-B", "-m", "mypy", "--version"],
		cwd=tmp_path,
		capture_output=True,
		check=False,
		timeout=10,
	)
	assert version.returncode == 0 and version.stdout.startswith(b"mypy 2.3.1")
	startup_ms = round((time.monotonic() - startup) * 1000)
	graph: dict[str, int] = {}
	assert full_component_diagnostics(tmp_path, typing_policy, ".", metrics=graph) == []
	from quality_gate.temp_workspace import temporary_workspace

	with temporary_workspace(tmp_path) as directory:
		profiler = cProfile.Profile()
		profiled = profiler.runcall(
			execute,
			request(tmp_path, typing_policy, "api.py", background=True),
			time.monotonic() + 30,
			Path(directory),
		)
		assert profiled["status"] == "passed"
	phases = [
		{"name": item.code.co_name, "calls": item.callcount, "seconds": item.totaltime}
		for item in profiler.getstats()
		if not isinstance(item.code, str)
		and item.code.co_name
		in {
			"select_environment",
			"environment_tree",
			"_verify_installed_wheel",
			"_analyze",
			"assert_environment_current",
			"run_process",
			"verify_release",
		}
	]
	(tmp_path.parent / "typing-measurements.json").write_text(
		json.dumps(
			{
				"cases": records,
				"mypy_version_startup_ms": startup_ms,
				"graph": graph,
				"application_profile": phases,
				"persistent_mypy_cache": False,
			},
			indent=2,
		),
		encoding="utf-8",
	)
	assert len(records) == MEASUREMENT_CASES
