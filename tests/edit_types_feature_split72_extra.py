"""Helpers shared by split feature tests."""

from __future__ import annotations

import edit_types_feature_split72_support as _support


def invoke_candidate_wheel(
	payload: dict[str, _support.Any],
	wheel: _support.Path,
	*,
	command: str = "edit-types",
	network_guard: bool = False,
) -> tuple[int, dict[str, _support.Any]]:
	project = _support.Path(payload["root"])
	cache = _support.Path(payload["cache_dir"])
	launcher = [_support.sys.executable, "-B", "-m", "quality_gate.cli", command]
	if network_guard:
		worker_guard = _support._candidate_worker_guard()
		guard = "import runpy, socket, subprocess, sys\ndef deny_network(*args, **kwargs):\n    raise RuntimeError('network access is disabled for this edit check')\nsocket.socket.connect = deny_network\nsocket.socket.connect_ex = deny_network\nsocket.create_connection = deny_network\noriginal_popen = subprocess.Popen\nworker_guard = sys.argv[2]\ndef guard_popen(args, *positional, **keywords):\n    values = [str(value) for value in args] if not isinstance(args, str) else [args]\n    command = ' '.join(values)\n    mypy = \"runpy.run_module('mypy'\" in command\n    runtime_probe = \"'base_executable'\" in command and \"'stdlib'\" in command\n    worker = 'from quality_gate.edit_worker import main' in command\n    if not (mypy or runtime_probe or worker):\n        raise RuntimeError('unexpected installer or network process: ' + command)\n    if worker:\n        code_index = values.index('-c') + 1\n        values[code_index] = worker_guard + '\\n' + values[code_index]\n        args = values\n    return original_popen(args, *positional, **keywords)\nsubprocess.Popen = guard_popen\nsys.argv = ['quality-gate', sys.argv[1]]\nrunpy.run_module('quality_gate.cli', run_name='__main__')\n"
		launcher = [_support.sys.executable, "-B", "-c", guard, command, worker_guard]
	completed = _support.subprocess.run(
		launcher,
		cwd=project.parent,
		env={
			**_support.os.environ,
			"PYTHONPATH": str(wheel),
			"PYTHONDONTWRITEBYTECODE": "1",
			"LOCALAPPDATA": str(cache.parent / "cli-user-cache"),
			"XDG_CACHE_HOME": str(cache.parent / "cli-user-cache"),
		},
		input=_support.json.dumps(payload).encode(),
		capture_output=True,
		check=False,
		timeout=33 if payload["schema"] == _support.BACKGROUND_SCHEMA else 13,
	)
	assert completed.stderr == b"", completed.stderr.decode("utf-8", errors="replace")
	result = _support.json.loads(completed.stdout)
	return (completed.returncode, result)


def _prepare_argus_layout(root: _support.Path, *, signature: str = "str") -> None:
	(root / "bot").mkdir(parents=True)
	(root / "bot/__init__.py").write_text("", encoding="utf-8")
	(root / "bot/stubs").mkdir()
	(root / "bot/stubs/local_api.pyi").write_text(
		"def format_label(value: str) -> str: ...\n", encoding="utf-8"
	)
	(root / "bot/main.py").write_text(
		"from apscheduler.schedulers.asyncio import AsyncIOScheduler\nfrom scripts.rendering import render\nscheduler = AsyncIOScheduler()\nscheduler.start()\nlabel: str = render(1)\n",
		encoding="utf-8",
	)
	(root / "scripts").mkdir()
	(root / "scripts/__init__.py").write_text("", encoding="utf-8")
	(root / "scripts/rendering.py").write_text(
		f"def render(value: {signature}) -> str:\n    return str(value)\n", encoding="utf-8"
	)
	(root / "apscheduler/schedulers").mkdir(parents=True)
	(root / "apscheduler/py.typed").write_bytes(b"\n")
	(root / "apscheduler/__init__.pyi").write_text("", encoding="utf-8")
	(root / "apscheduler/schedulers/__init__.pyi").write_text("", encoding="utf-8")
	(root / "apscheduler/schedulers/asyncio.pyi").write_text(
		"class AsyncIOScheduler:\n    def start(self) -> None: ...\n", encoding="utf-8"
	)
	(root / "bot/requirements.txt").write_text("APScheduler==3.10.4\n", encoding="utf-8")
	(root / "bot/requirements-dev.txt").write_text(
		"-r requirements.txt\npytest\n", encoding="utf-8"
	)
	(root / "quality-gate.toml").write_text(_support._argus_typing_manifest(), encoding="utf-8")


def _assert_candidate_stale_mutation(
	project: _support.Path,
	cache: _support.Path,
	wheel: _support.Path,
	*,
	relative: str,
	replacement: bytes | None,
	snapshot_relative: str | None = None,
) -> None:
	path = project / relative
	before = path.read_bytes() if path.is_file() else None
	captured = _support.threading.Event()
	stop = _support.threading.Event()

	def mutate_after_snapshot() -> None:
		snapshot_path = f"temp/qg-*/source/{snapshot_relative or relative}"
		while not stop.wait(0.01):
			if any(project.glob(snapshot_path)):
				if replacement is None:
					path.unlink()
				else:
					path.write_bytes(replacement)
				captured.set()
				return

	thread = _support.threading.Thread(target=mutate_after_snapshot)
	thread.start()
	try:
		try:
			code, result = _support._run_candidate_types(project, cache, wheel, "bot/main.py")
		finally:
			stop.set()
			thread.join(timeout=3)
	finally:
		if before is None:
			path.unlink(missing_ok=True)
		else:
			path.write_bytes(before)
	assert captured.is_set(), "Mutation must follow snapshot capture."
	assert (code, result["status"], result["reason"]) == (2, "unchecked", "stale_input"), result
	assert result["components"] == []
