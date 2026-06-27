// Small client-side helpers for the cockpit. Formatting + SDK-shape unwrapping
// only — no business logic (the cockpit is a read surface; all logic lives in
// alpha-core / the pod functions).

/** Unwrap a `{ items: T[] }` SDK list response to its array (or []). */
export function getItems<T>(value: unknown): T[] {
  if (
    value &&
    typeof value === 'object' &&
    'items' in value &&
    Array.isArray((value as { items: unknown }).items)
  ) {
    return (value as { items: T[] }).items
  }
  return []
}

/** A human message for any thrown/SDK error value. */
export function errMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

// --- display formatting -----------------------------------------------------
// Money/price/qty arrive as string-encoded Decimal (B5). The cockpit is a read
// surface: it parses to a JS number ONLY for visual display (charts, labels) and
// never does money arithmetic — all P&L math lives in alpha-core. Never write a
// derived money value back to the pod from here.

/** Parse a string-Decimal / number for display. Non-finite → 0. */
export function parseNum(value: unknown): number {
  if (typeof value === 'number') return Number.isFinite(value) ? value : 0
  if (typeof value === 'string' && value.trim() !== '') {
    const n = Number(value)
    return Number.isFinite(n) ? n : 0
  }
  return 0
}

/** ISO/DATETIME string → unix seconds (UTC). Unparseable → 0. */
export function toUnixSeconds(value: unknown): number {
  if (typeof value !== 'string') return 0
  const t = Date.parse(value)
  return Number.isNaN(t) ? 0 : Math.floor(t / 1000)
}

/** Format a money value for display (thousands sep, 2dp). */
export function fmtMoney(value: unknown): string {
  return parseNum(value).toLocaleString('en-US', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })
}

/** Compact integer for count tiles. `null` while loading → "…". */
export function fmtInt(value: number | null): string {
  return value === null ? '…' : value.toLocaleString('en-US')
}

/** Relative "time ago" for heartbeats/timestamps. */
export function timeAgo(iso: unknown, now: number = Date.now()): string {
  if (typeof iso !== 'string') return '—'
  const t = Date.parse(iso)
  if (Number.isNaN(t)) return '—'
  const s = Math.max(0, Math.round((now - t) / 1000))
  if (s < 60) return `${s}s ago`
  const m = Math.round(s / 60)
  if (m < 60) return `${m}m ago`
  const h = Math.round(m / 60)
  if (h < 24) return `${h}h ago`
  return `${Math.round(h / 24)}d ago`
}
