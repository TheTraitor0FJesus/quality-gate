from pathlib import Path

from quality_gate import runner


def test_runner_stays_within_requested_size_limit() -> None:
	assert len(Path(runner.__file__).read_bytes()) <= 20_000
