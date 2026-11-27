"""Check current PR identity in a fresh, read-only CI job without running PR code."""

from __future__ import annotations

import argparse
import json
import subprocess
from typing import Any


def validate_pull(pull: dict[str, Any], base_ref: str, base_sha: str, head_sha: str) -> None:
	"""Reject retargeted, closed, rebased or stale-base requests."""
	if (
		pull.get("state") != "open"
		or pull.get("base", {}).get("ref") != base_ref
		or pull.get("base", {}).get("sha") != base_sha
		or pull.get("head", {}).get("sha") != head_sha
	):
		raise ValueError("The PR candidate changed; a fresh complete check is required.")


def main() -> None:
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--repository", required=True)
	parser.add_argument("--number", type=int, required=True)
	parser.add_argument("--base-ref", required=True)
	parser.add_argument("--base-sha", required=True)
	parser.add_argument("--head-sha", required=True)
	args = parser.parse_args()
	data = subprocess.check_output(
		["gh", "api", f"repos/{args.repository}/pulls/{args.number}"],
		timeout=30,
	)
	if len(data) > 1024 * 1024:
		raise ValueError("PR metadata exceeds its limit")
	validate_pull(json.loads(data), args.base_ref, args.base_sha, args.head_sha)


if __name__ == "__main__":
	main()
