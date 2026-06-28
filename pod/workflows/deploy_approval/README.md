# Workflow B — `deploy_approval` (M3.7, the per-strategy human go-live gate)

The durable human approval gate: a paper survivor → risk-officer confirmation → **human FORM** →
the worker `start` command. Every go-live is this human decision; the pod never auto-approves.

## The split (why two halves)

The holdout lives in a no-ACL store the **pod must never reach** (TEST-3), so the risk-officer
confirmation runs **off-pod** and only a scalar verdict crosses onto the pod:

```
 off-pod (research plane, Mac CLI)                    on-pod (Vault)
 ─────────────────────────────────                   ──────────────────────────────────────────
 scripts/risk_officer_review.py                       approval_requests INSERT
   evaluate_paper_run  (paper ≈ backtest)   ──writes──▶  └─ deploy_approval_trigger (DATASTORE)
   HoldoutGate.evaluate (single legit          row          └─ deploy_approval workflow
     holdout read — TEST-3)                                       human_approval  FORM  ([You])
   RiskOfficerReview.request_fields                                 └─ finalize_deployment FUNCTION
     (summary scalars only — no series)                                  approve → `start` command
                                                                          reject  → record rejection
```

`commands.start` → the worker polls it and is the **sole** executor (TEST-8: the pod issues; the
worker never trades). `approval_requests` is **granted to no agent** — it carries the holdout-derived
verdict, which only the human approver / cockpit reads (the operator's own identity, never an agent).

## Resources (all in this bundle)

- `tables/approval_requests` — the agent-excluded approval queue (holdout verdict lives here).
- `functions/finalize_deployment` — the commit step (issues the `start` command / records rejection).
- `workflows/deploy_approval` — `FORM (human) → finalize`, triggered by `approval_requests` INSERT.
- `schedules/deploy_approval_trigger` — DATASTORE INSERT on `approval_requests` → the workflow.

Import (partial, in dependency order; always `--pod Vault`):

```bash
lemma pods import pod/tables/approval_requests          --pod Vault
lemma pods import pod/functions/finalize_deployment     --pod Vault
lemma pods import pod/workflows/deploy_approval          --pod Vault
lemma pods import pod/schedules/deploy_approval_trigger  --pod Vault
```

## Drive the demo + approve (the 3.GATE pipeline)

```bash
# 1. off-pod: write the request (a labelled TEST strategy; origin=demo). Triggers the FORM.
uv run python scripts/risk_officer_review.py --demo

# 2. [You] approve the test FORM (the run waiting in your queue):
lemma workflows runs waiting --pod Vault
lemma workflows runs submit-form <run-id> --data '{"approved": true, "notes": "3.GATE demo"}' --pod Vault
#   → finalize_deployment issues a `start` command on `commands` for alpha-paper-1.
```

For the worker to consume the `start` command, the box's `LEMMA_TOKEN` must be fresh (60-min). The
`--demo` deployment targets `worker_id=alpha-paper-1`; the assignee is the operator (POD_ADMIN).

> The demo's holdout verdict is **synthetic + labelled** (origin=demo, a `TEST-` name, the reason
> says so): it exercises the *plumbing*, not a real edge. A real survivor + the one-shot holdout read
> land with the M3.0 vendor data — the real path reuses `write_approval_request` with a
> `RiskOfficerReview` built from actual returns + `HoldoutGate` over the gate-only `HoldoutStore`.
