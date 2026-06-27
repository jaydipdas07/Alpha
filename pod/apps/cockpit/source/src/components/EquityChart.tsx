import { useEffect, useRef } from 'react'
import {
  createChart,
  AreaSeries,
  ColorType,
  type IChartApi,
  type ISeriesApi,
  type UTCTimestamp,
} from 'lightweight-charts'

export interface EquityPoint {
  time: UTCTimestamp
  value: number
}

/**
 * An equity / P&L area chart (lightweight-charts v5). Created once; `setData` is
 * re-run when `points` change, so a live `useLiveRecords` feed re-renders the
 * curve in place. `points` must be ascending + unique by `time` (the caller
 * dedupes). Terminal palette; sized to its container via `autoSize`.
 */
export function EquityChart({ points, height = 280 }: { points: EquityPoint[]; height?: number }) {
  const elRef = useRef<HTMLDivElement>(null)
  const chartRef = useRef<IChartApi | null>(null)
  const seriesRef = useRef<ISeriesApi<'Area'> | null>(null)

  useEffect(() => {
    const el = elRef.current
    if (!el) return
    const chart = createChart(el, {
      autoSize: true,
      layout: {
        background: { type: ColorType.Solid, color: 'transparent' },
        textColor: '#93a1ab',
        fontFamily: 'ui-monospace, "SF Mono", Menlo, monospace',
        fontSize: 11,
        attributionLogo: false,
      },
      grid: {
        vertLines: { color: 'rgba(29, 38, 46, 0.5)' },
        horzLines: { color: 'rgba(29, 38, 46, 0.5)' },
      },
      rightPriceScale: { borderColor: '#1d262e' },
      timeScale: { borderColor: '#1d262e', timeVisible: true, secondsVisible: false },
    })
    const series = chart.addSeries(AreaSeries, {
      lineColor: '#36d39a',
      lineWidth: 2,
      topColor: 'rgba(54, 211, 154, 0.28)',
      bottomColor: 'rgba(54, 211, 154, 0.02)',
      priceLineVisible: false,
    })
    chartRef.current = chart
    seriesRef.current = series
    return () => {
      chart.remove()
      chartRef.current = null
      seriesRef.current = null
    }
  }, [])

  useEffect(() => {
    seriesRef.current?.setData(points)
    if (points.length) chartRef.current?.timeScale().fitContent()
  }, [points])

  return <div ref={elRef} className="chart" style={{ height }} />
}
