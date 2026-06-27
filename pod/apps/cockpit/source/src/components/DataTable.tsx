import { useMemo, useState, type ReactNode } from 'react'
import { ChevronUp, ChevronDown } from 'lucide-react'

// A small, generic, client-side-sortable table over already-fetched records.
// Pure presentation (no SDK) — views fetch + shape rows, then hand them here.
// Reused across the cockpit's table views (Backtests, Discovery, Strategies, …).

export interface Column<T> {
  key: string
  header: string
  align?: 'left' | 'right'
  /** Present ⇒ the column is sortable; returns the comparable value for a row. */
  sort?: (row: T) => number | string
  render: (row: T) => ReactNode
}

export function DataTable<T>({
  columns,
  rows,
  rowKey,
  initialSortKey,
  initialDir = 'asc',
  empty,
}: {
  columns: Column<T>[]
  rows: T[]
  rowKey: (row: T) => string
  initialSortKey?: string
  initialDir?: 'asc' | 'desc'
  empty?: ReactNode
}) {
  const [sortKey, setSortKey] = useState<string | undefined>(initialSortKey)
  const [dir, setDir] = useState<'asc' | 'desc'>(initialDir)

  const sorted = useMemo(() => {
    const col = columns.find((c) => c.key === sortKey)
    if (!col?.sort) return rows
    const get = col.sort
    const factor = dir === 'asc' ? 1 : -1
    return [...rows].sort((a, b) => {
      const va = get(a)
      const vb = get(b)
      if (va < vb) return -factor
      if (va > vb) return factor
      return 0
    })
  }, [rows, columns, sortKey, dir])

  function onSort(col: Column<T>) {
    if (!col.sort) return
    if (sortKey === col.key) setDir((d) => (d === 'asc' ? 'desc' : 'asc'))
    else {
      setSortKey(col.key)
      setDir('asc')
    }
  }

  if (!rows.length && empty) return <>{empty}</>

  return (
    <div className="table-wrap">
      <table className="data-table">
        <thead>
          <tr>
            {columns.map((col) => (
              <th
                key={col.key}
                className={[col.align === 'right' ? 'r' : '', col.sort ? 'sortable' : '']
                  .filter(Boolean)
                  .join(' ')}
                onClick={() => onSort(col)}
                role={col.sort ? 'button' : undefined}
                tabIndex={col.sort ? 0 : undefined}
                onKeyDown={
                  col.sort
                    ? (e) => {
                        if (e.key === 'Enter' || e.key === ' ') {
                          e.preventDefault()
                          onSort(col)
                        }
                      }
                    : undefined
                }
                aria-sort={sortKey === col.key ? (dir === 'asc' ? 'ascending' : 'descending') : undefined}
              >
                <span className="th-inner">
                  {col.header}
                  {sortKey === col.key ? (
                    dir === 'asc' ? (
                      <ChevronUp size={12} />
                    ) : (
                      <ChevronDown size={12} />
                    )
                  ) : null}
                </span>
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {sorted.map((row) => (
            <tr key={rowKey(row)}>
              {columns.map((col) => (
                <td key={col.key} className={col.align === 'right' ? 'r' : ''}>
                  {col.render(row)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
