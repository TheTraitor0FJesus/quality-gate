# Architecture map

Use this map before non-trivial codebase questions, design work, or code changes. Verify relevant claims in code.

## Navigation

- **CLI and orchestration** — `quality_gate/cli.py` parses commands; `quality_gate/runner.py` loads manifests and executes checks.
- **Shared policy** — `quality_gate/policy/ruff.toml` and `quality_gate/policy/mypy.ini` define the common static-analysis rules.
- **Local runtime** — `.venv/` contains the untracked Python environment used by the global Git hook.
- **CI integration** — `.github/workflows/quality.yml` exposes the reusable workflow from `main`; project CI callers use the same branch as the local hook.
- **Project contract** — `templates/quality-gate.toml` is the manifest shape generated for a project by `$setup-repo`.

## Flows

- **Local and CI checking** — `quality-gate check` → `quality_gate/runner.py` → Ruff, mypy, pytest.
- **Project bootstrap** — `$setup-repo` → `quality-gate.toml` and a CI caller → reusable `quality.yml`.
- **Policy rollout** — validated `quality-gate` change → commit and push to `main` → local hooks and CI callers use the new behavior.
