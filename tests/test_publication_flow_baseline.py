"""Related behavior tests."""

from __future__ import annotations

import pytest
from publication_flow_fixture import BASE
from test_publication_flow import (
	BaselineGitHub,
	_event,
	_publisher,
)

from quality_gate.publication import PublicationError


def test_stable_baseline_beyond_first_page_rejects_stale_candidate() -> None:
	api = BaselineGitHub("later-page")
	api.tags["v2.2.0"] = BASE
	with pytest.raises(PublicationError, match="stale"):
		_publisher(api).prepare(7, event=_event(api))
	assert api.writes == []
