import { Plug, Activity, Database } from 'lucide-react'
import { useCurrentUser, useTableList } from 'lemma-sdk/react'
import { lemmaClient } from '../lemma-client'
import { getItems, errMessage } from '../lib'
import { Panel, EmptyState, StatusBadge } from '../ui'

// Overview — the first 30 seconds. For M2.1 (the shell) this proves the app is
// genuinely wired to the Vault pod: it authenticates, reads the pod's table
// inventory over the SDK, and renders designed loading / error / empty states.
// The live status strip (heartbeats, reconcile, data freshness, open
// risk_events) and the equity chart land here in M2.2 / B2.1b via watchChanges.

export function Overview() {
  const { user } = useCurrentUser({ client: lemmaClient })
  const tablesQuery = useTableList(lemmaClient, lemmaClient.podId)
  const tables = getItems<{ name?: string }>(tablesQuery.data)
    .map((t) => t.name ?? '')
    .filter(Boolean)
    .sort()

  return (
    <div className="stack">
      <div className="grid cols-2">
        <Panel
          title="Pod connection"
          icon={Plug}
          action={<StatusBadge kind="ok">live</StatusBadge>}
        >
          <dl className="kv">
            <dt>Pod</dt>
            <dd>{lemmaClient.podId ?? '—'}</dd>
            <dt>Signed in</dt>
            <dd>{user?.email ?? '—'}</dd>
            <dt>Tables</dt>
            <dd>{tablesQuery.isLoading ? '…' : tables.length}</dd>
          </dl>
        </Panel>

        <Panel title="Live status" icon={Activity}>
          <EmptyState
            icon={Activity}
            head="Status strip lands in M2.2"
            sub="Worker + research heartbeats, last reconcile, data freshness, and open risk_events — streamed live via watchChanges, with the equity chart alongside."
            hint="next: B2.1b → M2.2"
          />
        </Panel>
      </div>

      <Panel title="Pod tables" icon={Database}>
        {tablesQuery.error ? (
          <div className="alert">Could not load tables: {errMessage(tablesQuery.error)}</div>
        ) : tablesQuery.isLoading ? (
          <ul className="chips">
            {Array.from({ length: 8 }).map((_, i) => (
              <li key={i} className="skeleton" style={{ width: 96, height: 22 }} />
            ))}
          </ul>
        ) : tables.length ? (
          <ul className="chips">
            {tables.map((name) => (
              <li key={name}>{name}</li>
            ))}
          </ul>
        ) : (
          <p className="muted">No tables found in this pod.</p>
        )}
      </Panel>
    </div>
  )
}
