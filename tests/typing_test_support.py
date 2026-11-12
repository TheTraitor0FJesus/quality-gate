"""Independent full-component typing comparison helper."""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any


def full_component_diagnostics(
	root: Path,
	policy: Path,
	scope: str,
	python: Path | None = None,
	metrics: dict[str, int] | None = None,
) -> list[dict[str, Any]]:
	"""Independent same-release full directory invocation, without edit routing."""
	python = python or root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
	with tempfile.TemporaryDirectory(prefix="typing-full-") as cache:
		completed = subprocess.run(
			[
				str(python),
				"-I",
				"-B",
				"-m",
				"mypy",
				"--config-file",
				str(policy / "quality_gate/policy/mypy.ini"),
				"--show-column-numbers",
				"--show-error-codes",
				"--no-pretty",
				"--no-error-summary",
				"--cache-dir",
				cache,
				*(["--verbose"] if metrics is not None else []),
				scope,
			],
			cwd=root,
			capture_output=True,
			check=False,
			timeout=30,
		)
	assert completed.returncode in {0, 1}
	if metrics is None:
		assert not completed.stderr
	else:
		graph_match = re.search(rb"Loaded graph with (\d+) nodes", completed.stderr)
		assert graph_match is not None, "Real mypy must report a completed import graph."
		metrics["mypy_graph_nodes"] = int(graph_match[1])
	diagnostics = []
	for line in completed.stdout.decode().splitlines():
		match = re.fullmatch(r"(.+?):(\d+):(\d+): error: .*  \[([a-z0-9-]+)\]", line)
		if match:
			diagnostics.append(
				{
					"path": Path(match[1]).as_posix().removeprefix("./"),
					"line": int(match[2]),
					"column": int(match[3]),
					"code": match[4],
				}
			)
	return sorted(
		diagnostics, key=lambda item: (item["path"], item["line"], item["column"], item["code"])
	)
