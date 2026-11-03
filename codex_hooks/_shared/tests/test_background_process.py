"""Cooperative cancellation still reaps the supervised provider process tree."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from time import monotonic

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from _shared.quality_process import ProcessError, execute


class BackgroundProcessCancellation(unittest.TestCase):
	def test_already_cancelled_work_never_starts_a_process(self) -> None:
		cancel = threading.Event()
		cancel.set()
		with tempfile.TemporaryDirectory() as directory:
			marker = Path(directory) / "started"
			with self.assertRaisesRegex(ProcessError, "^cancelled$"):
				execute(
					[
						sys.executable,
						"-B",
						"-c",
						"import pathlib,sys; pathlib.Path(sys.argv[1]).touch()",
						str(marker),
					],
					deadline=monotonic() + 5,
					cancel_event=cancel,
				)
			self.assertFalse(marker.exists())

	@unittest.skipUnless(sys.platform == "win32", "Windows Job Object child cleanup")
	def test_cancellation_after_child_start_reaps_all_owned_processes(self) -> None:
		cancel, ready = threading.Event(), threading.Event()
		with tempfile.TemporaryDirectory() as directory:
			marker = Path(directory) / "child.pid"
			code = (
				"import pathlib,subprocess,sys,time; "
				"child=subprocess.Popen([sys.executable,'-B','-c','import time; time.sleep(30)']); "
				"pathlib.Path(sys.argv[1]).write_text(str(child.pid)); time.sleep(30)"
			)

			def cancel_started_work() -> None:
				deadline = monotonic() + 5
				while not marker.exists() and monotonic() < deadline:
					ready.wait(0.01)
				if marker.exists():
					ready.set()
					cancel.set()

			observer = threading.Thread(target=cancel_started_work)
			observer.start()
			try:
				with self.assertRaisesRegex(ProcessError, "^cancelled$"):
					execute(
						[sys.executable, "-B", "-c", code, str(marker)],
						deadline=monotonic() + 8,
						cancel_event=cancel,
					)
			finally:
				observer.join(timeout=6)
			self.assertFalse(observer.is_alive())
			self.assertTrue(
				ready.is_set(), "The provider child must really start before cancellation"
			)
			pid = int(marker.read_text(encoding="utf-8"))
			processes = subprocess.run(
				["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
				capture_output=True,
				text=True,
				timeout=5,
				check=False,
			)
			self.assertEqual(processes.returncode, 0)
			self.assertNotIn(f'"{pid}"', processes.stdout)


if __name__ == "__main__":
	unittest.main()
