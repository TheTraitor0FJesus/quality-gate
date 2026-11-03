"""Accept declared multi-root certificates and reject forged or stale coverage."""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from time import monotonic
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from python_types.results import validate_result
from python_types.tests.result_fixture import CONFIGURATION, certificate, digest, file_hash, seal


def managed_certificate(root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
	request, report = certificate(root)
	for relative in ("app/stubs", "shared/pkg/empty"):
		(root / relative).mkdir(parents=True)
	for name in ("example.py", "consumer.py"):
		(root / name).rename(root / "app" / name)
	(root / "app/stubs/client.pyi").write_bytes(b"VALUE: int\n")
	(root / "shared/pkg/__init__.py").write_bytes(b"VALUE: int = 1\n")
	(root / "shared/pkg/py.typed").write_bytes(b"")
	(root / "shared/py.typed").write_bytes(b"")
	(root / "quality-gate.toml").write_text(
		'[quality]\nschema = 2\npolicy_release = "v4.6.0"\n'
		'[repository]\nname = "fixture"\ndomains = ["repository", "python"]\n'
		'required_documents = ["quality-gate.toml"]\n[[python]]\nname = "app"\n'
		'path = "app"\npython_version = "3.12"\ndependency_inputs = []\n'
		'test_paths = []\ntests_applicable = false\ntests_reason = "Wire fixture."\n'
		'source_roots = ["shared/pkg"]\nstub_roots = ["stubs"]\n',
		encoding="utf-8",
	)
	selection = report["selections"][0]
	policy = Path(request["cache_dir"]) / "releases/v4.6.0"
	policy.with_name("v2.4.2").rename(policy)
	manifest = policy / "release.toml"
	manifest.write_text(manifest.read_text().replace("v2.4.2", "v4.6.0"), encoding="utf-8")
	selection.update(policy_release="v4.6.0", release_sha256=file_hash(manifest))
	key = hashlib.sha256(str(root).casefold().encode()).hexdigest()[:32]
	environment = Path(request["cache_dir"]) / "runtimes" / key / selection["runtime_fingerprint"]
	environment.parent.mkdir(parents=True)
	(root / ".venv").rename(environment)
	selection["environment"]["path"] = str(environment)
	selection["python"] = str(
		environment / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
	)
	request["files"] = [{"path": "app/example.py", "sha256": file_hash(root / "app/example.py")}]
	report["files"] = [
		{"path": item["path"], "input_sha256": item["sha256"], "final_sha256": item["sha256"]}
		for item in request["files"]
	]
	component = report["components"][0]
	component.update(name="app", path="app", coverage="managed")
	directories = sorted(
		path
		for scope in (root / "app", root / "shared/pkg")
		for path in (scope, *scope.rglob("*"))
		if path.is_dir()
	)
	component["directories"] = [
		{
			"path": path.relative_to(root).as_posix(),
			"entries": [
				{"name": item.name, "kind": "directory" if item.is_dir() else "file"}
				for item in sorted(path.iterdir())
			],
		}
		for path in directories
	]
	component["inventory"] = [
		{"path": path.relative_to(root).as_posix(), "sha256": file_hash(path)}
		for path in sorted(
			item
			for directory in directories
			for item in directory.iterdir()
			if item.is_file() and item.suffix in {".py", ".pyi"}
		)
	]
	component["source_count"] = len(component["inventory"])
	component["scope_sha256"] = digest(
		{"inventory": component["inventory"], "directories": component["directories"]}
	)
	paths = set(CONFIGURATION)
	for directory in directories:
		for ancestor in (directory, *directory.relative_to(root).parents):
			base = ancestor.relative_to(root) if ancestor.is_absolute() else ancestor
			paths.update(
				(base / name).as_posix()
				for name in (
					"quality-gate.toml",
					"pyproject.toml",
					"mypy.ini",
					".mypy.ini",
					"setup.cfg",
				)
			)
		if (directory / "py.typed").exists():
			paths.add((directory.relative_to(root) / "py.typed").as_posix())
	for ancestor in (Path("."), Path("shared")):
		paths.update(
			(ancestor / name).as_posix() for name in ("__init__.py", "__init__.pyi", "py.typed")
		)
	report["configuration"] = [
		{"path": path, "sha256": file_hash(root / path) if (root / path).exists() else None}
		for path in sorted(paths)
	]
	return request, seal(report)


class DeclaredSourceCoverage(unittest.TestCase):
	def validate(self, request: dict[str, Any], report: dict[str, Any]) -> dict[str, Any]:
		return validate_result(json.dumps(seal(report)), 0, request, "v9.0.0", monotonic() + 10)

	def test_complete_declared_sources_and_local_stubs_are_accepted(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			request, report = managed_certificate(Path(directory).resolve() / "project")
			self.assertEqual(self.validate(request, report)["status"], "passed")

	def test_stale_shared_stub_marker_and_manifest_invalidate_certificate(self) -> None:
		for path in (
			"shared/pkg/__init__.py",
			"app/stubs/client.pyi",
			"shared/py.typed",
			"quality-gate.toml",
		):
			with self.subTest(path=path), tempfile.TemporaryDirectory() as directory:
				root = Path(directory).resolve() / "project"
				request, report = managed_certificate(root)
				(root / path).write_bytes(b"changed\n")
				self.assertEqual(self.validate(request, report)["reason"], "stale_result")

	def test_added_source_directory_and_ancestor_marker_invalidate_certificate(self) -> None:
		for path in ("shared/pkg/added.py", "shared/pkg/new", "__init__.pyi"):
			with self.subTest(path=path), tempfile.TemporaryDirectory() as directory:
				root = Path(directory).resolve() / "project"
				request, report = managed_certificate(root)
				if path.endswith("/new"):
					(root / path).mkdir()
				else:
					(root / path).write_bytes(b"")
				self.assertEqual(self.validate(request, report)["reason"], "stale_result")

	def test_forged_missing_extra_and_undeclared_roots_are_rejected(self) -> None:
		for mutation in ("missing_root", "missing_child", "extra_root", "undeclared", "wrong_name"):
			with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
				root = Path(directory).resolve() / "project"
				request, report = managed_certificate(root)
				component = report["components"][0]
				if mutation.startswith("missing"):
					path = "shared/pkg" if mutation == "missing_root" else "shared/pkg/empty"
					component["directories"] = [
						item for item in component["directories"] if item["path"] != path
					]
				elif mutation == "wrong_name":
					component["name"] = "forged"
				elif mutation == "extra_root":
					(root / "foreign").mkdir()
					component["directories"].append({"path": "foreign", "entries": []})
					component["directories"].sort(key=lambda item: item["path"])
				else:
					manifest = root / "quality-gate.toml"
					manifest.write_text(
						manifest.read_text().replace(
							'source_roots = ["shared/pkg"]', "source_roots = []"
						),
						encoding="utf-8",
					)
					for record in report["configuration"]:
						if record["path"] == "quality-gate.toml":
							record["sha256"] = file_hash(manifest)
				self.assertEqual(
					self.validate(request, report)["reason"], "invalid_provider_result"
				)
