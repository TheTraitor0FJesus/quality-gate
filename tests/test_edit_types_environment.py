"""Related behavior tests."""

from __future__ import annotations

import os
import shutil
import sys
import time
from pathlib import Path

import pytest
from test_edit_types import (
	STANDARD_MANAGED_RUNTIME_PTH,
	_StubSelection,
	digest,
	invoke,
	request,
)
from test_typing_optout import manifest_text

from quality_gate.contracts import PythonComponent, ValidationError, parse_manifest
from quality_gate.edit_inputs import Configuration, SourceFile
from quality_gate.edit_protocol import EditError
from quality_gate.edit_type_environment import _MANAGED_RUNTIME_PTH_SHA256, _validate_site
from quality_gate.edit_type_imports import (
	_import_context,
	_local_stub_roots,
	_local_typed_dependency,
	_typed_dependency,
)
from quality_gate.edit_type_inputs import inventory
from quality_gate.edit_types import _mypy_environment, _TypingInvocation

pytest_plugins = ("test_edit_types",)


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
