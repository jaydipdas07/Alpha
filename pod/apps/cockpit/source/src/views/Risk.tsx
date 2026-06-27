import { ShieldAlert } from 'lucide-react'
import { ViewStub } from '../ui'

export function Risk() {
  return (
    <ViewStub
      icon={ShieldAlert}
      head="Risk"
      sub="Open risk_events, the latching kill-switch state, and the arm / flatten / clear-halt controls. These write to the commands table — the worker executes; the pod never trades (TEST-8)."
      milestone="M2.4 (controls wired in Phase 3)"
    />
  )
}
