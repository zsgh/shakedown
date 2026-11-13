/** 时间、耗时、数字的统一展示。 */

export function formatTime(value?: string | null): string {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleString('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  })
}

export function formatFullTime(value?: string | null): string {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleString('zh-CN', { hour12: false })
}

export function fromNow(value?: string | null): string {
  if (!value) return '—'
  const date = new Date(value)
  const diff = Date.now() - date.getTime()
  if (Number.isNaN(diff)) return value
  const minute = 60_000
  const hour = 60 * minute
  const day = 24 * hour
  if (diff < minute) return '刚刚'
  if (diff < hour) return `${Math.floor(diff / minute)} 分钟前`
  if (diff < day) return `${Math.floor(diff / hour)} 小时前`
  if (diff < 7 * day) return `${Math.floor(diff / day)} 天前`
  return formatTime(value)
}

export function duration(ms?: number | null): string {
  if (ms === null || ms === undefined || Number.isNaN(ms)) return '—'
  if (ms < 1000) return `${Math.round(ms)} ms`
  if (ms < 60_000) return `${(ms / 1000).toFixed(ms < 10_000 ? 1 : 0)} s`
  const minutes = Math.floor(ms / 60_000)
  const seconds = Math.round((ms % 60_000) / 1000)
  return `${minutes}m ${seconds}s`
}

export function percent(value?: number | null, digits = 1): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—'
  const normalized = value > 1 ? value : value * 100
  return `${normalized.toFixed(digits).replace(/\.0$/, '')}%`
}

export function num(value?: number | null): string {
  if (value === null || value === undefined) return '0'
  if (value >= 10_000) return `${(value / 1000).toFixed(1)}k`
  return String(value)
}

export function shortPath(value?: string | null, max = 52): string {
  if (!value) return '—'
  if (value.length <= max) return value
  return `…${value.slice(-(max - 1))}`
}

export function shortHash(value?: string | null): string {
  if (!value) return '—'
  return value.slice(0, 8)
}

export function relativeRun(status?: string | null): string {
  if (!status) return '未执行'
  return status
}

export function runDuration(run: { started_at?: string | null; finished_at?: string | null; created_at?: string | null }): string {
  if (!run.started_at) return '—'
  const end = run.finished_at ? new Date(run.finished_at).getTime() : Date.now()
  return duration(end - new Date(run.started_at).getTime())
}

export function safeJson(value: unknown): string {
  if (value === null || value === undefined) return ''
  if (typeof value === 'string') return value
  try {
    return JSON.stringify(value, null, 2)
  } catch {
    return String(value)
  }
}

export function methodTone(method?: string): string {
  switch ((method ?? '').toUpperCase()) {
    case 'GET':
      return 'blue'
    case 'POST':
      return 'success'
    case 'PUT':
    case 'PATCH':
      return 'warning'
    case 'DELETE':
      return 'danger'
    default:
      return 'muted'
  }
}
