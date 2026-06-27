import { FlaskConical } from 'lucide-react'
import { ViewStub } from '../ui'

export function Backtests() {
  return (
    <ViewStub
      icon={FlaskConical}
      head="Backtests"
      sub="Every candidate from the backtests table — including rejected ones — with Sharpe, DSR, PBO, max drawdown, and cost sensitivity, so the rigor filter is visibly working."
      milestone="M2.3"
    />
  )
}
