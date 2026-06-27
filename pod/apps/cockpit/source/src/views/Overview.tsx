import { useMemo } from 'react'
import { LineChart } from 'lucide-react'
import { useLiveRecords } from 'lemma-sdk/react'
import type { UTCTimestamp } from 'lightweight-charts'
import { lemmaClient } from '../lemma-client'
import { useCount } from '../hooks'
import { parseNum, toUnixSeconds, fmtMoney, fmtInt, errMessage } from '../lib'
import { Panel, Metric, EmptyState, StatusBadge, type StatusKind } from '../ui'
import { EquityChart, type EquityPoint } from '../components/EquityChart'

// Overview — the live default screen. M2.1 completed here: the equity curve
// streams from `pnl_snapshots` via watchChanges (`useLiveRecords`, merged in
// place, never polled), and the pipeline tiles count the book. The system-health
// strip (heartbeats / reconcile / freshness / open risk_events) lands above this
// in M2.2.

function buildEquity(records: Record<string, unknown>[]): EquityPoint[] {
  // Dedupe by timestamp (last write wins) and sort ascending — lightweight-charts
  // requires a strictly increasing, unique time axis.
  const byTime = new Map<number, number>()
  for (const r of records) {
    const t = toUnixSeconds(r.ts)
    if (t > 0) byTime.set(t, parseNum(r.equity))
  }
  return [...byTime.entries()]
    .sort((a, b) => a[0] - b[0])
    .map(([time, value]) => ({ time: time as UTCTimestamp, value }))
}

const LIVE_BADGE: Record<string, StatusKind> = {
  open: 'ok',
  connecting: 'info',
  reconnecting: 'warn',
  closed: 'neutral',
}

export function Overview() {
  const strategies = useCount('strategies')
  const backtests = useCount('backtests')
  const discovery = useCount('discovery_runs')
  const riskEvents = useCount('risk_events')

  const pnl = useLiveRecords({ client: lemmaClient, tableName: 'pnl_snapshots', limit: 1000 })
  const points = useMemo(() => buildEquity(pnl.records), [pnl.records])
  const latestEquity = points.length ? points[points.length - 1].value : null

  return (
    <div className="stack">
      <div className="grid cols-4">
        <Metric label="Strategies" value={fmtInt(strategies)} />
        <Metric label="Backtests" value={fmtInt(backtests)} />
        <Metric label="Discovery runs" value={fmtInt(discovery)} />
        <Metric label="Risk events" value={fmtInt(riskEvents)} />
      </div>

      <Panel
        title="Equity / P&L"
        icon={LineChart}
        action={
          <span className="row">
            {latestEquity !== null ? <span className="mono">{fmtMoney(latestEquity)}</span> : null}
            <StatusBadge kind={LIVE_BADGE[pnl.liveStatus] ?? 'neutral'}>{pnl.liveStatus}</StatusBadge>
          </span>
        }
      >
        {pnl.error ? (
          <div className="alert">Could not load P&amp;L: {errMessage(pnl.error)}</div>
        ) : pnl.isLoading ? (
          <div className="skeleton" style={{ height: 280 }} />
        ) : points.length ? (
          <EquityChart points={points} />
        ) : (
          <EmptyState
            icon={LineChart}
            head="No P&L snapshots yet"
            sub="The equity curve appears once paper or live runs write pnl_snapshots (Phase 3). It streams in live here via watchChanges — no refresh."
            hint="reads: pnl_snapshots"
          />
        )}
      </Panel>
    </div>
  )
}
