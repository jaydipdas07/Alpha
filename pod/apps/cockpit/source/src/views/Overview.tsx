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

type Snap = { t: number; v: number }

function buildEquity(records: Record<string, unknown>[]): EquityPoint[] {
  // pnl_snapshots is keyed per deployment; the Overview curve is *total account
  // equity*: forward-fill each deployment's latest equity and sum across
  // deployments at every snapshot timestamp. Output is ascending + unique by time
  // (lightweight-charts requires that). Degrades to a single deployment's own curve.
  const byDeployment = new Map<string, Snap[]>()
  for (const r of records) {
    const t = toUnixSeconds(r.ts)
    if (t <= 0) continue
    const dep = typeof r.deployment_id === 'string' ? r.deployment_id : '_'
    const snap: Snap = { t, v: parseNum(r.equity) }
    const existing = byDeployment.get(dep)
    if (existing) existing.push(snap)
    else byDeployment.set(dep, [snap])
  }

  const deployments = [...byDeployment.values()].map((s) => s.sort((a, b) => a.t - b.t))
  const times = [...new Set(deployments.flat().map((s) => s.t))].sort((a, b) => a - b)
  const cursor = deployments.map(() => -1) // index of each deployment's last snapshot ≤ t
  const last = deployments.map(() => 0) // its equity there

  return times.map((t) => {
    let sum = 0
    deployments.forEach((snaps, i) => {
      while (cursor[i] + 1 < snaps.length && snaps[cursor[i] + 1].t <= t) {
        cursor[i] += 1
        last[i] = snaps[cursor[i]].v
      }
      if (cursor[i] >= 0) sum += last[i] // only count deployments that have started
    })
    return { time: t as UTCTimestamp, value: sum }
  })
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

  // Most-recent window, newest first (buildEquity re-sorts ascending for display).
  const pnl = useLiveRecords({
    client: lemmaClient,
    tableName: 'pnl_snapshots',
    limit: 1000,
    sort: [{ field: 'ts', direction: 'desc' }],
  })
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
