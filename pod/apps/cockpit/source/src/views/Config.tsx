import { SlidersHorizontal } from 'lucide-react'
import { ViewStub } from '../ui'

export function Config() {
  return (
    <ViewStub
      icon={SlidersHorizontal}
      head="Config"
      sub="The tunables — risk / costs / rigor — shown read-only. Config is the single source of truth (no magic numbers in code); edits happen in config/ + PR review, never in the cockpit."
      milestone="M2.4"
    />
  )
}
