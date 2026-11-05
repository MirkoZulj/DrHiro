import { useMemo } from 'react'

export interface SparklineProps {
  points: { date: string; value: number | null }[]
  markers?: { date: string; kind: string; finding_id: string }[]
  width?: number
  height?: number
}

/** Hand-built inline SVG sparkline (no chart library — spec F12/A5).
 *  Null values create gaps; change-point markers render as vertical lines. */
export default function Sparkline({ points, markers = [], width = 480, height = 80 }: SparklineProps) {
  const { path, gapPaths, markerXs } = useMemo(() => {
    const vals = points.filter((p) => p.value !== null) as { date: string; value: number }[]
    if (vals.length < 2) return { path: '', gapPaths: [] as string[], xs: new Map<string, number>(), markerXs: [] as number[] }
    const min = Math.min(...vals.map((v) => v.value))
    const max = Math.max(...vals.map((v) => v.value))
    const span = max - min || 1
    const pad = 4
    const x = (i: number) => pad + (i / (points.length - 1)) * (width - 2 * pad)
    const y = (v: number) => height - pad - ((v - min) / span) * (height - 2 * pad)
    const xs = new Map<string, number>()
    let d = ''
    const gaps: string[] = []
    let inGap = true
    points.forEach((p, i) => {
      xs.set(p.date, x(i))
      if (p.value === null) {
        if (!inGap) gaps.push(d)
        d = ''
        inGap = true
        return
      }
      d += (d ? ' L' : 'M') + `${x(i).toFixed(1)},${y(p.value).toFixed(1)}`
      inGap = false
    })
    if (d) gaps.push(d)
    const mXs = markers.map((m) => xs.get(m.date)).filter((v): v is number => v !== undefined)
    return { path: d, gapPaths: gaps, xs, markerXs: mXs }
  }, [points, markers, width, height])

  if (!path) return <div className="muted" style={{ fontSize: 12 }}>no data</div>
  return (
    <svg width={width} height={height} style={{ display: 'block', maxWidth: '100%' }}>
      {markerXs.map((mx, i) => (
        <line key={i} x1={mx} y1={0} x2={mx} y2={height} stroke="#c0392b" strokeWidth={1.5} strokeDasharray="3,2" />
      ))}
      {gapPaths.map((d, i) => (
        <path key={i} d={d} fill="none" stroke="var(--color-accent, #4a7fb5)" strokeWidth={1.5} />
      ))}
    </svg>
  )
}
