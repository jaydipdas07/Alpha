import {
  Gauge,
  Boxes,
  Telescope,
  FlaskConical,
  ClipboardCheck,
  Network,
  ShieldAlert,
  SlidersHorizontal,
  type LucideIcon,
} from 'lucide-react'

// The cockpit's view map. One entry per Phase-2 dashboard surface (DESIGN_v4
// "Phase 2 — Cockpit"). The shell renders these in order; each view is fleshed
// out in its milestone PR. Keep this list and the VIEWS registry in App.tsx in
// lockstep — the ViewId union is the single source of truth for both.
export type ViewId =
  | 'overview'
  | 'strategies'
  | 'discovery'
  | 'backtests'
  | 'approvals'
  | 'exchanges'
  | 'risk'
  | 'config'

export interface NavItem {
  id: ViewId
  label: string
  icon: LucideIcon
  /** One-line description shown in the topbar under the title. */
  blurb: string
}

export const NAV: readonly NavItem[] = [
  {
    id: 'overview',
    label: 'Overview',
    icon: Gauge,
    blurb: 'System health, heartbeats, and live status at a glance.',
  },
  {
    id: 'strategies',
    label: 'Strategies',
    icon: Boxes,
    blurb: 'Discovered + deployed strategies and their lifecycle state.',
  },
  {
    id: 'discovery',
    label: 'Discovery',
    icon: Telescope,
    blurb: 'Nightly discovery runs — proposals, survivors, trial counts.',
  },
  {
    id: 'backtests',
    label: 'Backtests',
    icon: FlaskConical,
    blurb: 'Every candidate incl. rejected — Sharpe / DSR / PBO / maxDD.',
  },
  {
    id: 'approvals',
    label: 'Approvals',
    icon: ClipboardCheck,
    blurb: 'The human approval inbox — the per-strategy go-live gate.',
  },
  {
    id: 'exchanges',
    label: 'Exchanges',
    icon: Network,
    blurb: 'Venues + tradable instruments and their adapters.',
  },
  {
    id: 'risk',
    label: 'Risk',
    icon: ShieldAlert,
    blurb: 'Kill-switch, halts, and open risk events.',
  },
  {
    id: 'config',
    label: 'Config',
    icon: SlidersHorizontal,
    blurb: 'The tunables — risk / costs / rigor (read-only).',
  },
]
