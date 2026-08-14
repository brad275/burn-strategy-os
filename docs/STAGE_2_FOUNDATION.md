# Stage 2 Foundation

Status: implemented and locally verified on 2026-08-13.

## Delivered

- Python 3.9+ standard-library package with no runtime dependencies.
- Versioned project, cycle, attempt, quality, feedback, and memory contracts.
- Explicit project and cycle transition tables with optimistic revisions.
- Caller-supplied canonical storage root; no hardcoded vault path.
- Organisation/brand/project/cycle scoping and path traversal rejection.
- Atomic same-directory writes and immutable attempt/feedback/version records.
- One-active-cycle enforcement and retry-safe immutable attempt IDs.
- Quality findings with an attributable JSONL Learning Ledger.
- Human-decided memory proposals with partial approval, version history, stale-base protection, and merged current memory.
- Deterministic semantic validators aligned to Semantic Contract v1.1.
- Canonical index rebuild from files without a database.

## Intentionally deferred

- Provider/model calls and prompt execution.
- Source acquisition and source-content snapshots.
- Parallel orchestration and reconciliation execution.
- Model-based judgment rubrics.
- Strategy document assembly.
- Application API and Agentic Hub UI.
- Shared live-vault configuration or writes.
- `edge-new` inspection or integration.

## Verification

```bash
python3 -m unittest discover -s tests -t . -v
PYTHONPYCACHEPREFIX=/private/tmp/strategy-os-pycache python3 -m compileall -q src tests
```

Expected result: 26 tests pass and compilation exits successfully.

## Stage 3 entry conditions

- Human commit identity is configured locally before the first commit.
- A pilot brief is selected for fixture/evaluation work.
- Provider and web-research configuration is chosen without placing secrets in the repository.
- The Semantic Contract remains versioned; breaking field changes require a migration.
