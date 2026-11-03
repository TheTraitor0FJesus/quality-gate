"""Controlled public CLI for host protocol tests, never detector-parity evidence."""

from __future__ import annotations

import hashlib
import json
import sys
import textwrap
import zipfile
from pathlib import Path

from _shared.quality_settings import load_settings, settings_operation
from _shared.tests.provider_fixture import ENGINE_CODE, install_engine_fixture

CODE = """import hashlib, json, os, sys
from pathlib import Path
request = json.load(sys.stdin)
mode = os.environ.get("SECRET_FIXTURE_MODE", "passed")
marker = os.environ.get("SECRET_FIXTURE_MARKER")
if marker:
    Path(marker).write_text("called", encoding="utf-8")
if mode == "hang":
    import subprocess, time
    child = subprocess.Popen([sys.executable, "-B", "-c", "import time; time.sleep(30)"])
    Path(marker).write_text(str(child.pid), encoding="utf-8")
    time.sleep(30)
if mode == "malformed":
    print("synthetic-private-fragment")
    sys.exit(0)
if mode == "noisy":
    print("synthetic-private-fragment", file=sys.stderr)
if sys.argv[1:] != ["edit-secrets"] or set(request["baseline"]) != {"policy_release"}:
    print(json.dumps({"schema": 1, "check_id": "secrets.candidate",
        "status": "unchecked", "reason": "invalid_request"}))
    sys.exit(2)
files = []
for item in request["files"]:
    content = (Path(request["root"]) / item["path"]).read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    status = mode if mode in {"failed", "waived"} else "passed"
    findings = [] if status == "passed" else [{"check_id": "secrets.candidate",
        "path": item["path"],
        "target": item["path"] + ":1", "line": 1, "fingerprint": "b" * 64,
        "status": status, "Secret": "synthetic-private-fragment"}]
    files.append({"path": item["path"], "input_sha256": item["sha256"], "sha256": digest,
        "changed": False, "selection": 0, "coverage": "baseline",
        "status": status, "findings": findings})
selection = {"policy_release": request["baseline"]["policy_release"], "release_sha256": "c" * 64,
    "policy_source": "gitleaks-built-in", "policy_sha256": "d" * 64, "scanner_sha256": "d" * 64,
    "scanner_version": "8.30.1", "waiver_sha256": "e" * 64}
print(json.dumps({"schema": 1, "engine_version": "9.0.0", "check_id": "secrets.candidate",
    "root": request["root"], "status": files[0]["status"], "reason": None,
    "files": files, "selections": [selection],
    "inputs": [{"path": path, "sha256": None} for path in
        ["quality-gate.toml", ".gitleaks.toml", ".gitleaksignore"]],
    "process_count": 1, "elapsed_ms": 1, "writes_may_have_occurred": False}))
sys.exit(1 if mode == "failed" else 0)
"""


def install_secret_engine_fixture(root: Path) -> tuple[dict[str, str], Path]:
	"""Use the real immutable-wheel launcher with a disposable fixture inventory."""
	environment, wheel = install_engine_fixture(root)
	dispatch = (
		'import sys\nif sys.argv[1:] == ["edit-check"]:\n'
		+ textwrap.indent(ENGINE_CODE, "    ")
		+ "\n    raise SystemExit(0)\n"
		+ CODE
	)
	with zipfile.ZipFile(wheel, "w") as archive:
		archive.writestr("quality_gate/__init__.py", "")
		archive.writestr("quality_gate/cli.py", dispatch)
	digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
	(wheel.parent / "release.toml").write_text(
		'[release]\nversion = "v9.0.0"\n'
		f'files = [{{path = "{wheel.name}", sha256 = "{digest}"}}]\n',
		encoding="utf-8",
	)
	return environment, wheel


