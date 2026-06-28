import { useMemo } from 'react'
import { ClipboardCheck } from 'lucide-react'
import { useLiveRecords } from 'lemma-sdk/react'
import { lemmaClient } from '../lemma-client'
import { fmtMoney, fmtNum, fmtPct, errMessage } from '../lib'
import { Panel, EmptyState, StatusBadge, type StatusKind } from '../ui'

// M3.7 — the human go-live inbox, driven by Workflow B's `approval_requests`. Each row is a
// paper survivor that cleared the OFF-pod risk-officer review (paper-eval confirmed the backtest
// AND the one-shot holdout gate promoted) and now awaits the human FORM. The functional approval
// is the workflow FORM (`lemma workflows runs submit-form` or the Lemma app) — the pod never
// auto-approves (TEST-8). This view is the read-only evidence the approver decides from.
//
// `approval_requests` is the agent-EXCLUDED table that carries the HOLDOUT-derived verdict: it is
// granted to NO agent, so `desk`/RAG can never read it. The cockpit reads it under the operator's
// OWN identity (a human, not an agent), so surfacing the holdout verdict here is allowed — TEST-3
// holds. (Per the `backtests`-table comment: the holdout verdict lives only on this dedicated table.)

function text(v: unknown): string {
  return typeof v === 'string' ? v : ''
}

function num(v: unknown): number | null {
  if (v === null || v === undefined || v === '') return null
  const n = typeof v === 'number' ? v : Number(v)
  return Number.isFinite(n) ? n : null
}

const VERDICT_KIND: Record<string, StatusKind> = { promote: 'ok', revise: 'warn', reject: 'danger' }

interface Req {
  id: string
  strategy: string
  family: string
  market: string
  venue: string
  mode: string
  origin: string
  status: string
  paperSharpe: number | null
  backtestSharpe: number | null
  retention: number | null
  holdoutVerdict: string
  holdoutOos: number | null
  holdoutReason: string
  capital: string
  decidedAt: string
}

export function Approvals() {
  const reqs = useLiveRecords({
    client: lemmaClient,
    tableName: 'approval_requests',
    limit: 500,
    sort: [{ field: 'created_at', direction: 'desc' }],
  })
  const deps = useLiveRecords({ client: lemmaClient, tableName: 'deployments', limit: 500 })

  const capitalByDep = useMemo(() => {
    const m = new Map<string, string>()
    for (const d of deps.records) m.set(String(d.id), text(d.capital))
    return m
  }, [deps.records])

  const all = useMemo<Req[]>(
    () =>
      reqs.records.map(
        (r): Req => ({
          id: String(r.id),
          strategy: text(r.strategy_name) || '—',
          family: text(r.family),
          market: text(r.market),
          venue: text(r.venue),
          mode: text(r.mode) || 'paper',
          origin: text(r.origin),
          status: text(r.status) || 'pending',
          paperSharpe: num(r.paper_sharpe),
          backtestSharpe: num(r.backtest_sharpe),
          retention: num(r.sharpe_retention),
          holdoutVerdict: text(r.holdout_verdict),
          holdoutOos: num(r.holdout_oos_sharpe),
          holdoutReason: text(r.holdout_reason),
          capital: capitalByDep.get(String(r.deployment_id)) ?? '',
          decidedAt: text(r.decided_at),
        }),
      ),
    [reqs.records, capitalByDep],
  )

  const pending = useMemo(() => all.filter((r) => r.status === 'pending'), [all])
  const decided = useMemo(() => all.filter((r) => r.status !== 'pending').slice(0, 8), [all])

  const error = reqs.error || deps.error

  return (
    <div className="stack">
      <div className="snapshot-note">
        Workflow B — the per-strategy go-live gate. Each request cleared the off-pod risk-officer
        review (paper-eval + the one-shot holdout gate, TEST-3) and awaits your human FORM. The
        holdout verdict is shown to <strong>you</strong> (read under your own identity, never an
        agent — TEST-3). Approve via the workflow FORM (<code>lemma workflows runs submit-form</code>{' '}
        or the Lemma app); the pod never auto-approves.
      </div>

      {error ? (
        <Panel title="Approvals" icon={ClipboardCheck}>
          <div className="alert">Could not load approvals: {errMessage(error)}</div>
        </Panel>
      ) : reqs.isLoading ? (
        <Panel title="Approvals" icon={ClipboardCheck}>
          <div className="skeleton" style={{ height: 120 }} />
        </Panel>
      ) : pending.length ? (
        pending.map((r) => <RequestCard key={r.id} r={r} />)
      ) : (
        <Panel title="Approvals" icon={ClipboardCheck}>
          <EmptyState
            icon={ClipboardCheck}
            head="No deployments awaiting approval"
            sub="When a paper survivor clears the risk-officer review, it appears here for your go-live decision."
            hint="reads: approval_requests (pending) — agent-excluded; the holdout verdict is shown to the human only"
          />
        </Panel>
      )}

      {decided.length ? <DecidedList rows={decided} /> : null}
    </div>
  )
}

function RequestCard({ r }: { r: Req }) {
  return (
    <Panel
      title={r.strategy}
      icon={ClipboardCheck}
      action={
        <span className="row">
          <StatusBadge kind={r.mode === 'live' ? 'danger' : 'info'}>{r.mode}</StatusBadge>
          {r.origin === 'demo' ? <StatusBadge kind="neutral">demo</StatusBadge> : null}
          <StatusBadge kind="warn">pending approval</StatusBadge>
        </span>
      }
    >
      <div className="stack">
        <div className="kv">
          <dt>Venue · market</dt>
          <dd>
            {r.venue || '—'} · {r.market || '—'} · {r.family || '—'}
          </dd>
          <dt>Capital</dt>
          <dd>{r.capital ? fmtMoney(r.capital) : '—'}</dd>
        </div>
        <div className="evidence">
          <span className="muted">Paper-eval</span>
          <span className="mono">paper Sharpe {fmtNum(r.paperSharpe)}</span>
          <span className="mono">backtest {fmtNum(r.backtestSharpe)}</span>
          <span className="mono">retention {fmtPct(r.retention)}</span>
        </div>
        <div className="evidence">
          <span className="muted">Holdout gate (TEST-3)</span>
          <StatusBadge kind={VERDICT_KIND[r.holdoutVerdict] ?? 'neutral'}>
            {r.holdoutVerdict || '—'}
          </StatusBadge>
          <span className="mono">OOS Sharpe {fmtNum(r.holdoutOos)}</span>
        </div>
        {r.holdoutReason ? <div className="muted">{r.holdoutReason}</div> : null}
      </div>
    </Panel>
  )
}

function DecidedList({ rows }: { rows: Req[] }) {
  return (
    <Panel title="Recently decided" icon={ClipboardCheck}>
      <div className="stack">
        {rows.map((r) => (
          <div key={r.id} className="evidence">
            <StatusBadge kind={r.status === 'approved' ? 'ok' : 'danger'}>{r.status}</StatusBadge>
            <span className="mono">{r.strategy}</span>
            <span className="muted">
              {r.decidedAt ? r.decidedAt.slice(0, 19).replace('T', ' ') : ''}
            </span>
          </div>
        ))}
      </div>
    </Panel>
  )
}
