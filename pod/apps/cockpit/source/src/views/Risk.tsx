import { useMemo, useState } from 'react'
import { ShieldAlert, Power, Ban, RotateCcw, Check } from 'lucide-react'
import { useLiveRecords, useCreateRecord, useCurrentUser } from 'lemma-sdk/react'
import { lemmaClient } from '../lemma-client'
import { useNow } from '../hooks'
import { errMessage, freshest, timeAgo } from '../lib'
import { Panel, EmptyState, StatusBadge, type StatusKind } from '../ui'
import { DataTable, type Column } from '../components/DataTable'

// M2.4 — the Risk surface: kill-switch state, the risk_events audit log, and the
// governance controls. The controls ISSUE a command (arm_kill / flatten /
// clear_halt) — the pod commands, the worker EXECUTES (TEST-8). The pod never
// trades. Controls confirm inline (no blocking JS dialog) and require a known
// issuer before writing.

const SEV_KIND: Record<string, StatusKind> = { info: 'info', warning: 'warn', critical: 'danger' }
const CMD_KIND: Record<string, StatusKind> = { pending: 'warn', acked: 'info', done: 'ok', failed: 'danger' }

function text(v: unknown): string {
  return typeof v === 'string' ? v : ''
}

function detailOf(r: Record<string, unknown>): Record<string, unknown> | null {
  return typeof r.detail === 'object' && r.detail ? (r.detail as Record<string, unknown>) : null
}

function ConfirmButton({
  label,
  icon: Icon,
  tone,
  disabled,
  onConfirm,
}: {
  label: string
  icon: typeof Power
  tone: 'warn' | 'danger' | 'ok'
  disabled?: boolean
  onConfirm: () => void
}) {
  const [confirming, setConfirming] = useState(false)
  if (confirming) {
    return (
      <span className="confirm-row">
        <span className="muted">{label}?</span>
        <button
          type="button"
          className={`btn ${tone}`}
          disabled={disabled}
          onClick={() => {
            setConfirming(false)
            onConfirm()
          }}
        >
          confirm
        </button>
        <button type="button" className="btn ghost" onClick={() => setConfirming(false)}>
          cancel
        </button>
      </span>
    )
  }
  return (
    <button type="button" className={`btn ${tone}`} disabled={disabled} onClick={() => setConfirming(true)}>
      <Icon size={15} />
      {label}
    </button>
  )
}

// --- risk events table ---
interface EventRow {
  id: string
  kind: string
  severity: string
  when: string
  resolved: boolean
  note: string
}

const eventColumns: Column<EventRow>[] = [
  { key: 'kind', header: 'Event', sort: (r) => r.kind, render: (r) => <span className="mono">{r.kind}</span> },
  {
    key: 'severity',
    header: 'Severity',
    sort: (r) => r.severity,
    render: (r) => <StatusBadge kind={SEV_KIND[r.severity] ?? 'neutral'}>{r.severity || '—'}</StatusBadge>,
  },
  { key: 'when', header: 'When', sort: (r) => r.when, render: (r) => <span className="mono muted">{timeAgo(r.when)}</span> },
  {
    key: 'resolved',
    header: 'State',
    sort: (r) => String(r.resolved),
    render: (r) =>
      r.resolved ? (
        <StatusBadge kind="neutral">resolved</StatusBadge>
      ) : (
        <StatusBadge kind="warn">open</StatusBadge>
      ),
  },
  {
    key: 'note',
    header: 'Detail',
    render: (r) => (
      <span className="cell-clip" title={r.note}>
        {r.note || '—'}
      </span>
    ),
  },
]

