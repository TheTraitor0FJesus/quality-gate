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


def test_manifest_declares_component_local_stub_roots_only_on_supported_policy() -> None:
	manifest = manifest_text("v3.2.0").replace(
		"typecheck = false\n", 'typecheck = false\nstub_roots = ["stubs"]\n'
	)
	assert parse_manifest(manifest).python[0].stub_roots == ("stubs",)
	with pytest.raises(ValidationError, match="stub_roots"):
		parse_manifest(manifest.replace("v3.2.0", "v3.1.0"))
	for path in ["../stubs", "stubs/../shared", "D:/outside/stubs"]:
		with pytest.raises(ValidationError, match="stub_roots"):
			parse_manifest(manifest.replace('"stubs"', f'"{path}"'))
	with pytest.raises(ValidationError, match="stub_roots"):
		parse_manifest(manifest.replace('["stubs"]', "['stubs\\\\nested']"))


def test_manifest_declares_repository_source_roots_only_on_supported_policy() -> None:
	manifest = manifest_text("v4.6.0").replace(
		"typecheck = false\n", 'typecheck = false\nsource_roots = ["stubs", "scripts"]\n'
	)
	assert parse_manifest(manifest).python[0].source_roots == ("scripts", "stubs")
	with pytest.raises(ValidationError, match="source_roots"):
		parse_manifest(manifest.replace("v4.6.0", "v4.5.0"))
	for roots in [
		'["../scripts"]',
		'["D:/outside"]',
		'["scripts\\\\nested"]',
		'["scripts", "scripts"]',
		'["scripts", "scripts/private"]',
		'["scripts", "Scripts/private"]',
		'["app/shared"]',
	]:
		with pytest.raises(ValidationError, match="source_roots"):
			parse_manifest(manifest.replace('["stubs", "scripts"]', roots))


@pytest.mark.parametrize(
	"name,content",
	[
		("uv.lock", b"[broken\n"),
		("poetry.lock", b"[broken\n"),
		("Pipfile", b"[broken\n"),
		("Pipfile.lock", b'{"default": '),
		pytest.param("Pipfile.lock", b'{"default": {}, "default": {}}', id="duplicate-json"),
		pytest.param("Pipfile.lock", b'{"default": NaN}', id="non-json-constant"),
		("mypy.ini", b"[broken\n"),
		(".mypy.ini", b"[broken\n"),
		("setup.cfg", b"[broken\n"),
	],
)
def test_malformed_project_settings_are_unchecked(
	tmp_path: Path, typing_policy: Path, typing_environment: Path, name: str, content: bytes
) -> None:
	shutil.copytree(typing_environment, tmp_path / ".venv", symlinks=True)
	(tmp_path / "source.py").write_bytes(b"value: int = 1\n")
	(tmp_path / name).write_bytes(content)
	code, result = invoke(request(tmp_path, typing_policy, name, background=True))
	assert (code, result["status"], result["reason"]) == (2, "unchecked", "configuration"), result
	assert result["components"] == []


@pytest.mark.parametrize(("name", "content"), STANDARD_MANAGED_RUNTIME_PTH.items())
def test_standard_managed_runtime_pth_payloads_are_allowed(
	tmp_path: Path, name: str, content: bytes
) -> None:
	assert digest(content.replace(b"\r\n", b"\n")) == _MANAGED_RUNTIME_PTH_SHA256[name]
	site = tmp_path / "site"
	site.mkdir()
	(site / name).write_bytes(content)

	_validate_site(site)


@pytest.mark.parametrize(
	"name,content",
	[
		pytest.param("unknown.pth", b"../outside/site-packages\n", id="path-entry"),
		pytest.param(
			"unknown.pth", b"  # relative path-looking entry\n", id="indented-comment-path"
		),
		pytest.param("unknown.pth", b"import os; os.environ['BAD'] = '1'\n", id="arbitrary-code"),
		pytest.param(
			"a1_coverage.pth",
			STANDARD_MANAGED_RUNTIME_PTH["a1_coverage.pth"] + b"import os\n",
			id="modified-approved-name",
		),
		pytest.param(
			"unapproved-name.pth",
			STANDARD_MANAGED_RUNTIME_PTH["a1_coverage.pth"],
			id="approved-content-wrong-name",
		),
		pytest.param("pytest-cov.pth", b"\xff\n", id="malformed-bytes"),
	],
)
def test_unrecognized_or_modified_managed_runtime_pth_is_rejected(
	tmp_path: Path, name: str, content: bytes
) -> None:
	site = tmp_path / "site"
	site.mkdir()
	(site / name).write_bytes(content)

	with pytest.raises(EditError) as error:
		_validate_site(site)

	assert error.value.reason == "dependencies"


