"""Controlled released-CLI transport using a real verified-wheel launcher."""

from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

from _shared.quality_settings import settings_operation
from _shared.tests.provider_fixture import install_engine_fixture

from python_types.tests.result_fixture import certificate

CODE = """import json, os, sys, time
from pathlib import Path
request = json.load(sys.stdin)
mode = os.environ.get("TYPING_FIXTURE_MODE", "passed")
marker = os.environ.get("TYPING_FIXTURE_MARKER")
if marker:
    Path(marker).write_text(json.dumps(request), encoding="utf-8")
wait = os.environ.get("TYPING_FIXTURE_WAIT")
while wait and not Path(wait).exists():
    time.sleep(.01)
if mode == "hang":
    import subprocess
    child = subprocess.Popen([sys.executable, "-B", "-c", "import time; time.sleep(30)"])
    Path(marker).write_text(str(child.pid), encoding="utf-8")
    time.sleep(30)
if mode == "malformed":
    print("private-source-fragment")
    sys.exit(0)
if mode == "noisy":
    print("private-source-fragment", file=sys.stderr)
if sys.argv[1:] != ["edit-types"]:
    sys.exit(2)
result = json.loads(Path(os.environ["TYPING_FIXTURE_RESULT"]).read_text(encoding="utf-8"))
if request["schema"] == 2:
    import hashlib
    result.update({key:request[key] for key in ("schema", "mode", "event_id")})
    result["files"] = [
        {"path":item["path"], "input_sha256":item["sha256"], "final_sha256":item["sha256"]}
        for item in request["files"]]
    fields = {key:result[key] for key in (
        "schema", "mode", "event_id", "root", "root_identity", "files",
        "configuration", "selections")}
    fields["components"] = [
        {key:value for key,value in component.items() if key != "analysis_ms"}
        for component in result["components"]]
    result["freshness_sha256"] = hashlib.sha256(json.dumps(
        fields, sort_keys=True, ensure_ascii=True, separators=(",", ":")
    ).encode()).hexdigest()
print(json.dumps(result))
returned = os.environ.get("TYPING_FIXTURE_RETURNED")
if returned:
    Path(returned).touch()
sys.exit(1 if result["status"] == "failed" else 2 if result["status"] == "unchecked" else 0)
"""


def typing_project_fixture(base: Path) -> tuple[dict[str, str], Path, dict, Path]:
	host = base / "host"
	environment, wheel = install_engine_fixture(host)
	with zipfile.ZipFile(wheel, "w") as archive:
		archive.writestr("quality_gate/__init__.py", "")
		archive.writestr("quality_gate/cli.py", CODE)
	sha256 = hashlib.sha256(wheel.read_bytes()).hexdigest()
	(wheel.parent / "release.toml").write_text(
		'[release]\nversion = "v9.0.0"\n'
		f'files = [{{path = "{wheel.name}", sha256 = "{sha256}"}}]\n',
		encoding="utf-8",
	)
	root = base / "project"
	root.mkdir()
	request, report = certificate(
		root.resolve(), Path(environment["LOCALAPPDATA"]) / "quality-gate"
	)
	result = base / "result.json"
	result.write_text(json.dumps(report), encoding="utf-8")
	settings = host / "hooks/_shared/python-quality.json"
	settings_operation(settings, "init")
	settings_operation(
		settings,
		"update",
		{
			"baseline_release": "v2.4.2",
			"provider_release": "v9.0.0",
			"enabled": ["types"],
			"budget_seconds": 10.0,
		},
	)
	return (
		{
			**environment,
			"TYPING_FIXTURE_RESULT": str(result),
			# Background requests omit cache_dir, so Linux workers use XDG_CACHE_HOME.
			"XDG_CACHE_HOME": environment["LOCALAPPDATA"],
		},
		settings,
		request,
		wheel,
	)
