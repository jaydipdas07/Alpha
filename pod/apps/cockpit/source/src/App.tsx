import { useState, type ComponentType } from 'react'
import { Activity } from 'lucide-react'
import { useCurrentUser } from 'lemma-sdk/react'
import { lemmaClient } from './lemma-client'
import { NAV, type ViewId } from './nav'
import { Overview } from './views/Overview'
import { Strategies } from './views/Strategies'
import { Discovery } from './views/Discovery'
import { Backtests } from './views/Backtests'
import { Approvals } from './views/Approvals'
import { Exchanges } from './views/Exchanges'
import { Risk } from './views/Risk'
import { Config } from './views/Config'

// View registry. Keyed by ViewId so the compiler enforces one component per nav
// entry. Navigation is in-app state (not URL routing) — apps run inside the pod
// shell's iframe, so there are no top-level navigation assumptions to make.
const VIEWS: Record<ViewId, ComponentType> = {
  overview: Overview,
  strategies: Strategies,
  discovery: Discovery,
  backtests: Backtests,
  approvals: Approvals,
  exchanges: Exchanges,
  risk: Risk,
  config: Config,
}

const POD_ID = lemmaClient.podId

function ConnPill() {
  // AuthGuard already gated auth, so reaching here means we're connected; this
  // just surfaces it. useCurrentUser is deduped by TanStack across the app.
  const { user } = useCurrentUser({ client: lemmaClient })
  const ok = Boolean(user)
  return (
    <span className="conn" title={ok ? 'Authenticated to the Vault pod' : 'Connecting…'}>
      <span className={`dot ${ok ? 'ok' : 'warn'}`} />
      {ok ? 'connected' : 'connecting…'}
    </span>
  )
}

export function App() {
  const [view, setView] = useState<ViewId>('overview')
  const { user } = useCurrentUser({ client: lemmaClient })
  const active = NAV.find((item) => item.id === view) ?? NAV[0]
  const Body = VIEWS[active.id]

  return (
    <div className="app">
      <aside className="sidebar">
        <div className="brand">
          <span className="logo">
            <Activity size={18} />
          </span>
          <div>
            <div className="name">ALPHA</div>
            <div className="sub">cockpit</div>
          </div>
        </div>

        <nav className="nav" aria-label="Cockpit sections">
          {NAV.map((item) => {
            const Icon = item.icon
            const isActive = item.id === active.id
            return (
              <button
                key={item.id}
                type="button"
                className={`nav-item${isActive ? ' active' : ''}`}
                aria-current={isActive ? 'page' : undefined}
                onClick={() => setView(item.id)}
              >
                <Icon size={17} />
                {item.label}
              </button>
            )
          })}
        </nav>

        <div className="spacer" />
        <div className="side-foot">
          <span className="who">{user?.email ?? 'signed in'}</span>
          <span className="ver">pod {POD_ID ? `${POD_ID.slice(0, 8)}…` : '—'}</span>
        </div>
      </aside>

      <main className="main">
        <div className="topbar">
          <div className="crumb">
            <h1>{active.label}</h1>
            <span className="blurb">{active.blurb}</span>
          </div>
          <ConnPill />
        </div>
        <div className="content">
          <Body />
        </div>
      </main>
    </div>
  )
}
