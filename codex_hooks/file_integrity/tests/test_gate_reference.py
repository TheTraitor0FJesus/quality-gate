"""Characterize immutable Gate semantics; this is not a runtime provider adapter.

Set INTEGRITY_REFERENCE_RELEASE explicitly for offline integration acceptance.
The corpus is reusable by the future supported touched-file provider tests.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import tomllib
import unittest
from datetime import date, timedelta
from pathlib import Path
from typing import Any

CHECK_ID = "repository.git.conflict_markers"
CORPUS = Path(__file__).with_name("marker_cases.json")
REFERENCE_CODE = """
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from quality_gate.contracts import load_manifest, ValidationError
from quality_gate.integrity import git_integrity_results
results = []
for project in json.load(sys.stdin):
    try:
        manifest = load_manifest(Path(project))
    except ValidationError:
        results.append({"status": "invalid_manifest", "findings": []})
        continue
    result = next(item for item in git_integrity_results(Path(project) / "candidate", manifest)
        if item.check_id == "repository.git.conflict_markers")
    results.append({"status": result.status.value,
        "findings": [{"path": item.path, "line": item.line} for item in result.findings],
        "waiver_target": result.waiver_target})
print(json.dumps(results))
"""


class ReleasedGateReference(unittest.TestCase):
	wheel: Path
	release: str

	@classmethod
	def setUpClass(cls) -> None:
		release = os.environ.get("INTEGRITY_REFERENCE_RELEASE")
		if not release:
			raise unittest.SkipTest("Select an immutable Gate release for reference integration")
		if not re.fullmatch(r"v\d+\.\d+\.\d+", release):
			raise ValueError("Invalid reference release identity")
		local = os.environ.get("LOCALAPPDATA")
		if not local:
			raise ValueError("Reference integration requires the native Gate release cache")
		root = Path(local) / "quality-gate" / "releases" / release
		document = tomllib.loads((root / "release.toml").read_text(encoding="utf-8"))
		if document["release"]["version"] != release:
			raise ValueError("Reference cache identity mismatch")
		wheels = [
			item
			for item in document["release"]["files"]
			if item["path"].startswith("quality_gate-") and item["path"].endswith(".whl")
		]
		if len(wheels) != 1:
			raise ValueError("Reference cache must identify exactly one engine wheel")
		entry = wheels[0]
		wheel = root / entry["path"]
		if wheel.name != entry["path"] or wheel.is_symlink():
			raise ValueError("Unsafe reference wheel path")
		if hashlib.sha256(wheel.read_bytes()).hexdigest() != entry["sha256"]:
			raise ValueError("Reference wheel failed immutable inventory verification")
		cls.wheel = wheel
		cls.release = release

	def project(self, root: Path, files: dict[str, bytes], waiver: str = "") -> Path:
		(root / "candidate").mkdir(parents=True)
		for name, content in files.items():
			path = root / "candidate" / name
			path.parent.mkdir(parents=True, exist_ok=True)
			path.write_bytes(content)
		(root / "quality-gate.toml").write_text(
			(waiver or "waivers = []\n")
			+ f'[quality]\nschema = 2\npolicy_release = "{self.release}"\n'
			+ '[repository]\nname = "integrity-reference"\ndomains = ["repository"]\n'
			+ 'required_documents = ["quality-gate.toml"]\n',
			encoding="utf-8",
		)
		return root

	def reference(self, roots: list[Path]) -> list[dict[str, Any]]:
		result = subprocess.run(
			[sys.executable, "-I", "-B", "-c", REFERENCE_CODE, str(self.wheel)],
			input=json.dumps([str(root) for root in roots]),
			capture_output=True,
			text=True,
			encoding="utf-8",
			timeout=15,
			check=False,
		)
		self.assertEqual(result.returncode, 0, result.stderr)
		return json.loads(result.stdout)

	def test_real_gate_marker_and_encoding_corpus(self) -> None:
		cases = json.loads(CORPUS.read_text(encoding="utf-8"))
		with tempfile.TemporaryDirectory() as directory:
			roots = []
			for case in cases:
				content = bytes.fromhex(case["hex"]) if "hex" in case else case["text"].encode()
				roots.append(
					self.project(Path(directory) / case["name"], {"nested/merge.py": content})
				)
			results = self.reference(roots)
			self.assertEqual(len(results), len(cases))
			for case, result in zip(cases, results, strict=True):
				with self.subTest(case=case["name"], release=self.release):
					self.assertEqual(result["status"], "failed" if case["lines"] else "passed")
					self.assertEqual(
						result["findings"],
						[{"path": "nested/merge.py", "line": line} for line in case["lines"]],
					)

	def test_real_gate_exact_waiver_multiple_paths_and_expiry(self) -> None:
		today = date.today()

		def waiver(target: str, expires: date) -> str:
			return (
				f'[[waivers]]\nkind = "standard"\ncheck_id = "{CHECK_ID}"\n'
				f'target = "{target}"\nreason = "synthetic reference fixture"\n'
				'approved_by = "fixture-owner"\n'
				f'reviewed_on = "{today - timedelta(days=2)}"\nexpires_on = "{expires}"\n'
			)

		with tempfile.TemporaryDirectory() as directory:
			base = Path(directory)
			files = {"merge.py": b"<<<<<<< HEAD\n", "notes/other.md": b"=======\n"}
			roots = [
				self.project(
					base / "exact", {"merge.py": files["merge.py"]}, waiver("merge.py", today)
				),
				self.project(base / "partial", files, waiver("merge.py", today)),
				self.project(base / "mismatch", files, waiver("missing.py", today)),
				self.project(
					base / "expired", files, waiver("merge.py", today - timedelta(days=1))
				),
			]
			results = self.reference(roots)
			self.assertEqual(
				[item["status"] for item in results],
				[
					"waived",
					"failed",
					"failed",
					"invalid_manifest",
				],
			)
			self.assertEqual(results[0]["waiver_target"], "merge.py")
			self.assertEqual(results[1]["findings"], [{"path": "notes/other.md", "line": 1}])
			self.assertEqual(
				results[2]["findings"],
				[
					{"path": "merge.py", "line": 1},
					{"path": "notes/other.md", "line": 1},
				],
			)


if __name__ == "__main__":
	unittest.main()