@pytest.mark.parametrize("content", [b"", b"\n# harmless comment\r\n"])
def test_blank_and_comment_only_managed_runtime_pth_remains_allowed(
	tmp_path: Path, content: bytes
) -> None:
	site = tmp_path / "site"
	site.mkdir()
	(site / "unrecognized.pth").write_bytes(content)

	_validate_site(site)


@pytest.mark.parametrize(
	"mutation,reason",
	[
		("missing-venv-config", "runtime"),
		("system-site", "runtime"),
		("corrupt-tool", "tool"),
		("missing-tool-dependency", "tool"),
		("missing-package", "dependencies"),
		("missing-stdlib-submodule", "dependencies"),
		("untyped-package", "dependencies"),
		("missing-stub-submodule", "dependencies"),
		("nested-project", "configuration"),
	],
)
def test_unavailable_context_never_becomes_policy_ignore_success(
	tmp_path: Path, typing_policy: Path, typing_environment: Path, mutation: str, reason: str
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
	body = "value: int = 1\n"
	if mutation == "missing-venv-config":
		(tmp_path / ".venv/pyvenv.cfg").unlink()
	elif mutation == "system-site":
		config = tmp_path / ".venv/pyvenv.cfg"
		config.write_text(config.read_text().replace("false", "true"), encoding="utf-8")
	elif mutation == "corrupt-tool":
		(site / "mypy/__init__.py").write_bytes(b"raise RuntimeError('never execute')\n")
	elif mutation == "missing-tool-dependency":
		shutil.rmtree(site / "pathspec")
	elif mutation == "missing-package":
		body = "import typing_fixture_missing_package\n"
	elif mutation == "missing-stdlib-submodule":
		body = "import email.typing_fixture_missing_module\n"
	elif mutation == "untyped-package":
		(site / "fixture_package").mkdir()
		(site / "fixture_package/__init__.py").write_bytes(b"")
		body = "import fixture_package\n"
	elif mutation == "missing-stub-submodule":
		(site / "fixture_package-stubs").mkdir()
		(site / "fixture_package-stubs/__init__.pyi").write_bytes(b"")
		body = "import fixture_package.missing\n"
	elif mutation == "nested-project":
		(tmp_path / "nested").mkdir()
		(tmp_path / "nested/pyproject.toml").write_bytes(b"[project]\nname='nested'\n")
	(tmp_path / "source.py").write_text(body, encoding="utf-8")
	code, result = invoke(request(tmp_path, typing_policy, "source.py", background=True))
	assert (code, result["status"], result["reason"]) == (2, "unchecked", reason), result
	assert result["components"] == []


def test_shadowed_tool_module_is_rejected_without_execution(
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
	shadow = site / "typing_extensions"
	shadow.mkdir()
	marker = tmp_path / "must-not-execute"
	(shadow / "__init__.py").write_text(
		f"from pathlib import Path\nPath({str(marker)!r}).touch()\n"
		"raise RuntimeError('must never execute')\n",
		encoding="utf-8",
	)
	(tmp_path / "source.py").write_bytes(b"value: int = 1\n")
	code, result = invoke(request(tmp_path, typing_policy, "source.py", background=True))
	assert not marker.exists()
	assert (code, result["status"], result["reason"]) == (2, "unchecked", "tool"), result


def test_untrusted_managed_runtime_pth_is_rejected_without_execution(
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
	marker = tmp_path / "pth-startup-executed"
	(site / "untrusted.pth").write_text(
		f"import pathlib; pathlib.Path({str(marker)!r}).touch()\n", encoding="utf-8"
	)
	(tmp_path / "source.py").write_bytes(b"value: int = 1\n")

	code, result = invoke(request(tmp_path, typing_policy, "source.py", background=True))

	assert not marker.exists()
	assert (code, result["status"], result["reason"]) == (2, "unchecked", "dependencies"), result


def test_unverified_bytecode_cache_is_never_executed(
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
	marker = tmp_path / "must-not-execute"
	with tempfile.TemporaryDirectory(prefix="typing-bytecode-") as directory:
		payload = Path(directory) / "payload.py"
		payload.write_text(
			f"from pathlib import Path\nPath({str(marker)!r}).touch()\n"
			"raise RuntimeError('must never execute')\n",
			encoding="utf-8",
		)
		py_compile.compile(
			str(payload),
			cfile=cache_from_source(str(site / "typing_extensions.py")),
			doraise=True,
			invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH,
		)
	(tmp_path / "source.py").write_bytes(b"value: int = 1\n")
	code, result = invoke(request(tmp_path, typing_policy, "source.py", background=True))
	assert not marker.exists()
	assert (code, result["status"]) == (0, "passed"), result


@pytest.mark.parametrize("mutation", ["source-addition", "configuration", "environment"])
def test_concurrent_mutation_invalidates_whole_real_analysis(
	tmp_path: Path, typing_policy: Path, typing_environment: Path, mutation: str
) -> None:
	shutil.copytree(typing_environment, tmp_path / ".venv", symlinks=True)
	(tmp_path / "source.py").write_bytes(b"value: int = 1\n")
	payload = request(tmp_path, typing_policy, "source.py", background=True)
	changed = threading.Event()

	def mutate() -> None:
		end = time.monotonic() + 25
		while time.monotonic() < end:
			if any(tmp_path.glob("temp/qg-*/source/source.py")):
				if mutation == "source-addition":
					(tmp_path / "added.py").write_bytes(b"other: int = 2\n")
				elif mutation == "configuration":
					(tmp_path / "requirements.txt").write_bytes(b"# changed dependency context\n")
				else:
					(tmp_path / ".venv/extra-context").write_bytes(b"changed")
				changed.set()
				return
			time.sleep(0.01)

	thread = threading.Thread(target=mutate)
	thread.start()
	try:
		code, result = invoke(payload)
	finally:
		thread.join(timeout=26)
	assert changed.is_set(), "The mutation must occur after initial source/environment capture."
	assert (code, result["status"], result["reason"]) == (2, "unchecked", "stale_input"), result
	assert result["components"] == []


def test_optout_requires_a_supported_verified_new_policy(
	tmp_path: Path, typing_policy: Path
) -> None:
	(tmp_path / "app").mkdir()
	(tmp_path / "app/broken.py").write_text("not valid Python !\n", encoding="utf-8")
	(tmp_path / "quality-gate.toml").write_text(manifest_text("v99.0.0"), encoding="utf-8")
	code, result = invoke(request(tmp_path, typing_policy, "app/broken.py"))
	assert (code, result["status"], result["reason"]) == (2, "unchecked", "unsupported_policy"), (
		result
	)
	(tmp_path / "quality-gate.toml").write_text(manifest_text("v2.4.2"), encoding="utf-8")
	code, result = invoke(request(tmp_path, typing_policy, "app/broken.py"))
	assert (code, result["status"], result["reason"]) == (2, "unchecked", "manifest"), result


@pytest.mark.parametrize("configuration_exists", [True, False])
def test_valid_new_policy_optout_is_distinct_from_unavailable_typing(
	tmp_path: Path, configuration_exists: bool
) -> None:
	from test_edit_check import prepare_edit_cache

	project = tmp_path / "project"
	(project / "app").mkdir(parents=True)
	(project / "app/source.py").write_bytes(b"not even valid Python !\n")
	(project / "quality-gate.toml").write_text(manifest_text(), encoding="utf-8")
	configuration_path = "nested/requirements.txt"
	(project / "nested").mkdir()
	if configuration_exists:
		(project / configuration_path).write_bytes(b"# explicit dependency mutation\n")
	cache = prepare_edit_cache(tmp_path / "cache", "v3.1.0")
	code, result = invoke(
		request(project, cache / "releases/v3.1.0", "app/source.py", configuration_path)
	)
	assert (code, result["status"]) == (0, "not_applicable"), result
	assert result["selections"] == []
	assert result["components"][0]["reason"] == "typecheck_disabled"
	assert result["components"][0]["inventory"] == []
	assert {item["path"]: item["sha256"] for item in result["configuration"]}[
		configuration_path
	] == (digest((project / configuration_path).read_bytes()) if configuration_exists else None)


def test_source_limit_never_certifies_partial_component(
	tmp_path: Path, typing_policy: Path
) -> None:
	for index in range(1025):
		(tmp_path / f"source{index}.py").write_bytes(b"value: int = 1\n")
	code, result = invoke(request(tmp_path, typing_policy, "source0.py", background=True))
	assert (code, result["status"], result["reason"]) == (2, "unchecked", "limits"), result
	assert result["components"] == []
