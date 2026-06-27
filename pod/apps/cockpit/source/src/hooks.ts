import { useRecordAggregates } from 'lemma-sdk/react'
import { lemmaClient } from './lemma-client'

/**
 * A row count for a table via a server-side COUNT aggregate (no full-table
 * load). Returns `null` while loading or on error, so callers can show a
 * placeholder. Refreshes on mount/navigation — for a live count over the table
 * WebSocket use `useLiveRecords(...).records.length` instead.
 */
export function useCount(tableName: string): number | null {
  const { row, isLoading, error } = useRecordAggregates({
    client: lemmaClient,
    tableName,
    metrics: [{ key: 'count', op: 'count' }],
  })
  if (isLoading || error) return null
  return Number((row as { count?: unknown } | null)?.count ?? 0)
}
