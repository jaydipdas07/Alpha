import { Network } from 'lucide-react'
import { ViewStub } from '../ui'

export function Exchanges() {
  return (
    <ViewStub
      icon={Network}
      head="Exchanges & Instruments"
      sub="Configured venues (Delta / Binance / Kite / Dhan / Upstox) and the tradable universe with lot/tick sizes, from config/venues.yaml + config/instruments.yaml."
      milestone="M2.4"
    />
  )
}
