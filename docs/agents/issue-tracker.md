# Local issue tracker

Use `.scratch/<feature-slug>/` as the source of truth for project planning work.

## Publish

1. Store the feature specification as `.scratch/<feature-slug>/spec.md`.
2. Store one ticket per file under `.scratch/<feature-slug>/issues/`.
3. Number ticket files from `01` in dependency order and include each ticket's blocking IDs in its front matter and body.
4. Keep each ticket as one independently verifiable vertical slice.

Publication is complete when the specification and every approved ticket exist as separate local files and every blocking ID resolves to another ticket in the same feature directory.

## Execute

Work the frontier: start any `todo` ticket whose blockers are all `done`. Set its state to `in_progress` while it is active and to `done` only after its acceptance criteria pass. Preserve completed ticket files as the execution record.