def secret_project_fixture(
	base: Path, enabled: tuple[str, ...] = ("secrets",)
) -> tuple[dict[str, str], Path, dict, Path]:
	host, project = base / "host", (base / "project").resolve()
	environment, wheel = install_secret_engine_fixture(host)
	project.mkdir()
	(project / ".git").mkdir()
	content = b"VALUE = 1\n"
	(project / "a.py").write_bytes(content)
	settings = base / "settings.json"
	settings_operation(settings, "init")
	settings_operation(
		settings,
		"update",
		{
			"provider_release": "v9.0.0",
			"baseline_release": "v2.4.2",
			"enabled": list(enabled),
			"budget_seconds": 10,
		},
	)
	request = {
		"schema": 1,
		"root": str(project),
		"files": [
			{"path": "a.py", "sha256": hashlib.sha256(content).hexdigest()},
		],
	}
	return environment, settings, request, wheel


def background_secret_fixture(base: Path) -> tuple[dict[str, str], Path, dict, Path]:
	"""Bind the controlled reader to real fixture inventory for background validation.

	This authenticates fixture transport, not detector correctness or released policy parity.
	"""
	environment, settings, request, wheel = secret_project_fixture(base, ("ruff", "secrets"))
	policy_root = Path(environment["LOCALAPPDATA"]) / "quality-gate/releases/v2.4.2"
	contents = {
		"quality_gate/policy/ruff.toml": b"# Controlled transport fixture\n",
		"ruff-fixture.whl": b"Controlled Ruff inventory, not an executable checker\n",
		"gitleaks-fixture.exe": b"Controlled scanner inventory, not an executable scanner\n",
	}
	files = []
	for relative, content in contents.items():
		path = policy_root / relative
		path.parent.mkdir(parents=True, exist_ok=True)
		path.write_bytes(content)
		files.append({"path": relative, "sha256": hashlib.sha256(content).hexdigest()})
	tools = [
		{"name": name, "version": version, **files[index]}
		for name, version, index in (("ruff", "0.16.4", 1), ("gitleaks", "8.30.1", 2))
	]
	manifest = policy_root / "release.toml"
	manifest.write_text(
		'[release]\nversion = "v2.4.2"\n'
		+ "files = "
		+ "["
		+ ", ".join(
			"{" + ", ".join(f"{key} = {json.dumps(value)}" for key, value in item.items()) + "}"
			for item in files
		)
		+ "]"
		+ "\n"
		+ "".join(
			"[[release.tools]]\n"
			+ "".join(f"{key} = {json.dumps(value)}\n" for key, value in tool.items())
			for tool in tools
		),
		encoding="utf-8",
	)
	policy_identity = hashlib.sha256(manifest.read_bytes()).hexdigest()
	ruff_code = ENGINE_CODE.replace(
		'"release_sha256": "a" * 64', f'"release_sha256": "{policy_identity}"'
	)
	ruff_code = ruff_code.replace(
		'"policy_sha256": "b" * 64', f'"policy_sha256": "{files[0]["sha256"]}"'
	)
	python_identity = hashlib.sha256(Path(sys.executable).resolve().read_bytes()).hexdigest()
	ruff_code = ruff_code.replace(
		'"python_sha256": "d" * 64', f'"python_sha256": "{python_identity}"'
	)
	ruff_code = ruff_code.replace('"inputs": [],', '"phase": request.get("phase"), "inputs": [],')
	secret_code = CODE.replace(
		'"release_sha256": "c" * 64', f'"release_sha256": "{policy_identity}"'
	)
	secret_code = secret_code.replace('"d" * 64', f'"{files[2]["sha256"]}"')
	dispatch = (
		'import sys\nif sys.argv[1:] == ["edit-check"]:\n'
		+ textwrap.indent(ruff_code, "    ")
		+ "\n    raise SystemExit(0)\n"
		+ secret_code
	)
	with zipfile.ZipFile(wheel, "w") as archive:
		archive.writestr("quality_gate/__init__.py", "")
		archive.writestr("quality_gate/cli.py", dispatch)
	(wheel.parent / "release.toml").write_text(
		'[release]\nversion = "v9.0.0"\nfiles = [{path = "'
		+ wheel.name
		+ '", sha256 = "'
		+ hashlib.sha256(wheel.read_bytes()).hexdigest()
		+ '"}]\n',
		encoding="utf-8",
	)
	settings_operation(
		settings,
		"update",
		{
			**load_settings(settings)["current"],
			"baseline_python": sys.executable,
			"baseline_python_version": f"{sys.version_info.major}.{sys.version_info.minor}",
		},
	)
	return environment, settings, request, wheel
