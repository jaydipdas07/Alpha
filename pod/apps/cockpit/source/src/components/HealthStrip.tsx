import type { ReactNode } from 'react'
import { Cpu, Microscope, RefreshCw, ShieldAlert, type LucideIcon } from 'lucide-react'
import { useLiveRecords } from 'lemma-sdk/react'
import { lemmaClient } from '../lemma-client'
import { useNow } from '../hooks'
import { secondsAgo, timeAgo } from '../lib'
import { StatusBadge, type StatusKind } from '../ui'

// M2.2 — the system-health strip. Worker + research heartbeats (fresh/stale),
// last reconcile + data freshness, and open risk_events — streamed live via
// watchChanges. A dead heartbeat ages into "stale" because useNow ticks a local
// clock (no API polling). All read-only (TEST-8).

// DISPLAY heuristics only — the authoritative liveness lease + halt is enforced in
// alpha-core (R8), not here. A heartbeat beats every few seconds; reconcile runs
// periodically, so it gets a looser threshold.
const STALE_AFTER_SECONDS = 90
const RECONCILE_STALE_AFTER_SECONDS = 900

const SEV_RANK: Record<string, number> = { info: 1, warning: 2, critical: 3 }
const SEV_LABEL: Record<number, string> = { 1: 'info', 2: 'warning', 3: 'critical' }
const SEV_KIND: Record<number, StatusKind> = { 1: 'info', 2: 'warn', 3: 'danger' }
const RES_KIND: Record<string, StatusKind> = {
  idle: 'neutral',
  running: 'info',
  stale: 'warn',
  error: 'danger',
}

function text(v: unknown): string {
  return typeof v === 'string' ? v : ''
}

function detailOf(r: Record<string, unknown> | null | undefined): Record<string, unknown> | null {
  return r && typeof r.detail === 'object' && r.detail ? (r.detail as Record<string, unknown>) : null
}

function freshest(
  records: Record<string, unknown>[],
  tsField: string,
): Record<string, unknown> | null {
  let best: Record<string, unknown> | null = null
  let bestT = -Infinity
  for (const r of records) {
    const t = Date.parse(text(r[tsField]))
    if (!Number.isNaN(t) && t > bestT) {
      bestT = t
      best = r
    }
  }
  return best
}

function HealthTile({
  icon: Icon,
  label,
  dot,
  children,
}: {
  icon: LucideIcon
  label: string
  dot: StatusKind
  children: ReactNode
}) {
  return (
    <div className="health-tile">
      <div className="ht-head">
        <span className="ht-label">
          <Icon size={14} /> {label}
        </span>
        <span className={`dot ${dot}`} />
      </div>
      <div className="ht-body">{children}</div>
    </div>
  )
}

