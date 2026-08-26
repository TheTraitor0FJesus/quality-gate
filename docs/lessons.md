# Escaped-defect lessons

An escaped defect is a defect that reached the default branch even though Quality Gate should
have caught it. Store one reviewable English Markdown file per incident in `lessons/`. The file
must use this front matter:

```markdown
---
id: incident-2026-01
status: open
incident: A defect reached the default branch.
expected_layer: repository.git.conflict_markers
miss_cause: The candidate check did not cover this input.
adaptation:
evidence:
---

# Incident

Add context and links to the incident here.
```

`status` is `open` or `learned`. Open lessons remain visible and do not block an ordinary
consumer remediation commit. The audit and Quality Gate release controller require every lesson
to contain both a concrete `adaptation` and verification `evidence`. A malformed or duplicate
lesson is unchecked and blocks completion. `lessons/README.md` is reserved for directory
guidance and is not parsed as a lesson. The Quality Gate release controller must call
`ensure_release_ready` before creating a Quality Gate release. Consumer policy sync deliberately
does not call this controller.
