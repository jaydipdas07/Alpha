import { useMemo } from 'react'
import { Network, Plug } from 'lucide-react'
import { useLiveRecords } from 'lemma-sdk/react'
import { lemmaClient } from '../lemma-client'
import { CONFIG, isObject } from '../config-snapshot'
import { Panel, EmptyState, StatusBadge } from '../ui'
import { DataTable, type Column } from '../components/DataTable'

// M2.4 — venues + tradable universe. The venue REGISTRY (config/venues.yaml) is a
// skeleton today, so "active" venues are read from the pod (deployments + worker
// adapters); the instrument universe comes from the config snapshot. Read-only.

interface InstRow {
  id: string
  symbol: string
  segment: string
  assetClass: string
  lot: string
  tick: string
  ccy: string
}

const columns: Column<InstRow>[] = [
  { key: 'symbol', header: 'Symbol', sort: (r) => r.symbol, render: (r) => <span className="mono">{r.symbol}</span> },
  { key: 'segment', header: 'Segment', sort: (r) => r.segment, render: (r) => <span className="mono muted">{r.segment || '—'}</span> },
  { key: 'assetClass', header: 'Asset class', sort: (r) => r.assetClass, render: (r) => <span className="mono muted">{r.assetClass || '—'}</span> },
  { key: 'lot', header: 'Lot', align: 'right', sort: (r) => Number(r.lot) || 0, render: (r) => <span className="mono">{r.lot || '—'}</span> },
  { key: 'tick', header: 'Tick', align: 'right', sort: (r) => Number(r.tick) || 0, render: (r) => <span className="mono">{r.tick || '—'}</span> },
  { key: 'ccy', header: 'Ccy', sort: (r) => r.ccy, render: (r) => <span className="mono muted">{r.ccy || '—'}</span> },
]

export function Exchanges() {
  const deps = useLiveRecords({ client: lemmaClient, tableName: 'deployments', limit: 500 })
  const workers = useLiveRecords({ client: lemmaClient, tableName: 'worker_status', limit: 50 })

  const venues = useMemo(() => {
    const set = new Set<string>()
    for (const d of deps.records) {
      if (typeof d.venue === 'string' && d.venue) set.add(d.venue.toUpperCase())
    }
    for (const w of workers.records) {
      const detail = isObject(w.detail) ? w.detail : null
      const adapters = detail?.adapters
      if (Array.isArray(adapters)) {
        for (const a of adapters) if (typeof a === 'string' && a) set.add(a.toUpperCase())
      }
    }
    return [...set].sort()
  }, [deps.records, workers.records])

  const instruments = useMemo<InstRow[]>(() => {
    const inst = isObject(CONFIG.files.instruments) ? CONFIG.files.instruments : null
    const map = inst && isObject(inst.instruments) ? inst.instruments : null
    if (!map) return []
    return Object.entries(map).map(([symbol, meta]): InstRow => {
      const m = isObject(meta) ? meta : {}
      return {
        id: symbol,
        symbol,
        segment: String(m.segment ?? ''),
        assetClass: String(m.asset_class ?? ''),
        lot: m.lot_size != null ? String(m.lot_size) : '',
        tick: m.tick_size != null ? String(m.tick_size) : '',
        ccy: String(m.quote_currency ?? ''),
      }
    })
  }, [])

  return (
    <div className="stack">
      <Panel title="Active venues" icon={Plug}>
        {venues.length ? (
          <div className="venue-badges">
            {venues.map((v) => (
              <StatusBadge key={v} kind="info">
                {v}
              </StatusBadge>
            ))}
          </div>
        ) : (
          <p className="muted">No venues in use yet — they appear as deployments + worker adapters report them.</p>
        )}
        <p className="snapshot-note" style={{ marginTop: 12 }}>
          The full venue registry lives in <code>config/venues.yaml</code> (a skeleton today; populated as
          adapters land). Active venues here are read live from the pod.
        </p>
      </Panel>

      <Panel title="Instrument universe" icon={Network}>
        {instruments.length ? (
          <DataTable columns={columns} rows={instruments} rowKey={(r) => r.id} initialSortKey="symbol" />
        ) : (
          <EmptyState
            icon={Network}
            head="No instruments configured"
            sub="The tradable universe + lot/tick sizes come from config/instruments.yaml, mirrored into the cockpit snapshot."
            hint="source: config/instruments.yaml"
          />
        )}
      </Panel>
    </div>
  )
}
