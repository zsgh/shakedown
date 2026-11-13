import { useEffect, useMemo, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { api, apiUrl, artifactUrl } from '../lib/api'
import { duration, formatFullTime, percent, runDuration, safeJson } from '../lib/format'
import { useAsync } from '../lib/hooks'
import { ASSERTION_KIND, ATTRIBUTION, DIMENSION, RESULT_STATUS, RUN_STATUS, meta } from '../lib/labels'
import type { CaseResult, GateResult, Run } from '../lib/types'
import { Donut, Legend, type Segment } from '../ui/charts'
import { Icon } from '../ui/icons'
import {
  Button,
  Card,
  CopyButton,
  EmptyState,
  ErrorBox,
  KeyValue,
  PageHead,
  Pill,
  Skeleton,
  Status,
  cn,
} from '../ui/kit'
import { Drawer, useToast } from '../ui/overlays'

type ResultFilter = 'fail' | 'all' | 'pass' | 'flaky' | 'error'

const CATEGORY_OPTIONS = [
  { value: 'defect', label: '疑似真实缺陷' },
  { value: 'environment', label: '环境问题' },
  { value: 'platform', label: '平台能力缺口' },
  { value: 'case', label: '用例问题' },
  { value: 'evidence', label: '依据不足' },
  { value: 'flaky', label: '不稳定 / 偶发' },
  { value: 'unconfirmed', label: '归因待确认' },
]

export default function RunDetail() {
  const { projectId, runId } = useParams()
  const navigate = useNavigate()
  const toast = useToast()
  const [filter, setFilter] = useState<ResultFilter>('fail')
  const [selected, setSelected] = useState<CaseResult | null>(null)
  const [busy, setBusy] = useState(false)

  const run = useAsync<Run>(() => api.get(`/runs/${runId}`), [runId], { refreshMs: 3000 })
  const results = useAsync<CaseResult[]>(() => api.get(`/runs/${runId}/results`), [runId], { refreshMs: 3000 })
  const gate = useAsync<GateResult | null>(
    () => (runId ? api.get<GateResult>(`/projects/${projectId}/automation/gate?run_id=${runId}`) : Promise.resolve(null)),
    [projectId, runId],
    { refreshMs: 5000 },
  )
  const projectRuns = useAsync<Run[]>(() => api.get(`/projects/${projectId}/runs`), [projectId])
  const isActive = !!run.data && ['queued', 'preparing', 'running', 'analyzing'].includes(run.data.status)
  const previousRun = projectRuns.data?.find((item) => String(item.id) !== String(runId))

  const primary = useMemo(() => (results.data ?? []).filter((item) => !item.rerun), [results.data])
  const counts = useMemo(() => {
    const map: Record<string, number> = {}
    primary.forEach((item) => {
      map[item.status] = (map[item.status] ?? 0) + 1
    })
    return map
  }, [primary])
  const visible = useMemo(
    () =>
      filter === 'all'
        ? primary
        : filter === 'fail'
          ? primary.filter((item) => item.status === 'fail' || item.status === 'error')
          : primary.filter((item) => item.status === filter),
    [filter, primary],
  )

  async function cancel() {
    if (!run.data) return
    setBusy(true)
    try {
      await api.post(`/runs/${run.data.id}/cancel`)
      toast.success('已请求取消')
      run.reload()
    } catch (error) {
      toast.error('取消失败', String(error))
    } finally {
      setBusy(false)
    }
  }

  async function retry() {
    if (!run.data) return
    setBusy(true)
    try {
      const next = await api.post<Run>(`/runs/${run.data.id}/retry-failed`)
      toast.success('已排队重跑失败用例', `运行 #${next.id}`)
      navigate(`/projects/${projectId}/runs/${next.id}`)
    } catch (error) {
      toast.error('重跑没有排上', String(error))
    } finally {
      setBusy(false)
    }
  }

  if (run.error) return <ErrorBox>{run.error}</ErrorBox>
  if (!run.data) {
    return (
      <div className="page">
        <Skeleton lines={6} />
      </div>
    )
  }

  const data = run.data
  const stats = data.stats ?? {}
  const rate = stats.pass_rate ?? 0
  const failed = (stats.failed ?? 0) + (stats.errored ?? 0)
  const segments: Segment[] = [
    { label: '通过', value: stats.passed ?? 0, tone: 'success' },
    { label: '失败', value: stats.failed ?? 0, tone: 'danger' },
    { label: '不稳定', value: stats.flaky ?? 0, tone: 'warning' },
    { label: '错误', value: stats.errored ?? 0, tone: 'violet' },
  ]
  const steps = [
    { key: 'queued', label: '排队' },
    { key: 'preparing', label: '准备环境' },
    { key: 'running', label: '执行' },
    { key: 'analyzing', label: '归因分析' },
    { key: 'completed', label: '完成' },
  ]
  const currentIndex = data.status === 'completed' ? steps.length : Math.max(0, steps.findIndex((step) => step.key === data.status))

  return (
    <div className="page">
      <PageHead
        eyebrow={
          <span className="crumb-inline">
            <Link to={`/projects/${projectId}/runs`}>执行</Link> / 运行 #{data.id}
          </span>
        }
        title={`运行 #${data.id}`}
        subtitle={
          <span className="crumb-inline">
            <Status value={data.status} map={RUN_STATUS} />
            <span className="muted">
              {data.trigger === 'retry' ? '重跑失败用例' : data.trigger === 'agent' ? 'Agent 触发' : data.trigger === 'validation' ? '生成后试运行' : '手动触发'} · {formatFullTime(data.created_at)}
            </span>
          </span>
        }
        actions={
          <>
            {isActive && (
              <Button icon="stop" loading={busy} onClick={cancel}>取消执行</Button>
            )}
            {previousRun && (
              <Button
                icon="layers"
                onClick={() =>
                  navigate(`/projects/${projectId}/runs/compare?base=${previousRun.id}&target=${data.id}`)
                }
              >
                对比上次
              </Button>
            )}
            {failed > 0 && !isActive && (
              <Button icon="refresh" loading={busy} onClick={retry}>重跑失败（{failed}）</Button>
            )}
            <a className="btn btn--secondary btn--md" href={apiUrl(`/runs/${data.id}/report`)} target="_blank" rel="noreferrer">
              <Icon name="download" size={15} /> 导出报告
            </a>
            <Button icon="chevronLeft" onClick={() => navigate(`/projects/${projectId}/runs`)}>返回</Button>
          </>
        }
      />

      {isActive && (
        <div className="notice notice--info notice--wide">
          <span className="spinner" />
          <div>
            <strong>执行正在进行</strong>
            <span>当前阶段：{meta(RUN_STATUS, data.status).label}。结果每 3 秒自动刷新。</span>
          </div>
        </div>
      )}

      {gate.data && (
        <Card title="CI gate" subtitle="本次 run 的合并阻断判定；SLO 只统计本次 run 窗口内的性能结果。">
          <div className="row-actions">
            <Pill tone={gate.data.passed ? 'success' : 'danger'} dot>{gate.data.status}</Pill>
            <span className="muted small">
              阻断 {gate.data.blockers.length} 项 · 警告 {gate.data.warnings.length} 项
            </span>
            <div className="spacer" />
            <Link className="btn btn--secondary btn--sm" to={`/projects/${projectId}/automation`}>
              <Icon name="zap" size={14} /> 自动化控制台
            </Link>
          </div>
          <div className="stack" style={{ marginTop: 8 }}>
            {gate.data.blockers.map((item, index) => (
              <div key={index} className="row-actions">
                <Pill tone="danger">{String(item.kind)}</Pill>
                <span className="small">{String(item.message)}</span>
              </div>
            ))}
            {!gate.data.blockers.length && <span className="muted small">没有阻断项，可以合并。</span>}
          </div>
        </Card>
      )}

      <div className="run-detail-grid">
        <Card className="run-detail-summary">
          <div className="run-detail-summary__top">
            <Donut segments={segments} centerLabel={percent(rate)} centerHint="通过率" size={140} />
            <Legend segments={segments} />
          </div>
          <div className="run-detail-kpis">
            <div><span>总数</span><strong>{stats.total ?? 0}</strong></div>
            <div><span>通过</span><strong className="text-success">{stats.passed ?? 0}</strong></div>
            <div><span>失败</span><strong className="text-danger">{stats.failed ?? 0}</strong></div>
            <div><span>不稳定</span><strong className="text-warning">{stats.flaky ?? 0}</strong></div>
            <div><span>耗时</span><strong>{runDuration(data)}</strong></div>
          </div>
        </Card>

        <Card title="执行阶段" className="run-detail-steps">
          <ol className="stepper">
            {steps.map((step, index) => {
              const state = data.status === 'cancelled' ? 'cancelled' : index < currentIndex ? 'done' : index === currentIndex ? 'active' : 'todo'
              const time =
                step.key === 'queued'
                  ? data.created_at
                  : step.key === 'running'
                    ? data.started_at
                    : step.key === 'completed'
                      ? data.finished_at
                      : null
              return (
                <li key={step.key} className={cn('stepper__item', `is-${state}`)}>
                  <span className="stepper__marker">
                    {state === 'done' ? <Icon name="check" size={13} /> : state === 'active' ? <span className="spinner spinner--dark" /> : index + 1}
                  </span>
                  <div>
                    <strong>{step.label}</strong>
                    <span>{time ? formatFullTime(time) : state === 'todo' ? '等待中' : '—'}</span>
                  </div>
                </li>
              )
            })}
          </ol>
          <div className="run-env">
            <KeyValue
              items={[
                { label: '被测地址', value: <span className="mono">{data.env_snapshot?.base_url || '—'}</span> },
                { label: '被测库服务', value: <span className="mono">{data.env_snapshot?.db_service || '—'}</span> },
                { label: '沙箱任务', value: <span className="mono">{data.berth_task_id || data.stats?.sandbox?.task_id || '—'}</span> },
                { label: '创建时间', value: formatFullTime(data.created_at) },
              ]}
            />
          </div>
          {data.stats?.sandbox?.error && (
            <div className="notice notice--warning">
              <Icon name="alert" size={15} />
              <span>{data.stats.sandbox.error}</span>
            </div>
          )}
        </Card>
      </div>

      <Card
        title="执行结果"
        subtitle={isActive ? '执行完成后会补齐全部结果。' : `共 ${primary.length} 条用例，失败 ${failed} 条`}
        actions={
          <div className="segmented segmented--sm">
            {([
              ['fail', '失败', (counts.fail ?? 0) + (counts.error ?? 0)],
              ['all', '全部', primary.length],
              ['pass', '通过', counts.pass ?? 0],
              ['flaky', '不稳定', counts.flaky ?? 0],
              ['error', '错误', counts.error ?? 0],
            ] as Array<[ResultFilter, string, number]>).map(([value, label, count]) => (
              <button
                key={value}
                className={cn('segmented__item', filter === value && 'is-active')}
                onClick={() => setFilter(value)}
              >
                {label}
                <span className="segmented__count">{count}</span>
              </button>
            ))}
          </div>
        }
        flush
      >
        {visible.length === 0 ? (
          <EmptyState
            icon={filter === 'fail' && !isActive ? 'check' : 'list'}
            title={isActive ? '执行进行中' : filter === 'fail' ? '没有失败用例' : '没有结果'}
            description={
              isActive
                ? '用例执行完成后结果会出现在这里，页面每 3 秒自动刷新。'
                : filter === 'fail'
                  ? '这次执行里没有 fail / error。'
                  : '换个状态看看。'
            }
          />
        ) : (
          <div className="result-list">
            {visible.map((result) => {
              const status = meta(RESULT_STATUS, result.status)
              const attribution = result.attribution ?? {}
              return (
                <div
                  key={result.id}
                  className="result-row"
                  role="button"
                  tabIndex={0}
                  onClick={() => setSelected(result)}
                  onKeyDown={(event) => {
                    if (event.key === 'Enter' || event.key === ' ') setSelected(result)
                  }}
                >
                  <span className={`result-row__dot tone-${status.tone}`} />
                  <span className="result-row__seq">{result.seq + 1}</span>
                  <span className="result-row__main">
                    <strong>{result.case_title ?? `用例 #${result.case_id}`}</strong>
                    <span className="result-row__meta">
                      <Link
                        className="small"
                        to={`/projects/${projectId}/cases/${result.case_id}`}
                        onClick={(event) => event.stopPropagation()}
                      >
                        用例 #{result.case_id}
                      </Link>
                      {result.dimension && <Pill tone={meta(DIMENSION, result.dimension).tone}>{meta(DIMENSION, result.dimension).label}</Pill>}
                      <Status value={result.status} map={RESULT_STATUS} />
                      {result.output?.flaky && <Pill tone="warning">重跑后不一致</Pill>}
                      {result.rerun && <Pill tone="muted">重跑记录</Pill>}
                      <span>{duration(result.duration_ms)}</span>
                    </span>
                  </span>
                  <span className="result-row__attribution">
                    {attribution.category ? (
                      <>
                        <Pill tone={meta(ATTRIBUTION, attribution.category).tone}>
                          {meta(ATTRIBUTION, attribution.category).label}
                        </Pill>
                        <span>{attribution.reason}</span>
                      </>
                    ) : (
                      <span className="muted">{result.status === 'pass' ? '—' : '待归因'}</span>
                    )}
                  </span>
                  <Icon name="chevronRight" size={14} />
                </div>
              )
            })}
          </div>
        )}
      </Card>

      <ResultDrawer
        result={selected}
        onClose={() => setSelected(null)}
        onAttributionSaved={() => {
          results.reload()
          toast.success('归因已更新')
        }}
      />
    </div>
  )
}

function readJsonPath(body: any, path?: string): any {
  if (!path || path === '$') return body
  const expression = path.replace(/^\$\.?/, '')
  const tokens: Array<string | number> = []
  for (const match of expression.matchAll(/([^.[\]]+)|\[(\d+)\]/g)) {
    tokens.push(match[1] ?? Number(match[2]))
  }
  let current = body
  for (const token of tokens) {
    if (current == null) return undefined
    if (typeof token === 'number') {
      if (!Array.isArray(current)) return undefined
      current = current[token]
    } else {
      if (typeof current !== 'object' || !(token in current)) return undefined
      current = current[token]
    }
  }
  return current
}

function assertionActual(assertion: any, response: Record<string, any>): any {
  if (assertion.actual !== undefined) return assertion.actual
  // 兼容旧执行记录（当时还没把 actual 写进 output.assertions）
  if (assertion.kind === 'status_code') return response.status_code ?? '—'
  if (assertion.kind === 'json_path') {
    const value = readJsonPath(response.body, assertion.target)
    return value === undefined ? '—' : value
  }
  if (assertion.kind === 'text_contains') return response.body ?? '—'
  return '—'
}

function ResultDrawer({
  result,
  onClose,
  onAttributionSaved,
}: {
  result: CaseResult | null
  onClose: () => void
  onAttributionSaved: () => void
}) {
  const toast = useToast()
  const [tab, setTab] = useState<'overview' | 'request' | 'response' | 'assertions' | 'logs'>('overview')
  const [category, setCategory] = useState('')
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)

  const request = result?.request ?? {}
  const response = result?.response ?? {}
  const output = result?.output ?? {}
  const assertions: any[] = output.assertions ?? []
  const attribution = result?.attribution ?? {}

  useEffect(() => {
    if (!result) return
    setCategory(result.attribution?.category ?? 'defect')
    setReason(result.attribution?.reason ?? '')
    setTab('overview')
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [result?.id])

  async function saveAttribution() {
    if (!result) return
    setBusy(true)
    try {
      await api.post(`/results/${result.id}/attribution`, { category, reason })
      onAttributionSaved()
    } catch (error) {
      toast.error('保存归因失败', String(error))
    } finally {
      setBusy(false)
    }
  }

  const curl = result ? buildCurl(request) : ''

  return (
    <Drawer
      open={!!result}
      onClose={onClose}
      width={760}
      title={
        <span className="drawer-title">
          {result && <Status value={result.status} map={RESULT_STATUS} />}
          {result?.case_title ?? '执行结果'}
        </span>
      }
      subtitle={result ? `#${result.seq + 1} · ${duration(result.duration_ms)} · ${result.rerun ? '重跑记录' : '首次执行'}` : ''}
      actions={result ? <CopyButton text={curl} label="复制 curl" /> : undefined}
    >
      {result && (
        <div className="result-detail">
          <div className="tabs tabs--inner">
            {([
              ['overview', '概览'],
              ['request', '请求'],
              ['response', '响应'],
              ['assertions', `断言 ${assertions.length}`],
              ['logs', '日志'],
            ] as Array<[typeof tab, string]>).map(([value, label]) => (
              <button key={value} className={cn('tabs__item', tab === value && 'is-active')} onClick={() => setTab(value)}>
                {label}
              </button>
            ))}
          </div>

          {tab === 'overview' && (
            <div className="stack">
              {typeof output.failed_step_index === 'number' && (
                <div className="crumb-inline">
                  <Pill tone="danger">失败步骤 {output.failed_step_index + 1}</Pill>
                  <span className="muted small">顶层请求 / 响应已定位到该步骤，cleanup 不会覆盖</span>
                </div>
              )}
              <section className="result-block">
                <h3>归因</h3>
                <div className="attribution-editor">
                  <select className="input" value={category} onChange={(event) => setCategory(event.target.value)}>
                    {CATEGORY_OPTIONS.map((option) => (
                      <option key={option.value} value={option.value}>{option.label}</option>
                    ))}
                  </select>
                  <textarea
                    className="input"
                    rows={3}
                    value={reason}
                    onChange={(event) => setReason(event.target.value)}
                    placeholder="为什么这样归因？给出可核对的理由。"
                  />
                  <div className="attribution-editor__foot">
                    <span className="muted small">
                      {attribution.manual
                        ? '已人工修正'
                        : attribution.confidence
                          ? `${attribution.source === 'rules' ? '规则' : '模型'}置信度 ${Math.round(attribution.confidence * 100)}%`
                          : '等待归因'}
                    </span>
                    <Button size="sm" variant="primary" loading={busy} onClick={saveAttribution}>保存归因</Button>
                  </div>
                </div>
              </section>

              {output.errors?.length > 0 && (
                <section className="result-block">
                  <h3>失败原因</h3>
                  <div className="error-list">
                    {output.errors.map((error: string, index: number) => (
                      <div key={index} className="error-list__item">
                        <Icon name="alert" size={13} />
                        {error}
                      </div>
                    ))}
                  </div>
                </section>
              )}

              <section className="result-block">
                <h3>请求</h3>
                <div className="request-line">
                  <Pill tone={request.method === 'GET' ? 'blue' : request.method === 'DELETE' ? 'danger' : 'success'}>
                    {request.method ?? 'GET'}
                  </Pill>
                  <code>{request.url ?? '—'}</code>
                </div>
                {request.body && <pre className="code">{safeJson(request.body)}</pre>}
                <div className="result-block__actions">
                  <CopyButton text={curl} label="复制 curl" />
                </div>
              </section>

              <section className="result-block">
                <h3>响应</h3>
                <div className="request-line">
                  <Pill tone={response.status_code && response.status_code < 400 ? 'success' : 'danger'}>
                    HTTP {response.status_code ?? '—'}
                  </Pill>
                  <span className="muted small">{duration(response.duration_ms)}</span>
                </div>
                <pre className="code code--scroll">{safeJson(response.body)}</pre>
              </section>
            </div>
          )}

          {tab === 'request' && (
            <div className="stack">
              <section className="result-block">
                <h3>请求行</h3>
                <pre className="code">{`${request.method ?? 'GET'} ${request.url ?? ''}`}</pre>
              </section>
              <section className="result-block">
                <h3>Headers</h3>
                <pre className="code">{safeJson(request.headers ?? {})}</pre>
              </section>
              <section className="result-block">
                <h3>Query</h3>
                <pre className="code">{safeJson(request.query ?? {})}</pre>
              </section>
              <section className="result-block">
                <h3>Body</h3>
                <pre className="code">{safeJson(request.body)}</pre>
              </section>
            </div>
          )}

          {tab === 'response' && (
            <div className="stack">
              <section className="result-block">
                <h3>状态</h3>
                <pre className="code">HTTP {response.status_code ?? '—'} · {duration(response.duration_ms)}</pre>
              </section>
              <section className="result-block">
                <h3>Headers</h3>
                <pre className="code">{safeJson(response.headers ?? {})}</pre>
              </section>
              <section className="result-block">
                <h3>Body</h3>
                <pre className="code code--scroll">{safeJson(response.body)}</pre>
              </section>
              {output.a11y && (
                <section className="result-block">
                  <h3>可访问性指标</h3>
                  <pre className="code code--scroll">{safeJson(output.a11y)}</pre>
                </section>
              )}
              {output.screenshot_sha256 && (
                <section className="result-block">
                  <h3>截图 SHA-256</h3>
                  <code>{String(output.screenshot_sha256)}</code>
                </section>
              )}
              {result.screenshot_path && (
                <section className="result-block">
                  <h3>截图</h3>
                  <img className="screenshot" src={artifactUrl(result.screenshot_path)} alt="执行截图" />
                </section>
              )}
            </div>
          )}

          {tab === 'assertions' && (
            <div className="stack">
              {assertions.length === 0 && <div className="muted">没有断言记录。</div>}
              {assertions.map((assertion, index) => (
                <section key={index} className={cn('assertion', assertion.pending ? 'is-pending' : assertion.passed ? 'is-confirmed' : 'is-failed')}>
                  <div className="assertion__top">
                    {typeof assertion.step_index === 'number' && (
                      <Pill tone="blue">步骤 {assertion.step_index + 1}</Pill>
                    )}
                    {assertion.step_method && (
                      <code className="muted">{assertion.step_method} {assertion.step_path}</code>
                    )}
                    <Pill tone={assertion.pending ? 'warning' : assertion.passed ? 'success' : 'danger'}>
                      {assertion.pending ? '待确认' : assertion.passed ? '通过' : '失败'}
                    </Pill>
                    <code title={assertion.kind}>{ASSERTION_KIND[assertion.kind] ?? assertion.kind}</code>
                    {assertion.target && <code className="muted">{assertion.target}</code>}
                  </div>
                  {assertion.description && <p className="assertion__desc">{assertion.description}</p>}
                  <p>{assertion.detail}</p>
                  <div className="assertion__grid">
                    <div><span>期望</span><code>{safeJson(assertion.expected)}</code></div>
                    <div>
                      <span>实际</span>
                      <code>{safeJson(assertionActual(assertion, response))}</code>
                    </div>
                    <div><span>依据</span><code>{assertion.evidence?.ref ?? '—'}</code></div>
                  </div>
                </section>
              ))}
            </div>
          )}

          {tab === 'logs' && (
            <pre className="code code--log">{result.logs || (result.status === 'pass' ? '用例通过，没有错误日志。' : '没有日志。')}</pre>
          )}
        </div>
      )}
    </Drawer>
  )
}

function buildCurl(request: Record<string, any>): string {
  if (!request?.url) return ''
  const parts = ['curl', '-i', '-X', request.method ?? 'GET']
  Object.entries(request.headers ?? {}).forEach(([key, value]) => {
    parts.push('-H', `'${key}: ${String(value)}'`)
  })
  Object.entries(request.query ?? {}).forEach(([key, value]) => {
    parts.push('--data-urlencode', `'${key}=${String(value)}'`)
  })
  if (request.body) {
    parts.push('-d', `'${JSON.stringify(request.body).replace(/'/g, "'\\''")}'`)
  }
  parts.push(`'${request.url}'`)
  return parts.join(' ')
}
