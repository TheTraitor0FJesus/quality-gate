"""Measure the public provider on four disposable prepared-cache profiles."""

from __future__ import annotations

import hashlib
import json
import math
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from quality_gate.distribution import default_cache_root, verify_release
from quality_gate.edit_protocol import MAX_DEADLINE_MS

SAMPLES = 30
SINGLE_BYTES = 8192
BATCH_FILE_BYTES = 100_000
BATCH_FILES = 10
SOURCE_ROOT = Path(__file__).resolve().parents[1]


def _measure(root: Path, release: Path, paths: list[str], iteration: int) -> dict[str, int]:
	# Alternate real clean/failing bytes; changing every file prevents accidental reuse.
	credential = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
	matched = iteration % 2 == 0
	size = SINGLE_BYTES if len(paths) == 1 else BATCH_FILE_BYTES
	for index, path in enumerate(paths):
		body = f"value = {iteration}\n".encode()
		if matched and index == 0:
			body += f"credential = '{credential}'\n".encode()
		body += b"#" + b" " * (size - len(body) - 2) + b"\n"
		(root / path).write_bytes(body)
	request = {
		"schema": 1,
		"root": str(root.resolve()),
		"files": [
			{"path": path, "sha256": hashlib.sha256((root / path).read_bytes()).hexdigest()}
			for path in paths
		],
		"baseline": {"policy_release": release.name},
		"cache_dir": str(release.parent.parent),
		"deadline_unix_ms": int(time.time() * 1000) + MAX_DEADLINE_MS,
	}
	started = time.monotonic()
	completed = subprocess.run(
		[
			sys.executable,
			"-I",
			"-B",
			"-c",
			"import sys; sys.path.insert(0, sys.argv.pop(1)); "
			"from quality_gate.cli import main; raise SystemExit(main())",
			str(SOURCE_ROOT),
			"edit-secrets",
		],
		cwd=root,
		input=json.dumps(request).encode(),
		capture_output=True,
		timeout=13,
		check=False,
	)
	elapsed = round((time.monotonic() - started) * 1000)
	if completed.stderr or credential.encode() in completed.stdout:
		raise RuntimeError("Provider response failed the source-free boundary.")
	result = json.loads(completed.stdout)
	if completed.returncode != int(matched) or result["status"] != (
		"failed" if matched else "passed"
	):
		raise RuntimeError(
			f"Profile {root.name}, sample {iteration}: exit {completed.returncode}, "
			f"status {result.get('status')}, reason {result.get('reason')}, elapsed {elapsed} ms."
		)
	if len(result["files"]) != len(paths) or elapsed >= MAX_DEADLINE_MS:
		raise RuntimeError("Provider profile lost coverage or exceeded its work deadline.")
	return {
		"outer_ms": elapsed,
		"provider_ms": result["elapsed_ms"],
		"processes": result["process_count"],
	}


def main() -> None:
	"""Print source-free samples; fail if any real executable invocation is incomplete."""
	release = default_cache_root() / "releases/v2.4.2"
	verified = verify_release(release)
	profiles: list[dict[str, Any]] = []
	with tempfile.TemporaryDirectory(prefix="quality-gate-secret-profile-") as directory:
		for managed in (True, False):
			for count in (1, BATCH_FILES):
				name = f"{'managed' if managed else 'baseline'}-{count}"
				root = Path(directory) / name
				root.mkdir()
				if managed:
					(root / "quality-gate.toml").write_text(
						f'waivers = []\n[quality]\nschema = 2\npolicy_release = "{release.name}"\n'
						'[repository]\nname = "profile"\ndomains = ["repository"]\n'
						'required_documents = ["AGENTS.md"]\n',
						encoding="utf-8",
					)
				paths = [f"file_{index}.py" for index in range(count)]
				samples = [_measure(root, release, paths, index) for index in range(SAMPLES + 1)]
				warm = sorted(sample["outer_ms"] for sample in samples[1:])
				profiles.append(
					{
						"profile": name,
						"bytes": SINGLE_BYTES if count == 1 else count * BATCH_FILE_BYTES,
						"first_ms": samples[0]["outer_ms"],
						"warm_p50_ms": warm[math.ceil(SAMPLES * 0.50) - 1],
						"warm_p95_ms": warm[math.ceil(SAMPLES * 0.95) - 1],
						"warm_max_ms": warm[-1],
						"warm_samples": SAMPLES,
						"failed_samples": math.ceil((SAMPLES + 1) / 2),
						"clean_samples": (SAMPLES + 1) // 2,
						"process_counts": sorted({sample["processes"] for sample in samples}),
					}
				)
				sys.stdout.write(json.dumps(profiles[-1], sort_keys=True) + "\n")
				sys.stdout.flush()
	sys.stdout.write(
		json.dumps(
			{
				"measurement": (
					"provider launch-to-delivery; prepared cache; excludes host Ruff/event"
				),
				"python": sys.version.split()[0],
				"release": verified.version,
				"release_sha256": hashlib.sha256(
					(release / "release.toml").read_bytes()
				).hexdigest(),
				"profiles": profiles,
			},
			sort_keys=True,
		)
	)


if __name__ == "__main__":
	main()
