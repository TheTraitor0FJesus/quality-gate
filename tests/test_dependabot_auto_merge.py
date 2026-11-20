"""Protected Dependabot authorization and workflow-policy boundaries."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import textwrap
from pathlib import Path
from typing import Any

import pytest
from test_integrity_dependabot_review import _workflow_result

REPOSITORY = Path(__file__).resolve().parents[1]
PROTECTED_AUTO_MERGE = REPOSITORY / "templates" / "dependabot-auto-merge.yml"


@pytest.mark.parametrize("is_provider", [False, True])
@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_protected_auto_merge_requires_exact_provider_template(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch, is_provider: bool, newline: str
) -> None:
	monkeypatch.setattr("quality_gate.integrity._is_quality_gate_origin", lambda _root: is_provider)
	source = PROTECTED_AUTO_MERGE.read_text(encoding="utf-8").replace("\n", newline)
	result = _workflow_result(tmp_path, source, "dependabot-auto-merge.yml")
	assert result.status.value == ("passed" if is_provider else "failed")


@pytest.mark.parametrize(
	("before", "after"),
	[
		("and all(.[];", "and any(.[];"),
		("length > 0", "length >= 0"),
		("expectedHeadOid: $sha,", ""),
		('and .head.sha == $sha', ""),
		('.context == "Quality Gate"', "true"),
		("disablePullRequestAutoMerge", "enablePullRequestAutoMerge"),
		("permission-contents: write", "permission-administration: write"),
		("github-token:", "skip-verification: true #"),
		("version-update:semver-minor", "version-update:semver-major"),
		("steps.qualification.outputs.eligible == 'true'", "true"),
		("converted_to_draft, edited", "edited"),
	],
)
def test_protected_auto_merge_rejects_weakened_template(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch, before: str, after: str
) -> None:
	monkeypatch.setattr("quality_gate.integrity._is_quality_gate_origin", lambda _root: True)
	source = PROTECTED_AUTO_MERGE.read_text(encoding="utf-8")
	assert before in source
	result = _workflow_result(
		tmp_path, source.replace(before, after, 1), "dependabot-auto-merge.yml"
	)
	assert result.status.value == "failed"


def _protected_step(name: str) -> str:
	source = PROTECTED_AUTO_MERGE.read_text(encoding="utf-8")
	block = source.split(f"      - name: {name}\n", 1)[1].split("\n      - name:", 1)[0]
	return textwrap.dedent(block.split("        run: |\n", 1)[1])


def _run_protected_step(
	tmp_path: Path, name: str, values: dict[str, str]
) -> subprocess.CompletedProcess[str]:
	git = shutil.which("git")
	assert git, "Git is required by the repository contract"
	git_bash = Path(git).parent.parent / "bin" / "bash.exe"
	bash = str(git_bash) if os.name == "nt" and git_bash.is_file() else shutil.which("bash")
	assert bash and shutil.which("jq"), "Bash and jq are required for workflow verification"
	environment = os.environ.copy()
	environment.update(
		{
			"GITHUB_OUTPUT": (tmp_path / "outputs").as_posix(),
			"GH_CALLS": (tmp_path / "calls").as_posix(),
			"GH_REPO": "owner/repo",
			"PR_NUMBER": "42",
			"EXPECTED_HEAD": "a" * 40,
			"EXPECTED_NODE": "PR_example",
			"GRAPHQL_EXIT": "0",
			"READ_EXIT": "0",
			**values,
		}
	)
	cli_double = r"""
