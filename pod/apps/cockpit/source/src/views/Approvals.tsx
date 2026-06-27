import { useMemo } from 'react'
import { ClipboardCheck } from 'lucide-react'
import { useLiveRecords } from 'lemma-sdk/react'
import { lemmaClient } from '../lemma-client'
import { fmtMoney, fmtNum, errMessage } from '../lib'
import { Panel, EmptyState, StatusBadge } from '../ui'

// M2.4 — the human go-live inbox. Deployments awaiting approval, each with its
// review context: the strategy, the capital + risk limits it would run under, and
// the backtest evidence. Read-only here by design: the *functional* approval is
// Workflow B (risk-officer review → one-shot holdout gate → human FORM → start
// command), which lands in Phase 3 (M3.7). The pod never auto-approves.

function text(v: unknown): string {
  return typeof v === 'string' ? v : ''
}

function isObject(v: unknown): v is Record<string, unknown> {
  return v !== null && typeof v === 'object' && !Array.isArray(v)
}

function num(v: unknown): number | null {
  if (v === null || v === undefined || v === '') return null
  const n = typeof v === 'number' ? v : Number(v)
  return Number.isFinite(n) ? n : null
}

interface Pending {
  id: string
  strategy: string
  family: string
  market: string
  venue: string
  mode: string
  capital: string
  riskLimits: Array<[string, string]>
  sharpe: number | null
  dsr: number | null
  pbo: number | null
}

export function Approvals() {
  const deps = useLiveRecords({
    client: lemmaClient,
    tableName: 'deployments',
    limit: 500,
    sort: [{ field: 'created_at', direction: 'desc' }],
  })
  const strat = useLiveRecords({ client: lemmaClient, tableName: 'strategies', limit: 500 })
  const bt = useLiveRecords({ client: lemmaClient, tableName: 'backtests', limit: 500 })

  const stratById = useMemo(() => {
    const m = new Map<string, Record<string, unknown>>()
    for (const s of strat.records) m.set(String(s.id), s)
    return m
  }, [strat.records])

  // latest backtest per strategy (records arrive newest-first if sorted; we just
  // keep the first seen per strategy_id as the representative evidence)
  const btByStrategy = useMemo(() => {
    const m = new Map<string, Record<string, unknown>>()
    for (const b of bt.records) {
      const sid = String(b.strategy_id ?? '')
      if (sid && !m.has(sid)) m.set(sid, b)
    }
    return m
  }, [bt.records])

  const pending = useMemo<Pending[]>(() => {
    return deps.records
      .filter((d) => text(d.status) === 'pending_approval')
      .map((d): Pending => {
        const sid = String(d.strategy_id ?? '')
        const s = stratById.get(sid)
        const b = btByStrategy.get(sid)
        const limits = isObject(d.risk_limits) ? d.risk_limits : {}
        return {
          id: String(d.id),
          strategy: (typeof s?.name === 'string' && s.name) || '—',
          family: String(s?.family ?? ''),
          market: String(s?.market ?? ''),
          venue: text(d.venue),
          mode: text(d.mode) || 'paper',
          capital: text(d.capital),
          riskLimits: Object.entries(limits).map(([k, v]) => [k, String(v)] as [string, string]),
          sharpe: b ? num(b.sharpe) : null,
          dsr: b ? num(b.deflated_sharpe) : null,
          pbo: b ? num(b.cpcv_pbo) : null,
        }
      })
  }, [deps.records, stratById, btByStrategy])

  const error = deps.error || strat.error || bt.error

  return (
    <div className="stack">
      <div className="snapshot-note">
        The go-live approval FORM — Workflow B (risk-officer review → one-shot holdout gate → human FORM →
        start command) — lands here in <strong>Phase 3</strong> (M3.7). Every go-live is a human decision;
        the pod never auto-approves. This inbox shows deployments awaiting that approval.
      </div>

      {error ? (
        <Panel title="Approvals" icon={ClipboardCheck}>
          <div className="alert">Could not load approvals: {errMessage(error)}</div>
        </Panel>
      ) : deps.isLoading ? (
        <Panel title="Approvals" icon={ClipboardCheck}>
          <div className="skeleton" style={{ height: 120 }} />
        </Panel>
      ) : pending.length ? (
        pending.map((p) => (
          <Panel
            key={p.id}
            title={p.strategy}
            icon={ClipboardCheck}
            action={
              <span className="row">
                <StatusBadge kind={p.mode === 'live' ? 'danger' : 'info'}>{p.mode}</StatusBadge>
                <StatusBadge kind="warn">pending approval</StatusBadge>
              </span>
            }
          >
            <div className="stack">
              <div className="kv">
                <dt>Venue · market</dt>
                <dd>
                  {p.venue || '—'} · {p.market || '—'} · {p.family || '—'}
                </dd>
                <dt>Capital</dt>
                <dd>{p.capital ? fmtMoney(p.capital) : '—'}</dd>
                {p.riskLimits.map(([k, v]) => (
                  <FragmentRow key={k} k={k} v={v} />
                ))}
              </div>
              <div className="evidence">
                <span className="muted">Backtest evidence</span>
                <span className="mono">Sharpe {fmtNum(p.sharpe)}</span>
                <span className="mono">DSR {fmtNum(p.dsr)}</span>
                <span className="mono">PBO {fmtNum(p.pbo)}</span>
              </div>
            </div>
          </Panel>
        ))
      ) : (
        <Panel title="Approvals" icon={ClipboardCheck}>
          <EmptyState
            icon={ClipboardCheck}
            head="No deployments awaiting approval"
            sub="When the discovery loop proposes a survivor for go-live, it appears here for your review and approval."
            hint="reads: deployments (pending_approval) ⋈ strategies ⋈ backtests"
          />
        </Panel>
      )}
    </div>
  )
}

function FragmentRow({ k, v }: { k: string; v: string }) {
  return (
    <>
      <dt>{k.replace(/_/g, ' ')}</dt>
      <dd>{v}</dd>
    </>
  )
}
