# Architecture map

Use this map before non-trivial codebase questions, design work, or code changes. Verify relevant claims in code.

## Navigation

- **CLI and orchestration** — `quality_gate/cli.py` parses commands; `quality_gate/runner.py` loads manifests and executes checks.
- **Shared policy** — `quality_gate/policy/ruff.toml` and `quality_gate/policy/mypy.ini` define the common static-analysis rules.
- **CI integration** — `.github/workflows/quality.yml` exposes the reusable workflow consumed by project CI callers.
- **Project contract** — `templates/quality-gate.toml` is the manifest shape generated for a project by `$setup-repo`.

## Flows

- **Local and CI checking** — `quality-gate check` → `quality_gate/runner.py` → Ruff, mypy, pytest.
- **Project bootstrap** — `$setup-repo` → `quality-gate.toml` and a CI caller → reusable `quality.yml`.
