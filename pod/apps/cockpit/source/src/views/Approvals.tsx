import { ClipboardCheck } from 'lucide-react'
import { ViewStub } from '../ui'

export function Approvals() {
  return (
    <ViewStub
      icon={ClipboardCheck}
      head="Approvals"
      sub="The human approval inbox — Workflow B deployment FORMs awaiting a per-strategy go-live decision. Every go-live is a human approval; the cockpit never auto-approves."
      milestone="M2.4"
    />
  )
}
