import { useEffect, useMemo, useState } from 'react'
import { Link, useParams, useSearchParams } from 'react-router-dom'
import { api } from '../lib/api'
import { formatFullTime, percent, runDuration } from '../lib/format'
import { useAsync } from '../lib/hooks'
import { DIMENSION, RESULT_STATUS, RUN_CHANGE, RUN_STATUS, meta } from '../lib/labels'
import type { Run, RunChange, RunCompare } from '../lib/types'
import { Icon } from '../ui/icons'
import {
  Button,
  Card,
  EmptyState,
  ErrorBox,
  PageHead,
  Pill,
  Skeleton,
  Status,
  cn,
} from '../ui/kit'
import { useToast } from '../ui/overlays'

const FILTERS: Array<{ value: RunChange | 'all'; label: string }> = [
  { value: 'all', label: '全部差异' },
  { value: 'broken', label: '新失败' },
  { value: 'still_failing', label: '持续失败' },
  { value: 'fixed', label: '已修复' },
  { value: 'new', label: '新增' },
  { value: 'removed', label: '移除' },
  { value: 'changed', label: '状态变化' },
]

export default function RunCompare() {
  const { projectId } = useParams()
  const [searchParams, setSearchParams] = useSearchParams()
  const toast = useToast()
  const baseId = Number(searchParams.get('base') ?? 0)
  const targetId = Number(searchParams.get('target') ?? 0)
  const [filter, setFilter] = useState<RunChange | 'all'>('all')

  const runs = useAsync<Run[]>(() => api.get(`/projects/${projectId}/runs`), [projectId])
  const compare = useAsync<RunCompare | null>(
    () =>
      baseId && targetId
        ? api.get<RunCompare>(`/projects/${projectId}/runs/compare?base=${baseId}&target=${targetId}`)
        : Promise.resolve(null),
    [projectId, baseId, targetId],
  )

  const list = runs.data ?? []
  useEffect(() => {
    if (list.length < 2) return
    if (!baseId || !targetId) {
      const next = new URLSearchParams(searchParams)
      next.set('base', String(list[1].id))
      next.set('target', String(list[0].id))
      setSearchParams(next, { replace: true })
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [list.length, baseId, targetId])

  const data = compare.data
  useEffect(() => {
    if (!data) return
    if (data.summary.broken) setFilter('broken')
    else if (data.summary.still_failing) setFilter('still_failing')
    else if (data.summary.fixed) setFilter('fixed')
    else setFilter('all')
  }, [data?.base.id, data?.target.id])

  const visible = useMemo(
    () => (data ? (filter === 'all' ? data.cases : data.cases.filter((item) => item.change === filter)) : []),
    [data, filter],
  )

  function pick(base: number, target: number) {
    const next = new URLSearchParams(searchParams)
    next.set('base', String(base))
    next.set('target', String(target))
    setSearchParams(next)
  }

  if (compare.error) return <ErrorBox>{compare.error}</ErrorBox>

  if (list.length < 2) {
    return (
      <div className="page">
        <PageHead title="运行对比" subtitle="至少需要两次执行才能对比。" />
        <Card>
          <EmptyState
            icon="play"
            title="还没有可对比的运行"
            description="同一个项目执行两次后，可以在这里看到新失败、已修复和持续失败的用例。"
            action={<Link className="btn btn--primary btn--md" to={`/projects/${projectId}/runs`}>去执行</Link>}
          />
        </Card>
      </div>
    )
  }

  return (
    <div className="page">
      <PageHead
        eyebrow={
          <span className="crumb-inline">
            <Link to={`/projects/${projectId}/runs`}>执行</Link> / 运行对比
          </span>
        }
        title="运行对比"
        subtitle="看这次执行相对基线：哪些新失败、哪些修好了、哪些一直坏着。"
        actions={
          <Button icon="chevronLeft" onClick={() => window.history.back()}>
            返回
          </Button>
        }
      />

      <Card className="compare-picker">
        <div className="compare-picker__row">
          <label>
            <span>基线运行</span>
            <select
              className="input"
              value={baseId || ''}
              onChange={(event) => pick(Number(event.target.value), targetId)}
            >
              {list.map((run) => (
                <option key={run.id} value={run.id}>
                  #{run.id} · {meta(RUN_STATUS, run.status).label} · {percent(run.stats?.pass_rate)} · {formatFullTime(run.created_at)}
                </option>
              ))}
            </select>
          </label>
          <span className="compare-picker__arrow"><Icon name="arrowRight" size={18} /></span>
          <label>
            <span>目标运行</span>
            <select
              className="input"
              value={targetId || ''}
              onChange={(event) => pick(baseId, Number(event.target.value))}
            >
              {list.map((run) => (
                <option key={run.id} value={run.id}>
                  #{run.id} · {meta(RUN_STATUS, run.status).label} · {percent(run.stats?.pass_rate)} · {formatFullTime(run.created_at)}
                </option>
              ))}
            </select>
          </label>
          <div className="spacer" />
          <Button
            icon="refresh"
            onClick={() => {
              const latest = list[0]
              const previous = list.find((run) => run.id !== latest.id)
              if (previous) {
                pick(previous.id, latest.id)
                toast.success('已选择最近两次运行')
              }
            }}
          >
            对比最近两次
          </Button>
        </div>
      </Card>

      {!data && <Card><Skeleton lines={8} /></Card>}

      {data && (
        <>
          <div className="compare-summary">
            <Card className="compare-run">
              <div className="compare-run__head">
                <Link to={`/projects/${projectId}/runs/${data.base.id}`}>#{data.base.id}</Link>
                <Status value={data.base.status} map={RUN_STATUS} />
                <span className="muted small">基线</span>
              </div>
              <strong className="compare-run__rate">{percent(data.base.pass_rate)}</strong>
              <div className="compare-run__stats">
                <span>通过 {data.base.passed}</span>
                <span>失败 {data.base.failed}</span>
                <span>{data.base.total} 条</span>
                <span>{runDuration(data.base)}</span>
              </div>
            </Card>

            <div className="compare-delta">
              <span className={cn('compare-delta__value', data.delta.pass_rate > 0 ? 'text-success' : data.delta.pass_rate < 0 ? 'text-danger' : 'muted')}>
                {data.delta.pass_rate > 0 ? '+' : ''}{percent(Math.abs(data.delta.pass_rate))}
              </span>
              <span className="muted small">通过率变化</span>
            </div>

            <Card className="compare-run">
              <div className="compare-run__head">
                <Link to={`/projects/${projectId}/runs/${data.target.id}`}>#{data.target.id}</Link>
                <Status value={data.target.status} map={RUN_STATUS} />
                <span className="muted small">目标</span>
              </div>
              <strong className="compare-run__rate">{percent(data.target.pass_rate)}</strong>
              <div className="compare-run__stats">
                <span>通过 {data.target.passed}</span>
                <span>失败 {data.target.failed}</span>
                <span>{data.target.total} 条</span>
                <span>{runDuration(data.target)}</span>
              </div>
            </Card>
          </div>

          <div className="compare-badges">
            <Pill tone="danger" dot>{data.summary.broken ?? 0} 新失败</Pill>
            <Pill tone="warning" dot>{data.summary.still_failing ?? 0} 持续失败</Pill>
            <Pill tone="success" dot>{data.summary.fixed ?? 0} 已修复</Pill>
            <Pill tone="info" dot>{data.summary.new ?? 0} 新增</Pill>
            <Pill tone="muted" dot>{data.summary.removed ?? 0} 移除</Pill>
          </div>

          <Card
            title="用例级差异"
            subtitle={`共 ${data.cases.length} 条对比结果`}
            actions={
              <div className="segmented segmented--sm">
                {FILTERS.map((item) => {
                  const count = item.value === 'all' ? data.cases.length : data.summary[item.value] ?? 0
                  return (
                    <button
                      key={item.value}
                      className={cn('segmented__item', filter === item.value && 'is-active')}
                      onClick={() => setFilter(item.value)}
                    >
                      {item.label}
                      <span className="segmented__count">{count}</span>
                    </button>
                  )
                })}
              </div>
            }
            flush
          >
            {visible.length === 0 ? (
              <EmptyState
                icon="check"
                title={filter === 'all' ? '两次运行完全一致' : '没有这一类差异'}
                description={filter === 'all' ? '所有用例的状态都没有变化。' : '换个筛选条件看看。'}
              />
            ) : (
              <div className="compare-list">
                {visible.map((item) => {
                  const change = RUN_CHANGE[item.change] ?? RUN_CHANGE.changed
                  return (
                    <Link
                      key={item.case_id}
                      className={cn('compare-row', `is-${change.tone}`)}
                      to={`/projects/${projectId}/cases/${item.case_id}`}
                    >
                      <span className="compare-row__change">
                        <Pill tone={change.tone}>{change.label}</Pill>
                      </span>
                      <span className="compare-row__main">
                        <strong>{item.title}</strong>
                        <span className="compare-row__meta">
                          {item.dimension && <Pill tone={meta(DIMENSION, item.dimension).tone}>{meta(DIMENSION, item.dimension).label}</Pill>}
                          <span>#{item.case_id}</span>
                          <span className="muted">{change.hint}</span>
                        </span>
                      </span>
                      <span className="compare-row__states">
                        {item.base_status ? <Status value={item.base_status} map={RESULT_STATUS} /> : <Pill tone="muted">未执行</Pill>}
                        <Icon name="arrowRight" size={13} />
                        {item.target_status ? <Status value={item.target_status} map={RESULT_STATUS} /> : <Pill tone="muted">未执行</Pill>}
                      </span>
                    </Link>
                  )
                })}
              </div>
            )}
          </Card>
        </>
      )}
    </div>
  )
}
