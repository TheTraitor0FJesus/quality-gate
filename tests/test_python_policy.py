from __future__ import annotations

import json
import subprocess
import sys
import tomllib
from pathlib import Path

POLICY = Path(__file__).parents[1] / "quality_gate" / "policy" / "ruff.toml"
MAX_COMPLEXITY = 10
MAX_STATEMENTS = 50


def _ruff_findings(path: Path) -> tuple[int, set[str]]:
	result = subprocess.run(
		[
			sys.executable,
			"-m",
			"ruff",
			"check",
			"--config",
			str(POLICY),
			"--output-format",
			"json",
			str(path),
		],
		capture_output=True,
		check=False,
		text=True,
	)
	return result.returncode, {finding["code"] for finding in json.loads(result.stdout)}


def test_policy_blocks_selected_high_signal_findings(tmp_path: Path) -> None:
	production = tmp_path / "app.py"
	production.write_text(
		"""import asyncio


def unsafe(values: list[str] = []) -> None:
\tasyncio.create_task(asyncio.sleep(0))
\tassert values
\texec(\"pass\")
""",
		encoding="utf-8",
	)

	returncode, findings = _ruff_findings(production)

	assert returncode == 1
	assert {"B006", "RUF006", "S101", "S102"} <= findings


def test_policy_preserves_valid_test_assertions_and_credentials(tmp_path: Path) -> None:
	test_file = tmp_path / "tests" / "test_example.py"
	test_file.parent.mkdir()
	test_file.write_text(
		"""def test_example() -> None:
\ttoken = \"fake-secret\"
\tassert token
""",
		encoding="utf-8",
	)

	returncode, findings = _ruff_findings(test_file)

	assert returncode == 0
	assert not {"S101", "S105", "S106", "S107"} & findings


def test_policy_keeps_explicit_function_maintainability_budgets() -> None:
	policy = tomllib.loads(POLICY.read_text(encoding="utf-8"))

	assert policy["lint"]["mccabe"]["max-complexity"] == MAX_COMPLEXITY
	assert policy["lint"]["pylint"]["max-statements"] == MAX_STATEMENTS