export function Risk() {
  const now = useNow()
  const { user } = useCurrentUser({ client: lemmaClient })
  const workers = useLiveRecords({ client: lemmaClient, tableName: 'worker_status', limit: 50 })
  const events = useLiveRecords({
    client: lemmaClient,
    tableName: 'risk_events',
    limit: 200,
    sort: [{ field: 'ts', direction: 'desc' }],
  })
  const commands = useLiveRecords({
    client: lemmaClient,
    tableName: 'commands',
    limit: 200,
    sort: [{ field: 'created_at', direction: 'desc' }],
  })
  const [issueError, setIssueError] = useState<string | null>(null)
  const [issued, setIssued] = useState<string | null>(null)
  const { create, isSubmitting } = useCreateRecord({ client: lemmaClient, tableName: 'commands' })

  // Single-worker design today: the freshest heartbeat is "the" worker; the
  // controls target its worker_id. Revisit when multiple workers heartbeat.
  const worker = freshest(workers.records, 'last_seen')
  const armed = Boolean(worker?.armed)
  const workerId = text(worker?.worker_id)
  const mode = text(worker?.mode) || 'paper'
  // A governance command must carry an issuer (audit) and a target worker.
  const canIssue = Boolean(workerId) && Boolean(user?.id) && !isSubmitting

  async function issue(kind: string, reason: string) {
    if (!workerId || !user?.id) return
    setIssued(null)
    try {
      await create({
        kind,
        status: 'pending',
        worker_id: workerId,
        issued_by: user.id,
        payload: { reason, source: 'cockpit' },
      })
      setIssueError(null)
      setIssued(kind)
    } catch (e) {
      setIssueError(errMessage(e))
    }
  }

  const eventRows = useMemo<EventRow[]>(
    () =>
      events.records.map((e): EventRow => {
        const d = detailOf(e)
        return {
          id: String(e.id),
          kind: text(e.kind),
          severity: text(e.severity),
          when: text(e.ts),
          resolved: d?.resolved === true,
          note: d ? text(d.note) || text(d.error) || JSON.stringify(d) : '',
        }
      }),
    [events.records],
  )

  const pending = useMemo(
    () => commands.records.filter((c) => text(c.status) === 'pending'),
    [commands.records],
  )

  return (
    <div className="stack">
      <Panel
        title="Kill switch"
        icon={ShieldAlert}
        action={
          worker ? (
            <span className="row">
              <StatusBadge kind={mode === 'live' ? 'danger' : 'info'}>{mode}</StatusBadge>
              <StatusBadge kind={armed ? 'warn' : 'neutral'}>{armed ? 'armed' : 'disarmed'}</StatusBadge>
            </span>
          ) : null
        }
      >
        {worker ? (
          <div className="stack">
            <div className="kv">
              <dt>Worker</dt>
              <dd>{workerId || '—'}</dd>
              <dt>Heartbeat</dt>
              <dd>{timeAgo(worker.last_seen, now)}</dd>
            </div>
            <div className="control-row">
              <ConfirmButton label="Arm kill-switch" icon={Power} tone="warn" disabled={!canIssue} onConfirm={() => void issue('arm_kill', 'manual arm from cockpit')} />
              <ConfirmButton label="Flatten all" icon={Ban} tone="danger" disabled={!canIssue} onConfirm={() => void issue('flatten', 'manual flatten from cockpit')} />
              <ConfirmButton label="Clear halt" icon={RotateCcw} tone="ok" disabled={!canIssue} onConfirm={() => void issue('clear_halt', 'manual clear-halt from cockpit')} />
            </div>
            {issued ? (
              <p className="issued-note">
                <Check size={14} /> <span className="mono">{issued}</span> command issued — pending worker
                execution.
              </p>
            ) : null}
            {issueError ? <div className="alert">Could not issue command: {issueError}</div> : null}
            {!user?.id ? <p className="muted">Sign-in still resolving — controls enable once your identity loads.</p> : null}
            <p className="snapshot-note">
              Controls <strong>issue a command</strong> — the worker executes it; the pod never places an
              order itself (TEST-8). A tripped kill-switch latches; re-arm only on a clean reconcile.
            </p>
          </div>
        ) : workers.isLoading ? (
          <p className="muted">Loading worker state…</p>
        ) : (
          <p className="muted">No worker reporting — controls are unavailable.</p>
        )}
      </Panel>

      <Panel title="Pending commands" icon={Power}>
        {commands.error ? (
          <div className="alert">Could not load commands: {errMessage(commands.error)}</div>
        ) : commands.isLoading ? (
          <p className="muted">Loading…</p>
        ) : pending.length ? (
          <ul className="cmd-list">
            {pending.map((c) => (
              <li key={String(c.id)}>
                <StatusBadge kind={CMD_KIND[text(c.status)] ?? 'neutral'}>{text(c.kind)}</StatusBadge>
                <span className="mono muted">{text(c.worker_id) || '—'}</span>
                <span className="mono muted">{timeAgo(c.created_at, now)}</span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="muted">No pending commands.</p>
        )}
      </Panel>

      <Panel title="Risk events" icon={ShieldAlert}>
        {events.error ? (
          <div className="alert">Could not load risk events: {errMessage(events.error)}</div>
        ) : events.isLoading ? (
          <div className="skeleton" style={{ height: 160 }} />
        ) : (
          <DataTable
            columns={eventColumns}
            rows={eventRows}
            rowKey={(r) => r.id}
            initialSortKey="when"
            initialDir="desc"
            empty={
              <EmptyState
                icon={ShieldAlert}
                head="No risk events"
                sub="Limit breaches, drift, halts, and kill-switch arming land here as the worker reports them."
                hint="reads: risk_events"
              />
            }
          />
        )}
      </Panel>
    </div>
  )
}
