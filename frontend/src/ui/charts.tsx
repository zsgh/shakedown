import { useId } from 'react'

const TONES: Record<string, string> = {
  brand: '#0e9384',
  success: '#067647',
  danger: '#b42318',
  warning: '#b54708',
  info: '#175cd3',
  violet: '#6941c6',
  muted: '#98a2b3',
}

export function Sparkline({
  values,
  width = 120,
  height = 32,
  tone = 'brand',
  showArea = true,
}: {
  values: number[]
  width?: number
  height?: number
  tone?: string
  showArea?: boolean
}) {
  const id = useId().replace(/:/g, '')
  const color = TONES[tone] ?? TONES.brand
  if (!values.length) return <div className="sparkline-empty" style={{ width, height }} />
  const max = Math.max(...values, 1)
  const min = Math.min(...values, 0)
  const span = max - min || 1
  const step = values.length > 1 ? width / (values.length - 1) : width
  const points = values.map((value, index) => {
    const x = index * step
    const y = height - 3 - ((value - min) / span) * (height - 6)
    return `${x.toFixed(1)},${y.toFixed(1)}`
  })
  const last = points[points.length - 1].split(',')
  return (
    <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`} className="sparkline">
      <defs>
        <linearGradient id={`grad-${id}`} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor={color} stopOpacity="0.22" />
          <stop offset="100%" stopColor={color} stopOpacity="0" />
        </linearGradient>
      </defs>
      {showArea && (
        <polygon
          points={`0,${height} ${points.join(' ')} ${width},${height}`}
          fill={`url(#grad-${id})`}
        />
      )}
      <polyline
        points={points.join(' ')}
        fill="none"
        stroke={color}
        strokeWidth="1.8"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <circle cx={last[0]} cy={last[1]} r="2.4" fill={color} />
    </svg>
  )
}

export interface Segment {
  label: string
  value: number
  tone: string
}

export function Donut({
  segments,
  size = 132,
  thickness = 12,
  centerLabel,
  centerHint,
}: {
  segments: Segment[]
  size?: number
  thickness?: number
  centerLabel?: string
  centerHint?: string
}) {
  const total = segments.reduce((sum, item) => sum + item.value, 0)
  const radius = (size - thickness) / 2
  const circumference = 2 * Math.PI * radius
  let offset = 0
  return (
    <div className="donut" style={{ width: size, height: size }}>
      <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`}>
        <circle
          cx={size / 2}
          cy={size / 2}
          r={radius}
          fill="none"
          stroke="#eef0f3"
          strokeWidth={thickness}
        />
        {total > 0 &&
          segments.map((segment) => {
            const length = (segment.value / total) * circumference
            const circle = (
              <circle
                key={segment.label}
                cx={size / 2}
                cy={size / 2}
                r={radius}
                fill="none"
                stroke={TONES[segment.tone] ?? TONES.muted}
                strokeWidth={thickness}
                strokeDasharray={`${Math.max(length - 2, 0)} ${circumference}`}
                strokeDashoffset={-offset}
                strokeLinecap="round"
                transform={`rotate(-90 ${size / 2} ${size / 2})`}
              />
            )
            offset += length
            return circle
          })}
      </svg>
      <div className="donut__center">
        <strong>{centerLabel ?? total}</strong>
        {centerHint && <span>{centerHint}</span>}
      </div>
    </div>
  )
}

export function Legend({ segments }: { segments: Segment[] }) {
  return (
    <ul className="legend">
      {segments.map((segment) => (
        <li key={segment.label}>
          <span className={`legend__dot tone-${segment.tone}`} />
          <span>{segment.label}</span>
          <strong>{segment.value}</strong>
        </li>
      ))}
    </ul>
  )
}

export function TrendBars({
  runs,
  height = 120,
}: {
  runs: Array<{ id: number; pass_rate: number; status: string; total: number }>
  height?: number
}) {
  const width = 100
  const points = [...runs].reverse()
  if (!points.length) return null
  const maxRate = Math.max(...points.map((run) => (run.pass_rate > 1 ? run.pass_rate : run.pass_rate * 100)), 100)
  return (
    <div className="trend" style={{ height }}>
      <div className="trend__grid">
        {[0, 25, 50, 75, 100].map((line) => (
          <span key={line} style={{ bottom: `${line}%` }} />
        ))}
      </div>
      <div className="trend__bars">
        {points.map((run) => {
          const rate = run.pass_rate > 1 ? run.pass_rate : run.pass_rate * 100
          const barHeight = Math.max(4, (rate / maxRate) * 100)
          return (
            <div
              key={run.id}
              className={`trend__bar ${rate >= 90 ? 'is-good' : rate >= 70 ? 'is-warn' : 'is-bad'}`}
              style={{ height: `${barHeight}%` }}
              title={`#${run.id} · ${rate.toFixed(1)}% · ${run.total} 条`}
            >
              <span>{Math.round(rate)}</span>
            </div>
          )
        })}
      </div>
      <div className="trend__axis">
        {points.map((run) => (
          <span key={run.id}>#{run.id}</span>
        ))}
      </div>
      <span className="sr-only">{width}</span>
    </div>
  )
}
