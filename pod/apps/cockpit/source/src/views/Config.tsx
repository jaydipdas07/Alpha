import { SlidersHorizontal } from 'lucide-react'
import { CONFIG, flatten } from '../config-snapshot'
import { Panel, EmptyState } from '../ui'

// M2.4 — the tunables, read-only. config/ is the single source of truth (no magic
// numbers in code); the cockpit mirrors a committed snapshot and never edits it —
// changes happen in config/ + PR review.

const SECTIONS: { file: string; title: string }[] = [
  { file: 'risk', title: 'Risk limits' },
  { file: 'costs', title: 'Costs & taxes' },
  { file: 'rigor', title: 'Rigor gate' },
  { file: 'discovery', title: 'Discovery' },
  { file: 'portfolio', title: 'Portfolio' },
]

function fmtVal(v: unknown): string {
  if (v === null || v === undefined || v === '') return '—'
  if (typeof v === 'boolean') return v ? 'true' : 'false'
  return String(v)
}

function ConfigGrid({ data }: { data: unknown }) {
  const rows = flatten(data)
  if (!rows.length) return <p className="muted">empty</p>
  return (
    <dl className="cfg-grid">
      {rows.map(([path, val]) => (
        <div className="cfg-grid-row" key={path}>
          <dt className="mono">{path}</dt>
          <dd className="mono">{fmtVal(val)}</dd>
        </div>
      ))}
    </dl>
  )
}

export function Config() {
  const sections = SECTIONS.filter((s) => CONFIG.files[s.file] != null)

  return (
    <div className="stack">
      <div className="snapshot-note">
        Read-only snapshot of <code>config/</code> — the single source of truth. Edits happen in
        config + PR review, never in the cockpit. Generated{' '}
        <span className="mono">{CONFIG.generated_at}</span>.
      </div>

      {sections.length ? (
        sections.map((s) => (
          <Panel key={s.file} title={s.title} icon={SlidersHorizontal}>
            <ConfigGrid data={CONFIG.files[s.file]} />
          </Panel>
        ))
      ) : (
        <Panel title="Config" icon={SlidersHorizontal}>
          <EmptyState
            icon={SlidersHorizontal}
            head="No config snapshot"
            sub="Run scripts/snapshot_cockpit_config.py to mirror config/*.yaml into the cockpit."
            hint="source: config/"
          />
        </Panel>
      )}
    </div>
  )
}
