# GitHub Issues

GitHub Issues are the required specification and ticket tracker for every project. Use `gh` for all issue operations.

Creating an Issue never starts an agent or implementation automatically. The user invokes `implement` or `implement-swarm` explicitly.

## Structure and language

- A feature specification is one parent Issue. Implementation tickets are native sub-issues of that parent.
- Keep the feature's established English kebab-case slug in square brackets at the start of every related title: `[feature-slug] <Russian title>`. Preserve that slug; keep the rest of the title in Russian.
- Write Issue body headings and content in English.
- A specification body follows the `to-spec` template. A ticket body follows the `to-tickets` template, including `Implementation complexity` and `Execution stages` alongside its outcome, ownership, and acceptance criteria. Add a `References` section only when it helps the implementer find required context.
- The Issue body defines the assignment. During implementation and completion, update only its state and status label. Preserve its complexity label as the original estimate. Keep results, verification evidence, and coordination in the conversation or PR. Update unstarted tickets with confirmed impacts.
- Keep dependencies in GitHub's native sub-issue and issue-dependency relationships. Do not duplicate blockers or parent references in the body; record material unresolved decisions as concrete questions or stage outcomes.

## Labels and state

Create or update the standard labels in each repository:

```sh
gh label create "Easy" --color 1D76DB --force
gh label create "Hard" --color 5319E7 --force
gh label create "status:todo" --color EDEDED --description "Work has not started" --force
gh label create "status:in-progress" --color FBCA04 --description "Work has started and the issue remains open" --force
gh label create "status:done" --color 0E8A16 --description "Work is complete" --force
```

Apply exactly one `Easy` or `Hard` label to every actionable Issue (`Low`/`Easy` recommends `gpt-6-luna` with `max`; `High`/`Hard` recommends `gpt-6.1-sol` with `high`). Implementation Issues match the label to the body rating; standalone specifications use the same complexity rubric. While open, apply exactly one status label; completed Issues are closed with `status:done`. Keep the complexity label through closure. New child Issues start with `status:todo`. A pure parent specification whose work is entirely represented by sub-issues receives no standard ticket labels. Do not add uncertainty or feature labels; the title prefix groups Issues.

- `status:todo` means the open ticket has not started; `status:in-progress` means work has started and remains unfinished. These labels, not assignees or label absence, define an open ticket's state.
- Closed with reason `completed` means implementation met its criteria and required verification, review, commits, and hooks succeeded. Replace `status:in-progress` with `status:done` after closing.

## Commands

- **Create an Issue:** `gh issue create --title "$title" --body "$body" --label "status:todo" --label "Hard"` for an actionable ticket or standalone specification. Omit standard ticket labels for a pure parent specification implemented through sub-issues.
- **Read an Issue:** `gh issue view <number> --comments` and inspect labels, parent, sub-issues, and dependencies.
- **List Issues:** `gh issue list --state open --json number,title,body,labels,comments` with appropriate filters.
- **Update an unstarted ticket:** `gh issue edit <number> --body "$body"`.
- **Apply or remove labels:** `gh issue edit <number> --add-label "..."` or `--remove-label "..."`.
- **Start implementation:** before work starts, ensure the Issue is open with exactly `status:in-progress`. Remove `status:todo`, `status:done`, and unsupported `status:*` labels; if the Issue is closed, reopen it first.
- **Complete implementation:** `gh issue close <number> --reason completed`, then `gh issue edit <number> --remove-label "status:in-progress" --add-label "status:done"`.
- **Reopen work:** `gh issue reopen <number>`, then replace `status:done` with `status:in-progress` before continuing.

GitHub infers the repository from the current clone when possible. Verify `git remote -v` and `gh auth status`; use `--repo <owner>/<repo>` when inference is ambiguous. If the project has no GitHub repository or the current account lacks access, stop and report the prerequisite. Do not choose another tracker.

### Parent and blocker relationships

After creating child Issues, link each to its parent:

```sh
gh api --method POST repos/{owner}/{repo}/issues/{parent_number}/sub_issues \
  -F sub_issue_id={child_database_id}
```

Add a native blocker relationship to the dependent Issue:

```sh
gh api --method POST repos/{owner}/{repo}/issues/{child_number}/dependencies/blocked_by \
  -F issue_id={blocker_database_id}
```

Find a database ID with `gh api repos/{owner}/{repo}/issues/<number> --jq .id`. Use the number in URL paths and the numeric database ID in the relationship body. Verify parent and blocker links after writing. If GitHub rejects a required relationship because repository access or GitHub support is missing, stop and report it; do not encode that relationship as local ticket metadata.

### Wayfinding operations

The Wayfinder map is a parent Issue labelled `wayfinder:map`; its child Issues are native sub-issues. This record-type label is scoped to the map parent. Actionable child Issues use only complexity and lifecycle labels.

- **Blocking:** use native GitHub issue dependencies as described above. A ticket is unblocked when every blocker is closed.
- **Frontier query:** list the map's child Issues with `status:todo` and satisfied blockers. An assignee does not define status or readiness.
- **Claim:** replace `status:todo` with `status:in-progress` and assign the owner with `gh issue edit <number> --add-assignee @me`.
- **Resolve:** close the child Issue as completed and replace `status:in-progress` with `status:done`.
