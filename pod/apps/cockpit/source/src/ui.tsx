import type { ReactNode } from 'react'
import type { LucideIcon } from 'lucide-react'

// Shared presentational atoms for the cockpit. Pure UI — they take data as
// props and never call the SDK, so views stay the only place data is fetched.

export type StatusKind = 'ok' | 'info' | 'warn' | 'danger' | 'neutral'

/** A bordered, uppercase status chip — status as a badge, never color alone. */
export function StatusBadge({ kind, children }: { kind: StatusKind; children: ReactNode }) {
  return <span className={`badge ${kind}`}>{children}</span>
}

/** A titled card. `action` renders at the right of the header (e.g. a badge). */
export function Panel({
  title,
  icon: Icon,
  action,
  children,
}: {
  title: string
  icon?: LucideIcon
  action?: ReactNode
  children: ReactNode
}) {
  return (
    <section className="panel">
      <header>
        <span className="title">
          {Icon ? <Icon size={16} /> : null}
          {title}
        </span>
        {action ?? null}
      </header>
      <div className="body">{children}</div>
    </section>
  )
}

/** A single labelled figure — mono value, uppercase label. */
export function Metric({
  label,
  value,
  foot,
}: {
  label: string
  value: ReactNode
  foot?: ReactNode
}) {
  return (
    <div className="metric">
      <span className="label">{label}</span>
      <span className="value">{value}</span>
      {foot ? <span className="foot">{foot}</span> : null}
    </div>
  )
}

/** A designed empty state — icon, headline, sub, and an optional mono hint. */
export function EmptyState({
  icon: Icon,
  head,
  sub,
  hint,
}: {
  icon: LucideIcon
  head: string
  sub: string
  hint?: string
}) {
  return (
    <div className="empty">
      <span className="icon">
        <Icon size={22} />
      </span>
      <span className="head">{head}</span>
      <span className="sub">{sub}</span>
      {hint ? <span className="hint">{hint}</span> : null}
    </div>
  )
}

/**
 * A placeholder for a view that has its nav slot but not its data wiring yet.
 * Honest about what lands here and in which milestone — not a dead "coming soon".
 */
export function ViewStub({
  icon,
  head,
  sub,
  milestone,
}: {
  icon: LucideIcon
  head: string
  sub: string
  milestone: string
}) {
  return (
    <section className="panel">
      <div className="body">
        <EmptyState icon={icon} head={head} sub={sub} hint={`Builds out in ${milestone}`} />
      </div>
    </section>
  )
}
