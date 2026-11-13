import { useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { api, apiUrl } from '../lib/api'
import { fromNow, percent, runDuration } from '../lib/format'
import { useAsync } from '../lib/hooks'
import { RUN_STATUS } from '../lib/labels'
import type { Run } from '../lib/types'
import { Icon } from '../ui/icons'
import { Button, Card, EmptyState, ErrorBox, PageHead, Pill, Progress, Skeleton, Status, cn } from '../ui/kit'
import { useToast } from '../ui/overlays'

const DIMENSIONS = [
  { value: '', label: '全部维度' },
  { value: 'functional', label: '功能' },
  { value: 'security', label: '安全' },
  { value: 'boundary', label: '边界' },
  { value: 'performance', label: '性能' },
]

export default function Runs() {
  const { projectId } = useParams()
  const navigate = useNavigate()
  const toast = useToast()
  const [dimension, setDimension] = useState('')
  const [busy, setBusy] = useState(false)
  const { data, error, loading, reload } = useAsync<Run[]>(
    () => api.get(`/projects/${projectId}/runs`),
    [projectId],
    { refreshMs: 4000 },
  )
  const runs = data ?? []
  const active = runs.some((run) => ['queued', 'preparing', 'running', 'analyzing'].includes(run.status))

  async function trigger() {
    setBusy(true)
    try {
      const run = await api.post<Run>(`/projects/${projectId}/runs`, {
        dimensions: dimension ? [dimension] : null,
      })
      toast.success('已排队执行', `运行 #${run.id}`)
      navigate(`/projects/${projectId}/runs/${run.id}`)
    } catch (error) {
      toast.error('执行没有排上', String(error))
    } finally {
      setBusy(false)
    }
  }

  async function cancel(run: Run) {
    setBusy(true)
    try {
      await api.post(`/runs/${run.id}/cancel`)
      toast.success(`运行 #${run.id} 已取消`)
      reload()
    } catch (error) {
      toast.error('取消失败', String(error))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="page">
      <PageHead
        title="执行"
        subtitle="跑已启用的用例，失败自动重跑一次；两次不一致标为不稳定，不报缺陷。"
        actions={
          <>
            {runs.length > 1 && (
              <Link
                className="btn btn--secondary btn--md"
                to={`/projects/${projectId}/runs/compare?base=${runs[1].id}&target=${runs[0].id}`}
              >
                <Icon name="layers" size={15} /> 对比最近两次
              </Link>
            )}
            <select className="input" value={dimension} onChange={(event) => setDimension(event.target.value)}>
              {DIMENSIONS.map((item) => (
                <option key={item.value} value={item.value}>{item.label}</option>
              ))}
            </select>
            <Button variant="primary" icon="play" loading={busy} onClick={trigger}>
              执行已启用用例
            </Button>
          </>
        }
      />

      {error && <ErrorBox>{error}</ErrorBox>}

      <div className="run-overview">
        <Card>
          <div className="run-overview__row">
            <div>
              <span className="run-overview__label">执行总数</span>
              <strong>{runs.length}</strong>
            </div>
            <div>
              <span className="run-overview__label">运行中</span>
              <strong>{runs.filter((run) => ['queued', 'preparing', 'running', 'analyzing'].includes(run.status)).length}</strong>
            </div>
            <div>
              <span className="run-overview__label">最近通过率</span>
              <strong>{runs[0] ? percent(runs[0].stats?.pass_rate) : '—'}</strong>
            </div>
            <div>
              <span className="run-overview__label">最近耗时</span>
              <strong>{runs[0] ? runDuration(runs[0]) : '—'}</strong>
            </div>
            <div className="spacer" />
            {active && (
              <span className="live-indicator">
                <span className="spinner" /> 有执行进行中，自动刷新
              </span>
            )}
          </div>
        </Card>
      </div>

      {loading && !data && <Card><Skeleton lines={6} /></Card>}

      {!loading && runs.length === 0 && (
        <Card>
          <EmptyState
            icon="play"
            title="还没有执行记录"
            description="审核并启用用例后，点右上角执行。执行在沙箱里跑，失败会自动重跑一次。"
            action={
              <Button onClick={() => navigate(`/projects/${projectId}/cases`)}>去审核用例</Button>
            }
          />
        </Card>
      )}

      {runs.length > 0 && (
        <div className="runs">
          {runs.map((run) => {
            const stats = run.stats ?? {}
            const rate = stats.pass_rate ?? 0
            const isActive = ['queued', 'preparing', 'running', 'analyzing'].includes(run.status)
            return (
              <article key={run.id} className={cn('run-card', isActive && 'is-live')}>
                <Link to={`/projects/${projectId}/runs/${run.id}`} className="run-card__main">
                  <div className="run-card__head">
                    <span className="run-card__id">#{run.id}</span>
                    <Status value={run.status} map={RUN_STATUS} />
                    {run.trigger === 'retry' && <Pill tone="violet">重跑失败</Pill>}
                    {run.trigger === 'validation' && <Pill tone="info">试运行</Pill>}
                    {isActive && <span className="spinner" />}
                    <span className="run-card__time">{fromNow(run.created_at)}</span>
                  </div>
                  <div className="run-card__stats">
                    <div>
                      <span>通过率</span>
                      <strong>{percent(rate)}</strong>
                    </div>
                    <div><span>总数</span><strong>{stats.total ?? 0}</strong></div>
                    <div><span>通过</span><strong className="text-success">{stats.passed ?? 0}</strong></div>
                    <div><span>失败</span><strong className="text-danger">{stats.failed ?? 0}</strong></div>
                    <div><span>不稳定</span><strong className="text-warning">{stats.flaky ?? 0}</strong></div>
                    <div><span>耗时</span><strong>{runDuration(run)}</strong></div>
                  </div>
                  <div className="run-card__bar">
                    <Progress value={rate > 1 ? rate / 100 : rate} tone={rate >= 0.9 ? 'success' : rate >= 0.7 ? 'warning' : 'danger'} />
                  </div>
                </Link>
                <div className="run-card__actions">
                  {isActive && (
                    <Button size="sm" icon="stop" onClick={() => cancel(run)} disabled={busy}>取消</Button>
                  )}
                  <a className="btn btn--secondary btn--sm" href={apiUrl(`/runs/${run.id}/report`)} target="_blank" rel="noreferrer">
                    <Icon name="download" size={14} /> 报告
                  </a>
                  <Link className="btn btn--ghost btn--sm" to={`/projects/${projectId}/runs/${run.id}`}>
                    详情 <Icon name="arrowRight" size={13} />
                  </Link>
                </div>
              </article>
            )
          })}
        </div>
      )}
    </div>
  )
}