gh() {
  if [[ "$1 $2" == 'api graphql' || "$1 $2" == 'api --method' ]]; then
    printf '%s\n' "$*" >> "$GH_CALLS"
    return "$GRAPHQL_EXIT"
  elif [[ "$2" == */rules/branches/main ]]; then
    [[ "$READ_EXIT" == 0 ]] || return "$READ_EXIT"
    printf '%s' "$RULES"
  else
    [[ "$READ_EXIT" == 0 ]] || return "$READ_EXIT"
    printf '%s' "$PULL"
  fi
}
"""
	return subprocess.run(
		[bash, "--noprofile", "--norc", "-c", cli_double + _protected_step(name)],
		env=environment,
		text=True,
		capture_output=True,
		timeout=15,
		check=False,
	)


@pytest.mark.parametrize(
	("updates", "eligible"),
	[
		([{"dependencyName": "one", "updateType": "version-update:semver-patch"}], True),
		(
			[
				{
					"dependencyName": "one",
					"updateType": "version-update:semver-minor",
					"ghsaId": "GHSA-example",
				}
			],
			True,
		),
		(
			[
				{"dependencyName": "one", "updateType": "version-update:semver-minor"},
				{"dependencyName": "two", "updateType": "version-update:semver-patch"},
			],
			True,
		),
		(
			[
				{"dependencyName": "one", "updateType": "version-update:semver-patch"},
				{"dependencyName": "two", "updateType": "version-update:semver-major"},
			],
			False,
		),
		(
			[
				{"dependencyName": "one", "updateType": "version-update:semver-minor"},
				{"dependencyName": "two", "updateType": ""},
			],
			False,
		),
		(
			[
				{
					"dependencyName": "one",
					"updateType": "version-update:semver-major",
					"ghsaId": "GHSA-example",
				}
			],
			False,
		),
		([{"dependencyName": "one"}], False),
		([{"updateType": "version-update:semver-patch"}], False),
		([], False),
		({}, False),
		(["unexpected"], False),
		("not-json", False),
	],
)
def test_protected_auto_merge_classifies_every_member(
	tmp_path: Path, updates: object, eligible: bool
) -> None:
	result = _run_protected_step(
		tmp_path,
		"Classify the complete update set",
		{"UPDATED_DEPENDENCIES": updates if isinstance(updates, str) else json.dumps(updates)},
	)
	assert result.returncode == 0, result.stderr
	assert (
		(tmp_path / "outputs").read_text(encoding="utf-8").strip()
		== f"eligible={str(eligible).lower()}"
	)


@pytest.mark.parametrize(
	"change",
	["none", "head", "author", "fork", "base", "draft", "closed", "rules", "read", "mutation"],
)
def test_protected_auto_merge_binds_head_and_requires_protection(
	tmp_path: Path, change: str
) -> None:
	pull: dict[str, Any] = {
		"node_id": "PR_example",
		"state": "open",
		"draft": False,
		"user": {"login": "dependabot[bot]", "type": "Bot"},
		"head": {"sha": "a" * 40, "repo": {"full_name": "owner/repo"}},
		"base": {"ref": "main", "repo": {"full_name": "owner/repo"}},
	}
	if change == "head":
		pull["head"]["sha"] = "b" * 40
	elif change == "author":
		pull["user"]["login"] = "attacker"
	elif change == "fork":
		pull["head"]["repo"]["full_name"] = "attacker/repo"
	elif change == "base":
		pull["base"]["ref"] = "other"
	elif change == "draft":
		pull["draft"] = True
	elif change == "closed":
		pull["state"] = "closed"
	rules = [
		{
			"type": "required_status_checks",
			"parameters": {
				"strict_required_status_checks_policy": True,
				"required_status_checks": [{"context": "Quality Gate"}],
			},
		}
	]
	result = _run_protected_step(
		tmp_path,
		"Enable protected auto-merge for the verified head",
		{
			"PULL": json.dumps(pull),
			"RULES": json.dumps([] if change == "rules" else rules),
			"GRAPHQL_EXIT": "23" if change == "mutation" else "0",
			"READ_EXIT": "17" if change == "read" else "0",
		},
	)
	assert (result.returncode == 0) == (change == "none"), result.stderr
	calls = tmp_path / "calls"
	if change in {"none", "mutation"}:
		call = calls.read_text(encoding="utf-8")
		assert "enablePullRequestAutoMerge" in call
		assert "expectedHeadOid: $sha" in call
		assert f"sha={'a' * 40}" in call
	else:
		assert not calls.exists()


@pytest.mark.parametrize("change", ["none", "draft", "base", "head", "mutation"])
def test_protected_auto_merge_revokes_previous_authorization(
	tmp_path: Path, change: str
) -> None:
	pull = {
		"node_id": "PR_example",
		"state": "open",
		"draft": change == "draft",
		"user": {"login": "dependabot[bot]", "type": "Bot"},
		"head": {
			"sha": ("b" if change == "head" else "a") * 40,
			"repo": {"full_name": "owner/repo"},
		},
		"base": {
			"ref": "other" if change == "base" else "main",
			"repo": {"full_name": "owner/repo"},
		},
		"auto_merge": {"merge_method": "squash"},
	}
	result = _run_protected_step(
		tmp_path,
		"Reset prior authorization and capture the current candidate",
		{"PULL": json.dumps(pull), "GRAPHQL_EXIT": "23" if change == "mutation" else "0"},
	)
	if change == "head":
		assert result.returncode != 0
		assert not (tmp_path / "calls").exists()
	elif change == "mutation":
		assert result.returncode == 23
		assert not (tmp_path / "outputs").exists()
	else:
		assert result.returncode == 0, result.stderr
		assert "disablePullRequestAutoMerge" in (tmp_path / "calls").read_text(encoding="utf-8")
		eligible = change == "none"
		assert (
			f"eligible={str(eligible).lower()}"
			in (tmp_path / "outputs").read_text(encoding="utf-8")
		)


@pytest.mark.parametrize("change", ["none", "requested", "head", "draft", "base", "read", "write"])
def test_manual_update_review_is_current_and_idempotent(tmp_path: Path, change: str) -> None:
	pull = {
		"state": "open",
		"draft": change == "draft",
		"head": {"sha": ("b" if change == "head" else "a") * 40},
		"base": {"ref": "other" if change == "base" else "main"},
		"requested_reviewers": [{"login": "owner"}] if change == "requested" else [],
	}
	result = _run_protected_step(
		tmp_path,
		"Request owner review for manual updates",
		{
			"PULL": json.dumps(pull),
			"REVIEWER": "owner",
			"READ_EXIT": "17" if change == "read" else "0",
			"GRAPHQL_EXIT": "23" if change == "write" else "0",
		},
	)
	assert (result.returncode == 0) == (change in {"none", "requested"}), result.stderr
	if change in {"none", "write"}:
		call = (tmp_path / "calls").read_text(encoding="utf-8")
		assert "repos/owner/repo/pulls/42/requested_reviewers" in call
		assert "reviewers[]=owner" in call
	else:
		assert not (tmp_path / "calls").exists()
