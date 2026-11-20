"""Unknown Ruff policies and tools cannot acquire file-local reuse authority."""

import pytest

from quality_gate.evidence_identity import EngineIdentity, ruff_file_identity


@pytest.mark.parametrize(
	("policy_sha256", "tool_version"),
	[
		("f" * 64, "0.16.4"),
		("f49ac3e4baa6fdfdd66bb13f07d3ccaebe9174cd23af269fd7b55db30b0809af", "9.9.9"),
	],
)
def test_unreviewed_ruff_inputs_cannot_create_file_local_identity(
	policy_sha256: str, tool_version: str
) -> None:
	identity = ruff_file_identity(
		engine=EngineIdentity("source", "4.6.0", "a" * 64),
		repository="b" * 64,
		path="src/example.py",
		mode=0o644,
		content_sha256="c" * 64,
		tool_version=tool_version,
		tool_sha256="d" * 64,
		policy_release="v4.6.0",
		release_sha256="e" * 64,
		policy_sha256=policy_sha256,
		python_version="3.12",
		python_sha256="1" * 64,
		runtime_fingerprint="2" * 64,
		platform="test",
		configuration=[],
	)
	assert identity is None
