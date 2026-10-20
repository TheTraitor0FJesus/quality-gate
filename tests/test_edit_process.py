"""Native process ownership used by the normal and edit secret transports."""

from __future__ import annotations

import os
import signal
import sys
import time
from pathlib import Path

import pytest
from test_edit_check import process_alive

from quality_gate.edit_process import run_process
from quality_gate.edit_protocol import EditError


@pytest.mark.parametrize("lifecycle", ["timeout", "orphan"])
def test_native_tree_is_owned_before_descendants_start(tmp_path: Path, lifecycle: str) -> None:
	base_python = Path(sys.base_prefix) / "python.exe" if os.name == "nt" else Path(sys.executable)
	marker = tmp_path / "child.pid"
	body = (
		"import subprocess,sys,time; from pathlib import Path; "
		"child=subprocess.Popen([sys.executable,'-I','-S','-B','-c',"
		"'import time; time.sleep(30)'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
		f"Path({str(marker)!r}).write_text(str(child.pid)); "
		+ ("time.sleep(30)" if lifecycle == "timeout" else "print('complete')")
	)
	started = time.monotonic()
	try:
		if lifecycle == "timeout":
			with pytest.raises(EditError, match=r"^deadline$"):
				run_process(
					[str(base_python), "-I", "-S", "-B", "-c", body],
					root=tmp_path,
					environment=dict(os.environ),
					deadline=started + 2,
					own_tree=True,
				)
		else:
			code, output, diagnostics = run_process(
				[str(base_python), "-I", "-S", "-B", "-c", body],
				root=tmp_path,
				environment=dict(os.environ),
				deadline=started + 2,
				own_tree=True,
			)
			assert (code, output.strip(), diagnostics) == (0, b"complete", b"")
		assert marker.is_file(), "The owned descendant must start before cancellation."
		pid = int(marker.read_text())
		# Killing is asynchronous; allow the kernel/init to finish reaping the child.
		cleanup_deadline = time.monotonic() + 1
		while process_alive(pid) and time.monotonic() < cleanup_deadline:
			time.sleep(0.01)
		assert not process_alive(pid)
	finally:
		if marker.exists() and process_alive(int(marker.read_text())):
			os.kill(int(marker.read_text()), signal.SIGTERM)


def test_overflow_never_exposes_native_output(tmp_path: Path) -> None:
	body = (
		"import sys; sys.stdout.write('sensitive-synthetic' * 10000); sys.stderr.write('private')"
	)
	with pytest.raises(EditError, match=r"^limits$"):
		run_process(
			[sys.executable, "-I", "-S", "-B", "-c", body],
			root=tmp_path,
			environment=dict(os.environ),
			deadline=time.monotonic() + 3,
			limit=1024,
			own_tree=True,
		)
