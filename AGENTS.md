# QuantLab Agent Guidance

## Document authority

- Use `PRODUCT_STRATEGY.md` for product intent, non-goals, and compliance boundaries.
- Start an external review from `docs/AI_HANDOVER.md`; it gives the reading order,
  code map, dynamic checks, and acceptance checklist without replacing primary evidence.
- Use current code, schemas, APIs, and tests to decide whether a capability exists.
- Use the production database, `quantlab runtime-status`, and matching `*-latest.json`
  reports for runtime, data, experiment, sample, and quality claims.
- Use `PROJECT_HANDBOOK.md` and `docs/README.md` for the current user-facing map.
- Treat `docs/BACKEND_ROUND*.md`, dated acceptance reports, postmortems, and roadmaps as
  historical evidence only. They never override current code or live evidence.

Never infer current readiness from a static Markdown count. Do not rewrite recovery evidence as
first-window success, backfill formal samples, or claim profitability from engineering tests.

Before accepting a dynamic claim, record the database path, query or command, observation time,
source fingerprint when available, and the boundary of what the evidence does not prove. A Job
with `status=completed` proves only that its worker returned successfully.

The worktree may be modified by other agents. Preserve concurrent changes and avoid broad rewrites
outside the requested scope.
