from __future__ import annotations

import copy
from collections.abc import Mapping
from pathlib import Path

import pytest

from quality_gate.integrity import _parse_workflow, _single_workflow_findings
from quality_gate.publication import PUBLICATION, PublicationError, parse_decision
from scripts.dependabot_release import DECISION, update_decision


class FakePullAPI:
	def __init__(self, body: str | None = "Dependabot changelog") -> None:
		self.pull: dict[str, object] = {
			"number": 12,
			"state": "open",
			"user": {"login": "dependabot[bot]", "type": "Bot"},
			"head": {"sha": "a" * 40},
			"base": {"ref": "main"},
			"body": body,
		}
		self.patches = 0

	def get(self, path: str) -> object:
		assert path == "/pulls/12"
		return copy.deepcopy(self.pull)

	def patch(self, path: str, payload: Mapping[str, object]) -> dict[str, object]:
		assert path == "/pulls/12"
		self.patches += 1
		self.pull.update(payload)
		return copy.deepcopy(self.pull)


@pytest.mark.parametrize("body", [None, "", "Existing notes\n\n<details>details</details>"])
def test_dependabot_decision_preserves_body_and_is_idempotent(body: str | None) -> None:
	api = FakePullAPI(body)
	assert update_decision(api, 12)
	result = api.pull["body"]
	assert isinstance(result, str)
	assert result.startswith(body or "")
	assert result.endswith(DECISION)
	assert set(parse_decision(result)) == {"No release"}
	assert not update_decision(api, 12)
	assert api.patches == 1


@pytest.mark.parametrize(
	"body",
	[
		"## Release\n\n- No release: ранее\n\n## Release\n\n- No release: два\n",
		"## Release\n\n- Version: 1.0.0\n",
		"```\nunfinished fence",
	],
)
def test_ambiguous_body_is_not_overwritten(body: str) -> None:
	api = FakePullAPI(body)
	with pytest.raises(PublicationError):
		update_decision(api, 12)
	assert api.pull["body"] == body
	assert api.patches == 0


def test_existing_release_decision_is_not_overwritten() -> None:
	body = f"## Release\n- Version: 1.0.0\n- Changes: example\n- Publication: {PUBLICATION}\n"
	api = FakePullAPI(body)
	with pytest.raises(PublicationError, match="explicit release decision"):
		update_decision(api, 12)
	assert api.pull["body"] == body
	assert api.patches == 0


@pytest.mark.parametrize(
	"author", [{"login": "dependabot[bot]", "type": "User"}, {"login": "other", "type": "Bot"}]
)
def test_other_authors_cannot_receive_automatic_decision(author: dict[str, str]) -> None:
	api = FakePullAPI()
	api.pull["user"] = author
	with pytest.raises(PublicationError, match="not an open Dependabot"):
		update_decision(api, 12)
	assert api.patches == 0


def test_changed_body_is_not_overwritten() -> None:
	class ConcurrentEdit(FakePullAPI):
		def __init__(self) -> None:
			super().__init__()
			self.reads = 0

		def get(self, path: str) -> object:
			self.reads += 1
			if self.reads > 1:
				self.pull["body"] = "Owner edited the description"
			return super().get(path)

	api = ConcurrentEdit()
	with pytest.raises(PublicationError, match="changed while"):
		update_decision(api, 12)
	assert api.patches == 0


def test_app_workflow_accepts_only_exact_provider_template(tmp_path: Path) -> None:
	root = Path(__file__).resolve().parents[1]
	relative = ".github/workflows/dependabot-release-decision.yml"
	content = (root / relative).read_text(encoding="utf-8")
	path = tmp_path / "decision.yml"
	path.write_text(content, encoding="utf-8")
	workflow = _parse_workflow(path, relative)
	assert not _single_workflow_findings(workflow, True)
	assert _single_workflow_findings(workflow, False)
	path.write_text(
		content.replace("permission-pull-requests: write", "permission-contents: write"),
		encoding="utf-8",
	)
	assert _single_workflow_findings(_parse_workflow(path, relative), True)
