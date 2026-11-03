"""Controlled public provider process for installation transaction tests only.

This fixture proves launcher/activation behavior, not Ruff policy correctness.
Real released-tool and host-event acceptance remain separate required checks.
"""

from __future__ import annotations

import hashlib
import os
import zipfile
from pathlib import Path

ENGINE_CODE = """import hashlib, json, os, sys, time
from pathlib import Path
request = json.load(sys.stdin)
minimum_ms = int(os.environ.get("FIXTURE_MIN_REMAINING_MS", "0"))
if minimum_ms and request["deadline_unix_ms"] - time.time() * 1000 < minimum_ms:
    print(json.dumps({"schema": 1, "status": "unchecked", "reason": "deadline"}))
    sys.exit(2)
counter = os.environ.get("FIXTURE_PROVIDER_CALLS")
if counter:
    with Path(counter).open("a", encoding="utf-8") as sink:
        sink.write("call\\n")
files = []
for item in request["files"]:
    content = (Path(request["root"]) / item["path"]).read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    files.append({"path": item["path"], "status": "passed", "coverage": "baseline",
        "component": None, "input_sha256": item["sha256"], "sha256": digest,
        "changed": False, "selection": 0, "findings": [], "format": "passed"})
selection = {"policy_release": "v2.4.2", "release_sha256": "a" * 64,
    "policy_sha256": "b" * 64, "ruff_version": "0.16.4", "ruff_sha256": "c" * 64,
    "python": sys.executable, "python_version": "3.12", "python_sha256": "d" * 64,
    "runtime_fingerprint": "e" * 64}
print(json.dumps({"schema": 1, "engine_version": "9.0.0", "root": request["root"],
    "status": "passed", "reason": None, "files": files, "selections": [selection],
    "inputs": [], "process_count": 0, "elapsed_ms": 1, "writes_may_have_occurred": False}))
"""


def native_launcher() -> Path:
	root = os.environ.get("CODEX_HOME")
	if not root:
		root = str(
			Path(os.environ.get("USERPROFILE") or os.environ.get("HOME") or "~").expanduser()
			/ ".codex"
		)
	installed = Path(root) / "hooks" / "_shared" / "native_quality_gate.py"
	if installed.is_file():
		return installed
	return Path(__file__).resolve().parents[1] / "native_quality_gate.py"


def install_engine_fixture(root: Path) -> tuple[dict[str, str], Path]:
	launcher = native_launcher()
	target = root / "hooks" / "_shared" / launcher.name
	target.parent.mkdir(parents=True, exist_ok=True)
	target.write_bytes(launcher.read_bytes())
	for name in ("codex_hook_adapter.py", "command_safety.py", "github_safety.py"):
		source = launcher.parent / name
		if source.is_file():
			(target.parent / name).write_bytes(source.read_bytes())
	local = root.parent / "local-app-data"
	release = local / "quality-gate" / "releases" / "v9.0.0"
	release.mkdir(parents=True)
	wheel = release / "quality_gate-9.0.0-py3-none-any.whl"
	with zipfile.ZipFile(wheel, "w") as archive:
		archive.writestr("quality_gate/__init__.py", "")
		archive.writestr("quality_gate/cli.py", ENGINE_CODE)
	digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
	(release / "release.toml").write_text(
		'[release]\nversion = "v9.0.0"\n'
		f'files = [{{path = "{wheel.name}", sha256 = "{digest}"}}]\n',
		encoding="utf-8",
	)
	environment = {
		**os.environ,
		"CODEX_HOME": str(root),
		"LOCALAPPDATA": str(local),
		"CODEX_POST_EDIT_HEAVY_SECURITY": "0",
		"FIXTURE_PROVIDER_CALLS": str(root.parent / "provider.calls"),
	}
	return environment, wheel
