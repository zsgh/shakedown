import { useMemo, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { api } from '../lib/api'
import { duration, formatFullTime, fromNow, safeJson } from '../lib/format'
import { useAsync } from '../lib/hooks'
import { TASK_KIND, TASK_STATUS } from '../lib/labels'
import type { AgentTrace, Task } from '../lib/types'
import { Icon } from '../ui/icons'
import { Button, Card, EmptyState, ErrorBox, PageHead, Pill, Skeleton, Status, cn } from '../ui/kit'
import { useToast } from '../ui/overlays'

type Tab = 'tasks' | 'traces'

export default function Activity() {
  const { projectId } = useParams()
  const toast = useToast()
  const [tab, setTab] = useState<Tab>('tasks')
  const [expanded, setExpanded] = useState<number | null>(null)
  const [traceTaskId, setTraceTaskId] = useState<number | null>(null)
  const tasks = useAsync<Task[]>(() => api.get(`/projects/${projectId}/tasks?limit=100`), [projectId], {
    refreshMs: 3000,
  })
  const traces = useAsync<AgentTrace[]>(
    () =>
      api.get(
        `/projects/${projectId}/traces?limit=100${traceTaskId ? `&task_id=${traceTaskId}` : ''}`,
      ),
    [projectId, traceTaskId],
    { refreshMs: 5_000 },
  )

  const list = tasks.data ?? []
  const active = list.filter((task) => task.status === 'queued' || task.status === 'running')
  const failed = list.filter((task) => task.status === 'failed')
  const succeeded = list.filter((task) => task.status === 'succeeded')
  const totalTokens = (traces.data ?? []).reduce((sum, trace) => sum + (trace.input_tokens ?? 0) + (trace.output_tokens ?? 0), 0)

  const sorted = useMemo(
    () => [...list].sort((a, b) => Number(new Date(b.created_at)) - Number(new Date(a.created_at))),
    [list],
  )

  async function cancelTask(taskId: number) {
    try {
      await api.post(`/tasks/${taskId}/cancel`)
      toast.info('已请求停止任务', '已完成的结果会保留；worker 会在模块 / 批次边界退出')
      tasks.reload()
    } catch (error) {
      toast.error('停止失败', String(error))
    }
  }

  async function retryTask(taskId: number) {
    try {
      const created = await api.post<{ id: number }>(`/tasks/${taskId}/retry`)
      toast.success('已重新排队', `新任务 #${created.id}`)
      tasks.reload()
    } catch (error) {
      toast.error('重试失败', String(error))
    }
  }

  return (
    <div className="page">
      <PageHead
        title="Agent 活动"
        subtitle="所有后台任务的排队、执行、结果，以及 Agent 每一步的输入输出。"
        actions={
          <>
            <Button icon="refresh" onClick={() => { tasks.reload(); traces.reload() }}>刷新</Button>
            {tab === 'tasks' ? (
              <Button icon="activity" onClick={() => setTab('traces')}>查看轨迹</Button>
            ) : (
              <Button icon="list" onClick={() => setTab('tasks')}>查看任务</Button>
            )}
          </>
        }
      />

      <div className="activity-summary">
        <div><span>运行中</span><strong className="text-info">{active.length}</strong></div>
        <div><span>成功</span><strong className="text-success">{succeeded.length}</strong></div>
        <div><span>失败</span><strong className="text-danger">{failed.length}</strong></div>
        <div><span>轨迹条数</span><strong>{traces.data?.length ?? 0}</strong></div>
        <div><span>Token 用量</span><strong>{totalTokens.toLocaleString()}</strong></div>
      </div>

      <div className="page-tabs page-tabs--simple">
        <button className={cn('page-tabs__item', tab === 'tasks' && 'is-active')} onClick={() => setTab('tasks')}>
          <Icon name="list" size={15} /> 任务
          <span className="tab-count">{list.length}</span>
        </button>
        <button className={cn('page-tabs__item', tab === 'traces' && 'is-active')} onClick={() => setTab('traces')}>
          <Icon name="activity" size={15} /> 轨迹
          <span className="tab-count">{traces.data?.length ?? 0}</span>
        </button>
      </div>

      {tab === 'tasks' && (
        <Card flush>
          {tasks.error && <ErrorBox>{tasks.error}</ErrorBox>}
          {tasks.loading && !tasks.data && <div className="pad"><Skeleton lines={8} /></div>}
          {!tasks.loading && sorted.length === 0 && (
            <EmptyState icon="activity" title="还没有后台任务" description="抽取功能点、生成用例、执行、白盒走查都会出现在这里。" />
          )}
          <div className="task-list">
            {sorted.map((task) => {
              const activeTask = task.status === 'queued' || task.status === 'running'
              const open = expanded === task.id
              return (
                <div key={task.id} className={cn('task-row', activeTask && 'is-live')}>
                  <button className="task-row__head" onClick={() => setExpanded(open ? null : task.id)}>
                    <span className={cn('task-row__icon', activeTask && 'is-live', task.status === 'cancelled' && 'is-cancelled')}>
                      {activeTask ? (
                        <span className="spinner" />
                      ) : (
                        <Icon name={task.status === 'failed' ? 'alert' : task.status === 'cancelled' ? 'stop' : 'check'} size={14} />
                      )}
                    </span>
                    <span className="task-row__kind">{TASK_KIND[task.kind] ?? task.kind}</span>
                    <Status value={task.status} map={TASK_STATUS} />
                    <span className="task-row__meta">
                      #{task.id} · 第 {task.attempts} 次
                      {task.started_at && task.finished_at
                        ? ` · ${duration(new Date(task.finished_at).getTime() - new Date(task.started_at).getTime())}`
                        : activeTask
                          ? ' · 进行中'
                          : ''}
                    </span>
                    {task.checkpoint?.whitebox && (
                      <span className="task-row__progress">
                        白盒 {task.checkpoint.whitebox.done}/{task.checkpoint.whitebox.total}
                        {task.checkpoint.whitebox.module ? ` · ${task.checkpoint.whitebox.module}` : ''}
                      </span>
                    )}
                    {task.checkpoint?.triage && (
                      <span className="task-row__progress">
                        分诊 {task.checkpoint.triage.done}/{task.checkpoint.triage.total}
                        {task.checkpoint.triage.module ? ` · ${task.checkpoint.triage.module}` : ''}
                      </span>
                    )}
                    {task.checkpoint?.generate && (
                      <span className="task-row__progress">
                        生成 {task.checkpoint.generate.done_count}/{task.checkpoint.generate.total}
                        {task.checkpoint.generate.module ? ` · ${task.checkpoint.generate.module}` : ''}
                        {task.checkpoint.generate.failed_count ? ` · 失败 ${task.checkpoint.generate.failed_count}` : ''}
                      </span>
                    )}
                    <span className="task-row__time">{fromNow(task.created_at)}</span>
                    <Icon name={open ? 'chevronDown' : 'chevronRight'} size={14} />
                  </button>
                  {open && (
                    <div className="task-row__detail">
                      <div className="task-detail-grid">
                        <div><span>创建</span><strong>{formatFullTime(task.created_at)}</strong></div>
                        <div><span>开始</span><strong>{formatFullTime(task.started_at)}</strong></div>
                        <div><span>结束</span><strong>{formatFullTime(task.finished_at)}</strong></div>
                        <div><span>尝试次数</span><strong>{task.attempts}</strong></div>
                      </div>
                      <section>
                        <Button
                          size="sm"
                          icon="activity"
                          onClick={() => {
                            setTraceTaskId(task.id)
                            setTab('traces')
                          }}
                        >
                          查看本次输出
                        </Button>
                      </section>
                      {activeTask && (
                        <section>
                          <Button size="sm" variant="danger" icon="stop" onClick={() => cancelTask(task.id)}>
                            停止任务
                          </Button>
                        </section>
                      )}
                      {(task.status === 'failed' || task.status === 'cancelled') && (
                        <section>
                          <Button size="sm" icon="refresh" onClick={() => retryTask(task.id)}>
                            重试任务
                          </Button>
                        </section>
                      )}
                      {task.error && (
                        <div className="notice notice--danger"><Icon name="alert" size={15} /><span>{task.error}</span></div>
                      )}
                      {Object.keys(task.result ?? {}).length > 0 && (
                        <section>
                          <h4>结果</h4>
                          <pre className="code">{safeJson(task.result)}</pre>
                        </section>
                      )}
                      {Object.keys(task.payload ?? {}).length > 0 && (
                        <section>
                          <h4>参数</h4>
                          <pre className="code">{safeJson(task.payload)}</pre>
                        </section>
                      )}
                      {task.run_id && (
                        <Link className="link-btn" to={`/projects/${projectId}/runs/${task.run_id}`}>
                          查看关联执行 #{task.run_id} <Icon name="arrowRight" size={12} />
                        </Link>
                      )}
                    </div>
                  )}
                </div>
              )
            })}
          </div>
        </Card>
      )}

      {tab === 'traces' && (
        <Card flush>
          {traceTaskId && (
            <div className="list-toolbar">
              <span className="muted small">只看任务 #{traceTaskId} 的模型调用输出</span>
              <div className="spacer" />
              <Button size="sm" onClick={() => setTraceTaskId(null)}>查看全部轨迹</Button>
            </div>
          )}
          {traces.error && <ErrorBox>{traces.error}</ErrorBox>}
          {traces.loading && !traces.data && <div className="pad"><Skeleton lines={8} /></div>}
          {!traces.loading && (traces.data ?? []).length === 0 && (
            <EmptyState icon="activity" title="还没有 Agent 轨迹" description="规划、生成、归因时，每一步的输入输出都会记录在这里。" />
          )}
          <div className="trace-list">
            {(traces.data ?? []).map((trace) => (
              <div key={trace.id} className="trace-row">
                <div className="trace-row__head">
                  <Pill tone="violet">{trace.kind}</Pill>
                  <strong>{trace.step || '未命名步骤'}</strong>
                  <span className="muted small">{trace.model || '—'}</span>
                  <div className="spacer" />
                  <span className="muted small">{trace.input_tokens + trace.output_tokens} tokens</span>
                  <span className="muted small">{duration(trace.duration_ms)}</span>
                  <span className="muted small">{fromNow(trace.created_at)}</span>
                </div>
                {trace.error && <div className="notice notice--danger">{trace.error}</div>}
                <details className="trace-row__details">
                  <summary>输入 / 输出 / 工具调用</summary>
                  <div className="trace-columns">
                    <section><h4>输入</h4><pre className="code">{safeJson(trace.input)}</pre></section>
                    <section><h4>输出</h4><pre className="code">{safeJson(trace.output)}</pre></section>
                  </div>
                  {trace.tool_calls?.length > 0 && (
                    <section><h4>工具调用</h4><pre className="code">{safeJson(trace.tool_calls)}</pre></section>
                  )}
                </details>
              </div>
            ))}
          </div>
        </Card>
      )}
    </div>
  )
}
