import { Telescope } from 'lucide-react'
import { ViewStub } from '../ui'

export function Discovery() {
  return (
    <ViewStub
      icon={Telescope}
      head="Discovery"
      sub="Nightly discovery cycles from the discovery_runs table — proposals, survivors, per-run and cumulative trial counts, and run status."
      milestone="M2.4"
    />
  )
}
