import { useMemo, useState } from 'react'
import { Boxes } from 'lucide-react'
import { useLiveRecords } from 'lemma-sdk/react'
import { lemmaClient } from '../lemma-client'
import { errMessage } from '../lib'
import { Panel, EmptyState, StatusBadge, type StatusKind } from '../ui'
import { DataTable, type Column } from '../components/DataTable'

// M2.4 — the strategy book: discovered + deployed strategies and their lifecycle
// state, with a count of live/paper deployments. Read-only (TEST-8).

const STATUS_KIND: Record<string, StatusKind> = {
  candidate: 'info',
  backtested: 'info',
  paper: 'ok',
  approved: 'ok',
  live: 'ok',
  rejected: 'danger',
  retired: 'neutral',
}

const STATUSES = ['all', 'candidate', 'backtested', 'paper', 'approved', 'live', 'rejected', 'retired'] as const

interface Row {
  id: string
  name: string
  family: string
  market: string
  status: string
  origin: string
  rationale: string
  deployments: number
}

const columns: Column<Row>[] = [
  {
    key: 'name',
    header: 'Strategy',
    sort: (r) => r.name.toLowerCase(),
    render: (r) => (
      <div className="cell-main">
        <span>{r.name}</span>
        <span className="cell-sub mono">{r.family || '—'}</span>
      </div>
    ),
  },
  { key: 'market', header: 'Market', sort: (r) => r.market, render: (r) => <span className="mono muted">{r.market || '—'}</span> },
  {
    key: 'status',
    header: 'Status',
    sort: (r) => r.status,
    render: (r) => <StatusBadge kind={STATUS_KIND[r.status] ?? 'neutral'}>{r.status || '—'}</StatusBadge>,
  },
  { key: 'origin', header: 'Origin', sort: (r) => r.origin, render: (r) => <span className="mono muted">{r.origin || '—'}</span> },
  { key: 'deployments', header: 'Deploys', align: 'right', sort: (r) => r.deployments, render: (r) => <span className="mono muted">{r.deployments}</span> },
  {
    key: 'rationale',
    header: 'Rationale',
    render: (r) => (
      <span className="cell-clip" title={r.rationale}>
        {r.rationale || '—'}
      </span>
    ),
  },
]

export function Strategies() {
  const [status, setStatus] = useState<string>('all')
  const strat = useLiveRecords({
    client: lemmaClient,
    tableName: 'strategies',
    limit: 500,
    sort: [{ field: 'created_at', direction: 'desc' }],
  })
  const deps = useLiveRecords({ client: lemmaClient, tableName: 'deployments', limit: 500 })

  const depCount = useMemo(() => {
    const m = new Map<string, number>()
    for (const d of deps.records) {
      const sid = String(d.strategy_id ?? '')
      if (sid) m.set(sid, (m.get(sid) ?? 0) + 1)
    }
    return m
  }, [deps.records])

  const rows = useMemo<Row[]>(() => {
    return strat.records
      .map((s): Row => ({
        id: String(s.id),
        name: (typeof s.name === 'string' && s.name) || '—',
        family: String(s.family ?? ''),
        market: String(s.market ?? ''),
        status: String(s.status ?? ''),
        origin: String(s.origin ?? ''),
        rationale: typeof s.rationale === 'string' ? s.rationale : '',
        deployments: depCount.get(String(s.id)) ?? 0,
      }))
      .filter((r) => status === 'all' || r.status === status)
  }, [strat.records, depCount, status])

  return (
    <Panel
      title="Strategies"
      icon={Boxes}
      action={
        <div className="chips-filter" role="group" aria-label="Filter by status">
          {STATUSES.map((s) => (
            <button
              key={s}
              type="button"
              className={`chip-btn${status === s ? ' active' : ''}`}
              aria-pressed={status === s}
              onClick={() => setStatus(s)}
            >
              {s}
            </button>
          ))}
        </div>
      }
    >
      {strat.error ? (
        <div className="alert">Could not load strategies: {errMessage(strat.error)}</div>
      ) : strat.isLoading ? (
        <div className="skeleton" style={{ height: 200 }} />
      ) : (
        <DataTable
          columns={columns}
          rows={rows}
          rowKey={(r) => r.id}
          initialSortKey="name"
          empty={
            <EmptyState
              icon={Boxes}
              head="No strategies in this view"
              sub="Strategies the discovery loop proposes (and you approve) appear here with their lifecycle status, from candidate to live."
              hint="reads: strategies ⋈ deployments"
            />
          }
        />
      )}
    </Panel>
  )
}
