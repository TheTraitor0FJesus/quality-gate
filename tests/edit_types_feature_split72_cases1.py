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


@pytest.mark.skipif(os.name != "nt", reason="Windows candidate temp parent selection only")
def test_candidate_typing_cache_parent_falls_back_from_reparse_temp_parent(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	policy_cache = tmp_path / "policy-cache"
	policy_cache.mkdir()
	typing_policy = policy_cache / "releases/v4.6.0"
	temp_parent = tmp_path / "short"
	temp_directory = temp_parent / "temp"
	temp_directory.mkdir(parents=True)
	assert len(os.fspath(temp_parent)) < len(os.fspath(policy_cache))
	monkeypatch.setattr(tempfile, "gettempdir", lambda: os.fspath(temp_directory))
	reparse_identity = temp_parent.lstat()
	original_is_link_or_reparse = is_link_or_reparse

	def mark_temp_parent_as_reparse(metadata: os.stat_result) -> bool:
		if (metadata.st_dev, metadata.st_ino) == (
			reparse_identity.st_dev,
			reparse_identity.st_ino,
		):
			return True
		return original_is_link_or_reparse(metadata)

	monkeypatch.setattr(sys.modules[_candidate_typing_cache_parent.__module__], "is_link_or_reparse", mark_temp_parent_as_reparse)

	assert _candidate_typing_cache_parent(typing_policy) == policy_cache


def test_empty_typing_input_is_explicitly_inapplicable(tmp_path: Path, typing_policy: Path) -> None:
	code, result = invoke(request(tmp_path, typing_policy))
	assert (code, result["status"]) == (0, "not_applicable"), result
	assert result["components"] == []
	assert result["writes_may_have_occurred"] is False


def test_background_cancellation_reaps_active_public_worker(
	tmp_path: Path, typing_policy: Path, typing_environment: Path
) -> None:
	shutil.copytree(typing_environment, tmp_path / ".venv", symlinks=True)
	(tmp_path / "source.py").write_bytes(b"value: int = 1\n")
	payload = request(tmp_path, typing_policy, "source.py", background=True)
	signal = Path(payload["cancel_path"])
	created_parent = not signal.parent.exists()
	signal.parent.mkdir(exist_ok=True)
	stop = threading.Event()
	signalled = threading.Event()

	def cancel() -> None:
		while not stop.wait(0.01):
			if any(tmp_path.glob("temp/qg-*/source/source.py")):
				signal.write_bytes(b"")
				signalled.set()
				return

	thread = threading.Thread(target=cancel)
	thread.start()
	try:
		code, result = invoke(payload)
		assert signalled.is_set(), "Cancellation must reach an active worker."
		assert (code, result["status"], result["reason"]) == (2, "unchecked", "cancelled"), result
		assert result["event_id"] == payload["event_id"]
		assert not list(tmp_path.glob("temp/qg-*"))
	finally:
		stop.set()
		thread.join(timeout=3)
		signal.unlink(missing_ok=True)
		if created_parent and not any(signal.parent.iterdir()):
			signal.parent.rmdir()
	assert result["writes_may_have_occurred"] is False


def test_background_envelope_binds_event_and_rejects_unowned_cancel_path(
	tmp_path: Path, typing_policy: Path
) -> None:
	payload = request(tmp_path, typing_policy, background=True)
	code, result = invoke(payload)
	assert (code, result["schema"], result["status"]) == (0, 2, "not_applicable"), result
	assert result["mode"] == "background"
	assert result["event_id"] == payload["event_id"]
	payload["cancel_path"] = str(tmp_path / "unowned.cancel")
	code, result = invoke(payload)
	assert (code, result["status"], result["reason"]) == (2, "unchecked", "unsafe_path"), result


def test_expired_valid_background_request_keeps_event_identity(
	tmp_path: Path, typing_policy: Path
) -> None:
	payload = request(tmp_path, typing_policy, background=True)
	payload["deadline_unix_ms"] = int(time.time() * 1000) - 1
	code, result = invoke(payload)
	assert (code, result["schema"], result["status"], result["reason"]) == (
		2,
		2,
		"unchecked",
		"deadline",
	), result
	assert result["event_id"] == payload["event_id"]
	payload["unexpected"] = True
	code, result = invoke(payload)
	assert (code, result["schema"], result["reason"]) == (2, 1, "invalid_request"), result
	assert "event_id" not in result


def test_existing_background_signal_cancels_before_runtime_inspection(
	tmp_path: Path, typing_policy: Path
) -> None:
	(tmp_path / "source.py").write_text("value: int = 1\n", encoding="utf-8")
	payload = request(tmp_path, typing_policy, "source.py", background=True)
	signal = Path(payload["cancel_path"])
	created_parent = not signal.parent.exists()
	signal.parent.mkdir(exist_ok=True)
	signal.write_bytes(b"")
	try:
		code, result = invoke(payload)
		assert (code, result["schema"], result["status"], result["reason"]) == (
			2,
			2,
			"unchecked",
			"cancelled",
		), result
		assert result["event_id"] == payload["event_id"]
		assert not list(tmp_path.glob("temp/qg-*"))
	finally:
		signal.unlink(missing_ok=True)
		if created_parent and not any(signal.parent.iterdir()):
			signal.parent.rmdir()


def test_changed_signature_reports_unchanged_consumer_with_real_mypy(
	tmp_path: Path, typing_policy: Path, typing_environment: Path
) -> None:
	shutil.copytree(typing_environment, tmp_path / ".venv", symlinks=True)
	(tmp_path / "api.py").write_text(
		"def accept(value: str) -> str:\n    return value\n", encoding="utf-8"
	)
	(tmp_path / "consumer.py").write_text(
		"from api import accept\nresult = accept(123)\n", encoding="utf-8"
	)
	code, result = invoke(request(tmp_path, typing_policy, "api.py", background=True))
	assert (code, result["status"]) == (1, "failed"), json.dumps(result, indent=2)
	component = result["components"][0]
	assert component["diagnostics"] == [
		{"path": "consumer.py", "line": 2, "column": 17, "code": "arg-type"}
	]
	assert {item["path"] for item in component["inventory"]} == {"api.py", "consumer.py"}
	assert result["selections"][0]["policy_release"] == typing_policy.name
	assert result["selections"][0]["mypy_version"] == "2.3.1"
	assert component["diagnostics"] == full_component_diagnostics(tmp_path, typing_policy, ".")
	assert not list(tmp_path.glob("temp/qg-*"))


def test_typing_does_not_require_unrelated_released_pytest_dependencies(
	tmp_path: Path, typing_policy: Path, typing_environment: Path
) -> None:
	shutil.copytree(typing_environment, tmp_path / ".venv", symlinks=True)
	site = (
		tmp_path
		/ ".venv"
		/ (
			"Lib/site-packages"
			if os.name == "nt"
			else f"lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages"
		)
	)
	for path in site.glob("pluggy*"):
		if path.is_dir():
			shutil.rmtree(path)
		else:
			path.unlink()
	(tmp_path / "source.py").write_text("value: int = 1\n", encoding="utf-8")
	code, result = invoke(request(tmp_path, typing_policy, "source.py", background=True))
	assert (code, result["status"]) == (0, "passed"), result


def test_standard_managed_runtime_pth_reaches_component_inventory_and_mypy(
	tmp_path: Path, typing_policy: Path, typing_environment: Path
) -> None:
	shutil.copytree(typing_environment, tmp_path / ".venv", symlinks=True)
	site = (
		tmp_path
		/ ".venv"
		/ (
			"Lib/site-packages"
			if os.name == "nt"
			else f"lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages"
		)
	)
	for name, content in STANDARD_MANAGED_RUNTIME_PTH.items():
		(site / name).write_bytes(content)
	source = b"value: int = 1\n"
	(tmp_path / "source.py").write_bytes(source)

	code, result = invoke(request(tmp_path, typing_policy, "source.py", background=True))

	assert (code, result["status"]) == (0, "passed"), result
	assert result["components"][0]["inventory"] == [{"path": "source.py", "sha256": digest(source)}]
	assert result["selections"][0]["mypy_version"] == "2.3.1"
	assert not list(tmp_path.glob("temp/qg-*"))


def test_released_typing_extensions_context_is_supported_without_project_execution(
	tmp_path: Path, typing_policy: Path, typing_environment: Path
) -> None:
	shutil.copytree(typing_environment, tmp_path / ".venv", symlinks=True)
	(tmp_path / "source.py").write_text(
		"import email.message\nimport os.path\nimport importlib.metadata\n"
		"from typing_extensions import TypeAlias\nValue: TypeAlias = int\n"
		"raise RuntimeError('must never execute')\n",
		encoding="utf-8",
	)
	code, result = invoke(request(tmp_path, typing_policy, "source.py", background=True))
	assert (code, result["status"]) == (0, "passed"), result


def test_deleted_module_and_missing_stub_context_are_unchecked(
	tmp_path: Path, typing_policy: Path, typing_environment: Path
) -> None:
	shutil.copytree(typing_environment, tmp_path / ".venv", symlinks=True)
	(tmp_path / "consumer.py").write_text(
		"from removed import Value\nvalue: Value\n", encoding="utf-8"
	)
	code, result = invoke(request(tmp_path, typing_policy, "removed.py", background=True))
	assert (code, result["status"], result["reason"]) == (2, "unchecked", "dependencies"), result
	(tmp_path / "consumer.py").write_text(
		"from .removed import Value\nvalue: Value\n", encoding="utf-8"
	)
	code, result = invoke(request(tmp_path, typing_policy, "removed.py", background=True))
	assert (code, result["status"], result["reason"]) == (2, "unchecked", "dependencies"), result


def test_missing_environment_never_uses_global_python(tmp_path: Path, typing_policy: Path) -> None:
	(tmp_path / "example.py").write_text("value: int = 1\n", encoding="utf-8")
	code, result = invoke(request(tmp_path, typing_policy, "example.py"))
	assert (code, result["status"], result["reason"]) == (2, "unchecked", "runtime"), result


def test_unmanaged_dependency_evidence_is_path_ordered(
	tmp_path: Path, typing_policy: Path, typing_environment: Path
) -> None:
	shutil.copytree(typing_environment, tmp_path / ".venv", symlinks=True)
	(tmp_path / "source.py").write_bytes(b"value: int = 1\n")
	(tmp_path / "pyproject.toml").write_bytes(b"[project]\nname = 'fixture'\n")
	(tmp_path / ".mypy.ini").write_bytes(b"[mypy]\n")
	code, result = invoke(request(tmp_path, typing_policy, "source.py", background=True))
	assert (code, result["status"]) == (0, "passed"), result
	assert result["components"][0]["dependency_inputs"] == [
		{"path": ".mypy.ini", "sha256": digest(b"[mypy]\n")},
		{"path": "pyproject.toml", "sha256": digest(b"[project]\nname = 'fixture'\n")},
	]


def test_component_local_stubs_resolve_and_are_included_in_freshness(tmp_path: Path) -> None:
	(tmp_path / "app/stubs").mkdir(parents=True)
	stub = tmp_path / "app/stubs/vendor.pyi"
	stub.write_text("class Value: ...\n", encoding="utf-8")
	component = PythonComponent(
		"app", "app", "3.12", (), (), False, "unused", 300, True, ("stubs",)
	)
	directories = [
		{"path": "app", "entries": [{"name": "stubs", "kind": "directory"}]},
		{"path": "app/stubs", "entries": [{"name": "vendor.pyi", "kind": "file"}]},
	]
	roots = _local_stub_roots(tmp_path, component, directories)
	assert roots == ("app/stubs",)
	assert _local_typed_dependency(tmp_path / roots[0], "vendor")
	sources, captured_directories = inventory(
		tmp_path, "app", managed=True, deadline=time.monotonic() + 5
	)
	invocation = object.__new__(_TypingInvocation)
	invocation.snapshot = tmp_path / "snapshot"
	invocation.snapshot.mkdir()
	invocation.configuration = Configuration(tmp_path)
	invocation._snapshot_sources(sources, "app", [])
	assert (invocation.snapshot / "app/stubs/vendor.pyi").read_bytes() == stub.read_bytes()
	assert (
		_import_context(
			[SourceFile("app/main.py", b"import vendor\n", digest(b"import vendor\n"))],
			"app",
			_StubSelection(),
			tuple(str(tmp_path / root) for root in roots),
		)
		== 1
	)
	before = sources, captured_directories
	stub.write_text("class Value: ...\nchanged: int\n", encoding="utf-8")
	after = inventory(tmp_path, "app", managed=True, deadline=time.monotonic() + 5)
	assert before != after
	environment = _mypy_environment(tmp_path, tmp_path / "snapshot", roots)
	assert environment["MYPYPATH"] == str(tmp_path / "snapshot/app/stubs")
	environment = _mypy_environment(tmp_path, tmp_path / "snapshot", (*roots, "app/extra-stubs"))
	assert environment["MYPYPATH"].split(os.pathsep) == [
		str(tmp_path / "snapshot/app/stubs"),
		str(tmp_path / "snapshot/app/extra-stubs"),
	]


def test_missing_local_stub_root_is_unchecked_by_preflight(tmp_path: Path) -> None:
	with pytest.raises(EditError) as error:
		_local_stub_roots(
			tmp_path,
			PythonComponent("app", "app", "3.12", (), (), False, "unused", 300, True, ("stubs",)),
			[{"path": "app"}],
		)
	assert error.value.reason == "dependencies"


def test_local_stub_root_cannot_escape_component(tmp_path: Path) -> None:
	with pytest.raises(EditError) as error:
		_local_stub_roots(
			tmp_path,
			PythonComponent(
				"app", "app", "3.12", (), (), False, "unused", 300, True, ("../shared-stubs",)
			),
			[{"path": "app"}],
		)
	assert error.value.reason == "unsafe_path"


def test_installed_site_stub_is_accepted_without_local_root(tmp_path: Path) -> None:
	site = tmp_path / "site"
	(site / "vendor-stubs").mkdir(parents=True)
	(site / "vendor-stubs/__init__.pyi").write_text("class Value: ...\n", encoding="utf-8")
	assert _typed_dependency(site, "vendor")
