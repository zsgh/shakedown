import { useCallback, useEffect, useState } from 'react'
import { ApiError } from './api'

interface AsyncOptions {
  /** 自动轮询间隔（毫秒）；页面不可见时暂停。 */
  refreshMs?: number
}

export function useAsync<T>(
  fn: () => Promise<T>,
  deps: unknown[] = [],
  options: AsyncOptions = {},
): {
  data: T | null
  error: string | null
  loading: boolean
  reload: () => void
} {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [nonce, setNonce] = useState(0)

  const reload = useCallback(() => setNonce((value) => value + 1), [])

  useEffect(() => {
    let alive = true
    setLoading(true)

    const run = () => {
      if (typeof document !== 'undefined' && document.hidden) return
      fn()
        .then((result) => {
          if (!alive) return
          setData(result)
          setError(null)
        })
        .catch((err: unknown) => {
          if (!alive) return
          setError(err instanceof ApiError ? err.message : String(err))
        })
        .finally(() => {
          if (alive) setLoading(false)
        })
    }

    run()
    const timer = options.refreshMs ? window.setInterval(run, options.refreshMs) : null
    return () => {
      alive = false
      if (timer) window.clearInterval(timer)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, nonce, options.refreshMs])

  return { data, error, loading, reload }
}

export function useDebounced<T>(value: T, delay = 300): T {
  const [debounced, setDebounced] = useState(value)
  useEffect(() => {
    const timer = window.setTimeout(() => setDebounced(value), delay)
    return () => window.clearTimeout(timer)
  }, [value, delay])
  return debounced
}

export function useDocumentTitle(title: string) {
  useEffect(() => {
    document.title = title ? `${title} · Shakedown` : 'Shakedown'
  }, [title])
}
