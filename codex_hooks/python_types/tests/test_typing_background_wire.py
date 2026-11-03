"""The additive background envelope retains independent freshness validation."""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from time import monotonic

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from python_types.results import freshness_digest, validate_result
from python_types.tests.result_fixture import certificate


class BackgroundWire(unittest.TestCase):
	def test_cancelled_freshness_validation_never_opens_a_large_inventory(self) -> None:
		from python_types.freshness import cancellation_scope

		with tempfile.TemporaryDirectory() as directory:
			request, report = certificate(Path(directory).resolve() / "project")
			cancel = threading.Event()
			cancel.set()
			with cancellation_scope(cancel):
				result = validate_result(json.dumps(report), 0, request, "v9.0.0", monotonic() + 10)
			self.assertEqual((result["status"], result["reason"]), ("unchecked", "cancelled"))

	def test_schema_two_matches_event_and_rejects_replayed_or_stale_results(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory).resolve() / "project"
			request, report = certificate(root)
			identity = {
				"schema": 2,
				"mode": "background",
				"event_id": "12345678-1234-4234-8234-123456789abc",
			}
			request.update(identity)
			report.update(identity)
			report["freshness_sha256"] = freshness_digest(report)
			self.assertEqual(
				validate_result(json.dumps(report), 0, request, "v9.0.0", monotonic() + 10)[
					"status"
				],
				"passed",
			)
			request["event_id"] = "12345678-1234-4234-8234-123456789abd"
			self.assertEqual(
				validate_result(json.dumps(report), 0, request, "v9.0.0", monotonic() + 10)[
					"reason"
				],
				"invalid_provider_result",
			)
			request.update(identity)
			(root / "consumer.py").write_bytes(b"VALUE: str = 'changed'\n")
			self.assertEqual(
				validate_result(json.dumps(report), 0, request, "v9.0.0", monotonic() + 10)[
					"reason"
				],
				"stale_result",
			)


if __name__ == "__main__":
	unittest.main()
