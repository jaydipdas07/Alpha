import { useMemo, useState } from 'react'
import { Telescope } from 'lucide-react'
import { useLiveRecords } from 'lemma-sdk/react'
import { lemmaClient } from '../lemma-client'
import { errMessage, timeAgo } from '../lib'
import { Panel, EmptyState, StatusBadge, type StatusKind } from '../ui'
import { DataTable, type Column } from '../components/DataTable'

// M2.4 — nightly discovery cycles (Workflow A): proposals, survivors, trial
// counts, and run status from discovery_runs. Read-only (TEST-8).

const STATUS_KIND: Record<string, StatusKind> = {
  running: 'info',
  complete: 'ok',
  failed: 'danger',
}

const STATUSES = ['all', 'running', 'complete', 'failed'] as const

interface Row {
  id: string
  market: string
  family: string
  window: string
  status: string
  trials: number
  survivors: number
  createdAt: string
  note: string
}

function noteOf(detail: unknown): string {
  if (!detail || typeof detail !== 'object') return ''
  const d = detail as Record<string, unknown>
  if (typeof d.error === 'string') return `error: ${d.error}`
  if (typeof d.note === 'string') return d.note
  if (Array.isArray(d.promoted) && d.promoted.length) return `promoted: ${d.promoted.join(', ')}`
  return ''
}

const columns: Column<Row>[] = [
  {
    key: 'cell',
    header: 'Cell',
    sort: (r) => `${r.market}/${r.family}`,
    render: (r) => (
      <div className="cell-main">
        <span className="mono">
          {r.market || '—'} · {r.family || '—'}
        </span>
        <span className="cell-sub mono">{r.window || '—'}</span>
      </div>
    ),
  },
  {
    key: 'status',
    header: 'Status',
    sort: (r) => r.status,
    render: (r) => <StatusBadge kind={STATUS_KIND[r.status] ?? 'neutral'}>{r.status || '—'}</StatusBadge>,
  },
  { key: 'trials', header: 'Trials', align: 'right', sort: (r) => r.trials, render: (r) => <span className="mono muted">{r.trials}</span> },
  {
    key: 'survivors',
    header: 'Survivors',
    align: 'right',
    sort: (r) => r.survivors,
    render: (r) => (
      <span className="mono" style={{ color: r.survivors > 0 ? 'var(--ok)' : 'var(--muted)' }}>
        {r.survivors}
      </span>
    ),
  },
  { key: 'when', header: 'When', sort: (r) => r.createdAt, render: (r) => <span className="mono muted">{timeAgo(r.createdAt)}</span> },
  {
    key: 'note',
    header: 'Note',
    render: (r) => (
      <span className="cell-clip" title={r.note}>
        {r.note || '—'}
      </span>
    ),
  },
]

export function Discovery() {
  const [status, setStatus] = useState<string>('all')
  const runs = useLiveRecords({
    client: lemmaClient,
    tableName: 'discovery_runs',
    limit: 500,
    sort: [{ field: 'created_at', direction: 'desc' }],
  })

  const rows = useMemo<Row[]>(() => {
    return runs.records
      .map((r): Row => ({
        id: String(r.id),
        market: String(r.market ?? ''),
        family: String(r.family ?? ''),
        window: String(r.window ?? ''),
        status: String(r.status ?? ''),
        trials: typeof r.trial_count === 'number' ? r.trial_count : 0,
        survivors: typeof r.survivors === 'number' ? r.survivors : 0,
        createdAt: typeof r.created_at === 'string' ? r.created_at : '',
        note: noteOf(r.detail),
      }))
      .filter((r) => status === 'all' || r.status === status)
  }, [runs.records, status])

  return (
    <Panel
      title="Discovery runs"
      icon={Telescope}
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
      {runs.error ? (
        <div className="alert">Could not load discovery runs: {errMessage(runs.error)}</div>
      ) : runs.isLoading ? (
        <div className="skeleton" style={{ height: 200 }} />
      ) : (
        <DataTable
          columns={columns}
          rows={rows}
          rowKey={(r) => r.id}
          initialSortKey="when"
          initialDir="desc"
          empty={
            <EmptyState
              icon={Telescope}
              head="No discovery runs in this view"
              sub="Each nightly discovery cycle lands here — the (market, family, window) cell it swept, how many trials it ran, and how many survived the rigor gate."
              hint="reads: discovery_runs"
            />
          }
        />
      )}
    </Panel>
  )
}
