"""Controlled wire certificate with real files; not an executable checker fixture."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

CONFIGURATION = (
	"quality-gate.toml",
	"pyproject.toml",
	"mypy.ini",
	".mypy.ini",
	"setup.cfg",
	"requirements.txt",
	"requirements-dev.txt",
	"uv.lock",
	"poetry.lock",
	"Pipfile",
	"Pipfile.lock",
)


def digest(value: object) -> str:
	return hashlib.sha256(
		json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()
	).hexdigest()


def file_hash(path: Path) -> str:
	return hashlib.sha256(path.read_bytes()).hexdigest()


def seal(report: dict[str, Any]) -> dict[str, Any]:
	components = [
		{key: value for key, value in item.items() if key != "analysis_ms"}
		for item in report["components"]
	]
	report["freshness_sha256"] = digest(
		{
			**{
				key: report[key]
				for key in ("root", "root_identity", "files", "configuration", "selections")
			},
			"components": components,
		}
	)
	return report


def certificate(root: Path, cache_dir: Path | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
	root.mkdir(parents=True, exist_ok=True)
	cache_dir = cache_dir or root.parent / "gate-cache"
	policy_root = cache_dir / "releases/v2.4.2"
	policy = policy_root / "quality_gate/policy/mypy.ini"
	policy.parent.mkdir(parents=True, exist_ok=True)
	policy.write_bytes(b"[mypy]\ncheck_untyped_defs = True\n")
	tool = policy_root / "mypy-2.3.1-fixture.whl"
	tool.write_bytes(b"Controlled transport fixture, not executable mypy")
	manifest = policy_root / "release.toml"
	manifest.write_text(
		'[release]\nversion = "v2.4.2"\n'
		+ 'files = [{path = "quality_gate/policy/mypy.ini", sha256 = "'
		+ file_hash(policy)
		+ '"}]\n'
		+ '[[release.tools]]\nname = "mypy"\nversion = "2.3.1"\npath = "'
		+ tool.name
		+ '"\nsha256 = "'
		+ file_hash(tool)
		+ '"\n',
		encoding="utf-8",
	)
	(root / ".git").mkdir()
	(root / "example.py").write_bytes(b"VALUE: int = 1\n")
	(root / "consumer.py").write_bytes(b"VALUE: int = 2\n")
	environment = root / ".venv"
	python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
	python.parent.mkdir(parents=True)
	shutil.copyfile(sys.executable, python)
	(environment / "pyvenv.cfg").write_text(
		"include-system-site-packages = false\n", encoding="utf-8"
	)
	records: list[dict[str, Any]] = []
	for path in [environment, *environment.rglob("*")]:
		relative = path.relative_to(environment).as_posix()
		if path.is_dir():
			records.append({"path": relative, "kind": "directory"})
		else:
			records.append(
				{
					"path": relative,
					"kind": "file",
					"sha256": file_hash(path),
					"size": path.stat().st_size,
				}
			)
	base_python = Path(sys.executable).resolve()
	records.append(
		{
			"path": "@base-python",
			"kind": "interpreter",
			"target": str(base_python),
			"sha256": file_hash(base_python),
			"size": base_python.stat().st_size,
		}
	)
	records.sort(key=lambda item: item["path"])
	env_digest = digest(records)
	selection = {
		"policy_release": "v2.4.2",
		"release_sha256": file_hash(manifest),
		"policy_sha256": file_hash(policy),
		"mypy_version": "2.3.1",
		"mypy_sha256": file_hash(tool),
		"python": str(python),
		"python_version": f"{sys.version_info.major}.{sys.version_info.minor}",
		"python_sha256": file_hash(python),
		"base_python": str(base_python),
		"base_python_sha256": file_hash(base_python),
		"runtime_fingerprint": "d" * 64,
		"environment_sha256": env_digest,
		"environment": {
			"path": str(environment),
			"sha256": env_digest,
			"file_count": sum(item["kind"] != "directory" for item in records),
			"bytes": sum(item.get("size", 0) for item in records),
		},
	}
	inventory = [
		{"path": name, "sha256": file_hash(root / name)} for name in ("consumer.py", "example.py")
	]
	directories = [
		{"path": ".", "entries": [{"name": item["path"], "kind": "file"} for item in inventory]}
	]
	component = {
		"name": "unmanaged",
		"path": ".",
		"coverage": "baseline",
		"status": "passed",
		"reason": None,
		"selection": 0,
		"inventory": inventory,
		"directories": directories,
		"scope_sha256": digest({"inventory": inventory, "directories": directories}),
		"dependency_inputs": [],
		"diagnostics": [],
		"source_count": 2,
		"import_count": 0,
		"analysis_ms": 1,
	}
	files: list[dict[str, Any]] = [
		{"path": "example.py", "sha256": file_hash(root / "example.py")},
		{"path": "deleted.py", "sha256": None},
	]
	request = {
		"schema": 1,
		"cache_dir": str(cache_dir),
		"root": str(root),
		"files": files,
		"baseline": {"policy_release": "v2.4.2"},
	}
	report = {
		"schema": 1,
		"check_id": "mypy",
		"engine_version": "9.0.0",
		"status": "passed",
		"reason": None,
		"action": None,
		"writes_may_have_occurred": False,
		"root": str(root),
		"root_identity": {"device": root.stat().st_dev, "inode": root.stat().st_ino},
		"files": sorted(
			[
				{
					"path": item["path"],
					"input_sha256": item["sha256"],
					"final_sha256": item["sha256"],
				}
				for item in files
			],
			key=lambda item: item["path"],
		),
		"configuration": [{"path": name, "sha256": None} for name in sorted(CONFIGURATION)],
		"selections": [selection],
		"components": [component],
		"process_count": 1,
		"elapsed_ms": 1,
	}
	return request, seal(report)