export function HealthStrip({ dataAsOf }: { dataAsOf: number | null }) {
  const now = useNow()
  const workers = useLiveRecords({ client: lemmaClient, tableName: 'worker_status', limit: 50 })
  const research = useLiveRecords({ client: lemmaClient, tableName: 'research_status', limit: 50 })
  const risk = useLiveRecords({
    client: lemmaClient,
    tableName: 'risk_events',
    limit: 200,
    sort: [{ field: 'ts', direction: 'desc' }],
  })

  // --- worker (a stale worker → danger: it is the money path) ---
  const worker = freshest(workers.records, 'last_seen')
  const wAge = worker ? secondsAgo(worker.last_seen, now) : null
  const wFresh = wAge !== null && wAge <= STALE_AFTER_SECONDS
  const wMode = text(worker?.mode) || 'paper'
  const wArmed = Boolean(worker?.armed)
  const reconciledAt = detailOf(worker)?.reconciled_at
  const wDot: StatusKind = workers.error
    ? 'danger'
    : worker
      ? wFresh
        ? 'ok'
        : 'danger'
      : 'neutral'

  // --- research (a stale research box → warn: off the money path) ---
  const res = freshest(research.records, 'last_seen')
  const rAge = res ? secondsAgo(res.last_seen, now) : null
  const rStatus = text(res?.status) || 'unknown'
  const rTask = text(res?.current_task)
  const rDot: StatusKind = research.error
    ? 'danger'
    : res
      ? rAge !== null && rAge > STALE_AFTER_SECONDS
        ? 'warn'
        : (RES_KIND[rStatus] ?? 'neutral')
      : 'neutral'

  // --- reconcile + data freshness ---
  const recAge = secondsAgo(reconciledAt, now)
  const recDot: StatusKind =
    recAge === null ? 'neutral' : recAge <= RECONCILE_STALE_AFTER_SECONDS ? 'ok' : 'warn'

  // --- risk: "open" = unresolved (risk_events is append-only; resolution is
  // carried in detail.resolved), most-recent first ---
  const openEvents = risk.records.filter((e) => detailOf(e)?.resolved !== true)
  const worst = openEvents.reduce((acc, e) => Math.max(acc, SEV_RANK[text(e.severity)] ?? 0), 0)
  const riskDot: StatusKind = risk.error
    ? 'danger'
    : worst > 0
      ? SEV_KIND[worst]
      : risk.isLoading
        ? 'neutral'
        : openEvents.length
          ? 'info'
          : 'ok'

  return (
    <div className="grid cols-4">
      <HealthTile icon={Cpu} label="Worker" dot={wDot}>
        {workers.error ? (
          <span className="ht-sub" style={{ color: 'var(--danger)' }}>
            health unavailable
          </span>
        ) : workers.isLoading && !worker ? (
          <span className="ht-sub muted">loading…</span>
        ) : worker ? (
          <>
            <div className="row">
              <StatusBadge kind={wMode === 'live' ? 'danger' : 'info'}>{wMode}</StatusBadge>
              <StatusBadge kind={wArmed ? 'warn' : 'neutral'}>
                {wArmed ? 'armed' : 'disarmed'}
              </StatusBadge>
            </div>
            <span className="ht-sub mono">beat {timeAgo(worker.last_seen, now)}</span>
          </>
        ) : (
          <span className="ht-sub muted">no worker reporting</span>
        )}
      </HealthTile>

      <HealthTile icon={Microscope} label="Research" dot={rDot}>
        {research.error ? (
          <span className="ht-sub" style={{ color: 'var(--danger)' }}>
            health unavailable
          </span>
        ) : research.isLoading && !res ? (
          <span className="ht-sub muted">loading…</span>
        ) : res ? (
          <>
            <StatusBadge kind={RES_KIND[rStatus] ?? 'neutral'}>{rStatus}</StatusBadge>
            <span className="ht-sub mono">
              beat {timeAgo(res.last_seen, now)}
              {rTask ? ` · ${rTask}` : ''}
            </span>
          </>
        ) : (
          <span className="ht-sub muted">no research box</span>
        )}
      </HealthTile>

      <HealthTile icon={RefreshCw} label="Reconcile" dot={recDot}>
        <span className="ht-value mono">{reconciledAt ? timeAgo(reconciledAt, now) : '—'}</span>
        <span className="ht-sub muted">
          data {dataAsOf ? timeAgo(new Date(dataAsOf * 1000).toISOString(), now) : '—'}
        </span>
      </HealthTile>

      <HealthTile icon={ShieldAlert} label="Risk" dot={riskDot}>
        <span className="ht-value mono">
          {risk.error ? '—' : risk.isLoading ? '…' : openEvents.length} open
        </span>
        {worst > 0 ? (
          <StatusBadge kind={SEV_KIND[worst]}>{SEV_LABEL[worst]}</StatusBadge>
        ) : (
          <span className="ht-sub muted">{risk.error ? 'unavailable' : 'all clear'}</span>
        )}
      </HealthTile>
    </div>
  )
}
