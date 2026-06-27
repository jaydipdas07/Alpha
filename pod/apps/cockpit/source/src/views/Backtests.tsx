import { useMemo, useState } from 'react'
import { FlaskConical } from 'lucide-react'
import { useLiveRecords } from 'lemma-sdk/react'
import { lemmaClient } from '../lemma-client'
import { fmtMoney, fmtNum, fmtPct, errMessage } from '../lib'
import { Panel, EmptyState, StatusBadge, type StatusKind } from '../ui'
import { DataTable, type Column } from '../components/DataTable'

// M2.3 — every backtest candidate INCL. rejected. The rigor filter is visibly
// working: rejected strategies show their failing PBO / DSR right next to the
// promoted ones. The verdict is the strategy's lifecycle status (the cockpit
// shows metrics + outcome; it does not re-run the gate). Read-only (TEST-8).

const VERDICT_KIND: Record<string, StatusKind> = {
  approved: 'ok',
  paper: 'ok',
  live: 'ok',
  candidate: 'info',
  backtested: 'info',
  rejected: 'danger',
  retired: 'neutral',
}

const MARKETS = ['all', 'crypto', 'equity', 'index_option'] as const

interface Row {
  id: string
  strategy: string
  family: string
  verdict: string
  market: string
  window: string
  sharpe: number | null
  dsr: number | null
  pbo: number | null
  maxDd: number | null
  netPnl: number | null
  trials: number
  status: string
}

/** Coerce a FLOAT (number) or string-Decimal (TEXT) field to a number; else null. */
function num(v: unknown): number | null {
  if (v === null || v === undefined || v === '') return null
  const n = typeof v === 'number' ? v : Number(v)
  return Number.isFinite(n) ? n : null
}

const dash = (s: string) => (s ? <span className="mono muted">{s}</span> : <span className="mono muted">—</span>)

const columns: Column<Row>[] = [
  {
    key: 'strategy',
    header: 'Strategy',
    sort: (r) => r.strategy.toLowerCase(),
    render: (r) => (
      <div className="cell-main">
        <span>{r.strategy}</span>
        <span className="cell-sub mono">{r.family || '—'}</span>
      </div>
    ),
  },
  { key: 'market', header: 'Market', sort: (r) => r.market, render: (r) => dash(r.market) },
  { key: 'window', header: 'Window', render: (r) => dash(r.window) },
  { key: 'sharpe', header: 'Sharpe', align: 'right', sort: (r) => r.sharpe ?? -Infinity, render: (r) => <span className="mono">{fmtNum(r.sharpe)}</span> },
  { key: 'dsr', header: 'DSR', align: 'right', sort: (r) => r.dsr ?? -Infinity, render: (r) => <span className="mono">{fmtNum(r.dsr)}</span> },
  { key: 'pbo', header: 'PBO', align: 'right', sort: (r) => r.pbo ?? Infinity, render: (r) => <span className="mono">{fmtNum(r.pbo)}</span> },
  { key: 'maxDd', header: 'Max DD', align: 'right', sort: (r) => r.maxDd ?? Infinity, render: (r) => <span className="mono">{fmtPct(r.maxDd)}</span> },
  {
    key: 'netPnl',
    header: 'Net P&L',
    align: 'right',
    sort: (r) => r.netPnl ?? -Infinity,
    render: (r) =>
      r.netPnl === null ? (
        <span className="mono muted">—</span>
      ) : (
        <span className="mono" style={{ color: r.netPnl >= 0 ? 'var(--ok)' : 'var(--danger)' }}>
          {fmtMoney(r.netPnl)}
        </span>
      ),
  },
  { key: 'trials', header: 'Trials', align: 'right', sort: (r) => r.trials, render: (r) => <span className="mono muted">{r.trials}</span> },
  {
    key: 'status',
    header: 'Run',
    sort: (r) => r.status,
    render: (r) => (
      <StatusBadge kind={r.status === 'failed' ? 'danger' : r.status === 'complete' ? 'neutral' : 'info'}>
        {r.status || '—'}
      </StatusBadge>
    ),
  },
  {
    key: 'verdict',
    header: 'Verdict',
    sort: (r) => r.verdict,
    render: (r) => <StatusBadge kind={VERDICT_KIND[r.verdict] ?? 'neutral'}>{r.verdict}</StatusBadge>,
  },
]

export function Backtests() {
  const [market, setMarket] = useState<string>('all')
  const bt = useLiveRecords({
    client: lemmaClient,
    tableName: 'backtests',
    limit: 500,
    sort: [{ field: 'created_at', direction: 'desc' }],
  })
  const strat = useLiveRecords({
    client: lemmaClient,
    tableName: 'strategies',
    limit: 500,
    sort: [{ field: 'created_at', direction: 'desc' }],
  })

  const stratById = useMemo(() => {
    const m = new Map<string, Record<string, unknown>>()
    for (const s of strat.records) m.set(String(s.id), s)
    return m
  }, [strat.records])

  const rows = useMemo<Row[]>(() => {
    return bt.records
      .map((b): Row => {
        const s = stratById.get(String(b.strategy_id))
        return {
          id: String(b.id),
          strategy: (typeof s?.name === 'string' && s.name) || '—',
          family: String(b.family ?? ''),
          verdict: (typeof s?.status === 'string' && s.status) || 'unknown',
          market: String(b.market ?? ''),
          window: String(b.window ?? ''),
          sharpe: num(b.sharpe),
          dsr: num(b.deflated_sharpe),
          pbo: num(b.cpcv_pbo),
          maxDd: num(b.max_dd),
          netPnl: num(b.net_pnl),
          trials: typeof b.trial_count === 'number' ? b.trial_count : 0,
          status: String(b.status ?? ''),
        }
      })
      .filter((r) => market === 'all' || r.market === market)
  }, [bt.records, stratById, market])

  const error = bt.error || strat.error

  return (
    <Panel
      title="Candidates"
      icon={FlaskConical}
      action={
        <div className="chips-filter" role="group" aria-label="Filter by market">
          {MARKETS.map((m) => (
            <button
              key={m}
              type="button"
              className={`chip-btn${market === m ? ' active' : ''}`}
              aria-pressed={market === m}
              onClick={() => setMarket(m)}
            >
              {m}
            </button>
          ))}
        </div>
      }
    >
      {error ? (
        <div className="alert">Could not load backtests: {errMessage(error)}</div>
      ) : bt.isLoading ? (
        <div className="skeleton" style={{ height: 200 }} />
      ) : (
        <DataTable
          columns={columns}
          rows={rows}
          rowKey={(r) => r.id}
          initialSortKey="dsr"
          initialDir="desc"
          empty={
            <EmptyState
              icon={FlaskConical}
              head="No backtests in this view"
              sub="Backtest candidates — promoted and rejected — appear here with Sharpe / DSR / PBO / max-DD as the discovery loop runs them."
              hint="reads: backtests ⋈ strategies"
            />
          }
        />
      )}
    </Panel>
  )
}
