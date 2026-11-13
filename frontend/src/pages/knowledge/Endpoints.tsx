import { useEffect, useMemo, useState } from 'react'
import { useParams, useSearchParams } from 'react-router-dom'
import { api } from '../../lib/api'
import { safeJson } from '../../lib/format'
import { useAsync } from '../../lib/hooks'
import { ENDPOINT_STATE } from '../../lib/labels'
import type { Endpoint } from '../../lib/types'
import { Icon } from '../../ui/icons'
import {
  Button,
  Card,
  CopyButton,
  EmptyState,
  ErrorBox,
  Pill,
  Skeleton,
  Status,
} from '../../ui/kit'
import { Drawer, useToast } from '../../ui/overlays'

const METHODS = ['GET', 'POST', 'PUT', 'PATCH', 'DELETE']

export default function Endpoints() {
  const { projectId } = useParams()
  const [searchParams, setSearchParams] = useSearchParams()
  const toast = useToast()
  const [query, setQuery] = useState('')
  const [method, setMethod] = useState('')
  const [state, setState] = useState('')
  const [selectedId, setSelectedId] = useState<number | null>(
    searchParams.get('endpoint') ? Number(searchParams.get('endpoint')) : null,
  )
  const [busy, setBusy] = useState(false)

  const endpoints = useAsync<Endpoint[]>(() => api.get(`/projects/${projectId}/endpoints`), [projectId])
  const all = endpoints.data ?? []
  const selected = all.find((item) => item.id === selectedId) ?? null

  useEffect(() => {
    const fromUrl = searchParams.get('endpoint')
    const next = fromUrl ? Number(fromUrl) : null
    if (next !== selectedId) setSelectedId(next)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchParams])

  const filtered = useMemo(
    () =>
      all.filter((item) => {
        if (method && item.method.toUpperCase() !== method) return false
        if (state && (item.consistency?.state ?? 'unknown') !== state) return false
        if (query.trim()) {
          const haystack = `${item.method} ${item.path} ${item.summary}`.toLowerCase()
          if (!haystack.includes(query.trim().toLowerCase())) return false
        }
        return true
      }),
    [all, method, state, query],
  )

  const inconsistent = all.filter((item) => ['inconsistent', 'conflict'].includes(item.consistency?.state ?? '')).length
  const consistent = all.filter((item) => item.consistency?.state === 'consistent').length
  const single = all.filter((item) => !item.consistency?.state || item.consistency?.state === 'unknown').length

  async function extract() {
    setBusy(true)
    try {
      const result = await api.post<{ endpoints: number }>(`/projects/${projectId}/agent/extract-endpoints`)
      toast.success('Agent 抽接口完成', `新增 ${result.endpoints} 个接口`)
      endpoints.reload()
    } catch (error) {
      toast.error('抽取失败', String(error))
    } finally {
      setBusy(false)
    }
  }

  async function check() {
    setBusy(true)
    try {
      await api.post(`/projects/${projectId}/consistency/check`)
      toast.success('一致性对照已重做')
      endpoints.reload()
    } catch (error) {
      toast.error('对照失败', String(error))
    } finally {
      setBusy(false)
    }
  }

  function openEndpoint(endpoint: Endpoint) {
    setSelectedId(endpoint.id)
    const next = new URLSearchParams(searchParams)
    next.set('endpoint', String(endpoint.id))
    setSearchParams(next, { replace: true })
  }

  function close() {
    setSelectedId(null)
    const next = new URLSearchParams(searchParams)
    next.delete('endpoint')
    setSearchParams(next, { replace: true })
  }

  return (
    <div className="stack">
      <div className="section-bar">
        <div className="section-bar__stats">
          <span><strong>{all.length}</strong> 个接口</span>
          <span className="text-success"><strong>{consistent}</strong> 一致</span>
          <span className="text-danger"><strong>{inconsistent}</strong> 不一致</span>
          <span className="muted"><strong>{single}</strong> 单一来源</span>
        </div>
        <div className="section-bar__actions">
          <Button size="sm" icon="sparkles" loading={busy} onClick={extract}>Agent 抽接口</Button>
          <Button size="sm" icon="refresh" loading={busy} onClick={check}>重新对照</Button>
        </div>
      </div>

      <Card flush>
        <div className="list-toolbar">
          <div className="list-search">
            <Icon name="search" size={14} />
            <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索路径、说明" />
          </div>
          <div className="list-filters">
            <select className="input input--sm" value={method} onChange={(event) => setMethod(event.target.value)}>
              <option value="">全部方法</option>
              {METHODS.map((item) => <option key={item} value={item}>{item}</option>)}
            </select>
            <select className="input input--sm" value={state} onChange={(event) => setState(event.target.value)}>
              <option value="">全部状态</option>
              {Object.entries(ENDPOINT_STATE).map(([key, item]) => (
                <option key={key} value={key}>{item.label}</option>
              ))}
            </select>
          </div>
        </div>

        {endpoints.error && <ErrorBox>{endpoints.error}</ErrorBox>}
        {endpoints.loading && !endpoints.data && <div className="pad"><Skeleton lines={8} /></div>}
        {!endpoints.loading && filtered.length === 0 && (
          <EmptyState
            icon="api"
            title={all.length ? '没有符合条件的接口' : '还没有接口'}
            description={all.length ? '换个筛选条件。' : '导入 OpenAPI / Swagger，或导入代码库自动抽路由。'}
          />
        )}

        {filtered.length > 0 && (
          <div className="endpoint-list">
            {filtered.map((endpoint) => (
              <button key={endpoint.id} className="endpoint-row" onClick={() => openEndpoint(endpoint)}>
                  <Pill tone={endpoint.method === 'GET' ? 'blue' : endpoint.method === 'DELETE' ? 'danger' : endpoint.method === 'POST' ? 'success' : 'warning'}>
                    {endpoint.method}
                  </Pill>
                  <span className="endpoint-row__path mono">{endpoint.path}</span>
                  <span className="endpoint-row__summary">{endpoint.summary || <span className="muted">—</span>}</span>
                  <span className="endpoint-row__sources">
                    {(endpoint.sources ?? []).map((source) => (
                      <Pill key={`${source.type}-${source.ref}`} tone="muted">{source.type}</Pill>
                    ))}
                  </span>
                  <Status value={endpoint.consistency?.state ?? 'unknown'} map={ENDPOINT_STATE} />
                  <Icon name="chevronRight" size={14} />
                </button>
            ))}
          </div>
        )}
      </Card>

      <Drawer
        open={!!selected}
        onClose={close}
        title={
          selected ? (
            <span className="drawer-title mono">
              <Pill tone={selected.method === 'GET' ? 'blue' : selected.method === 'DELETE' ? 'danger' : 'success'}>
                {selected.method}
              </Pill>
              {selected.path}
            </span>
          ) : ''
        }
        subtitle={selected?.summary}
        width={760}
        actions={selected ? <CopyButton text={`${selected.method} ${selected.path}`} label="复制接口" /> : undefined}
      >
        {selected && (
          <div className="stack">
            <section className="endpoint-meta">
              <div><span>一致性</span><Status value={selected.consistency?.state ?? 'unknown'} map={ENDPOINT_STATE} /></div>
              <div><span>来源</span><strong>{(selected.sources ?? []).map((item) => item.type).join(' / ') || '—'}</strong></div>
              <div><span>接口 ID</span><strong>#{selected.id}</strong></div>
            </section>

            {(selected.consistency?.mismatches ?? []).length > 0 && (
              <section className="result-block">
                <h3>三方差异</h3>
                <div className="mismatch-list">
                  {(selected.consistency?.mismatches ?? []).map((mismatch, index) => (
                    <div key={index} className="mismatch">
                      <Pill tone="danger">{mismatch.kind}</Pill>
                      <span>{mismatch.message}</span>
                    </div>
                  ))}
                </div>
              </section>
            )}

            <section className="result-block">
              <h3>参数</h3>
              <pre className="code">{safeJson(selected.params)}</pre>
            </section>
            <section className="result-block">
              <h3>请求体</h3>
              <pre className="code">{safeJson(selected.request_body)}</pre>
            </section>
            <section className="result-block">
              <h3>响应</h3>
              <pre className="code">{safeJson(selected.responses)}</pre>
            </section>
            <section className="result-block">
              <h3>鉴权</h3>
              <pre className="code">{safeJson(selected.security)}</pre>
            </section>
            {selected.examples && Object.keys(selected.examples).length > 0 && (
              <section className="result-block">
                <h3>示例</h3>
                <pre className="code">{safeJson(selected.examples)}</pre>
              </section>
            )}
            <section className="result-block">
              <h3>来源证据</h3>
              <div className="evidence-list">
                {(selected.sources ?? []).map((source, index) => (
                  <div key={index} className="evidence">
                    <Pill tone="blue">{source.type}</Pill>
                    <code>{source.ref ?? '—'}</code>
                    {source.summary && <p>{source.summary}</p>}
                  </div>
                ))}
              </div>
            </section>
          </div>
        )}
      </Drawer>
    </div>
  )
}
