"""Public edit-check file-evidence correction-loop regression."""

from __future__ import annotations

import time
from pathlib import Path

from edit_check_support78 import edit_cache as edit_cache
from edit_check_support78 import invoke, request


def test_read_only_validation_reuses_unchanged_file_results_and_keeps_other_failure(
	tmp_path: Path, edit_cache: Path
) -> None:
	paths = [f"file_{index}.py" for index in range(10)]
	for index, relative in enumerate(paths):
		content = b"value = 1\n"
		if index == 2:
			content = b"value = missing_two\n"
		elif index == 3:
			content = b"value = missing_three\n"
		(tmp_path / relative).write_bytes(content)
	payload = request(tmp_path, edit_cache, *paths)
	payload["phase"] = "validate"
	payload["deadline_unix_ms"] = int(time.time() * 1000) + 25_000

	first_exit, first = invoke(payload)

	assert first_exit == 1, first
	assert all(file["evidence"] == "executed" for file in first["files"])
	assert {finding["rule"] for finding in first["files"][2]["findings"]} == {"F821"}
	assert {finding["rule"] for finding in first["files"][3]["findings"]} == {"F821"}
	(tmp_path / paths[2]).write_bytes(b"value = 2\n")
	payload = request(tmp_path, edit_cache, *paths)
	payload["phase"] = "validate"
	payload["deadline_unix_ms"] = int(time.time() * 1000) + 25_000

	second_exit, second = invoke(payload)

	assert second_exit == 1, second
	assert sum(file["evidence"] == "reused" for file in second["files"]) == 9
	assert second["files"][2]["evidence"] == "executed"
	assert second["files"][2]["status"] == "passed"
	assert {finding["rule"] for finding in second["files"][3]["findings"]} == {"F821"}
	assert second["files"][3]["evidence"] == "reused"

	ci_exit, ci_result = invoke(payload, ci=True)
	assert ci_exit == 1, ci_result
	assert all(file["evidence"] == "executed" for file in ci_result["files"])
