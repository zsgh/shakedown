import { useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { api, apiUrl } from '../lib/api'
import { fromNow, percent, runDuration } from '../lib/format'
import { useAsync } from '../lib/hooks'
import { DIMENSION, RUN_STATUS, meta } from '../lib/labels'
import type { Overview, ProjectHealth } from '../lib/types'
import { useProject } from '../app/project'
import { Donut, Legend, TrendBars, type Segment } from '../ui/charts'
import { Icon } from '../ui/icons'
import { Button, Card, EmptyState, ErrorBox, Metric, PageHead, Pill, Progress, Skeleton, Status } from '../ui/kit'
import { useToast } from '../ui/overlays'

export default function Overview() {
  const { project } = useProject()
  const navigate = useNavigate()
  const toast = useToast()
  const overview = useAsync<Overview | null>(
    () => (project ? api.get<Overview>(`/projects/${project.id}/overview`) : Promise.resolve(null)),
    [project?.id],
    { refreshMs: 15_000 },
  )
  const health = useAsync<ProjectHealth | null>(
    () => (project ? api.get<ProjectHealth>(`/projects/${project.id}/health`) : Promise.resolve(null)),
    [project?.id],
    { refreshMs: 60_000 },
  )
  const [busy, setBusy] = useState<string | null>(null)

  async function trigger(kind: 'plan' | 'generate' | 'whitebox' | 'consistency', label: string) {
    if (!project) return
    setBusy(kind)
    try {
      const path = {
        plan: `/projects/${project.id}/agent/plan-features`,
        generate: `/projects/${project.id}/cases/generate`,
        whitebox: `/projects/${project.id}/agent/whitebox`,
        consistency: `/projects/${project.id}/consistency/check`,
      }[kind]
      await api.post(path)
      toast.success(`${label}已排队`, '完成后会自动刷新，也可以去 Agent 活动看进度')
      overview.reload()
    } catch (error) {
      toast.error(`${label}没有启动`, String(error))
    } finally {
      setBusy(null)
    }
  }

  if (overview.error) {
    return <ErrorBox>{overview.error}</ErrorBox>
  }

  if (!overview.data) {
    return (
      <div className="page">
        <PageHead title="总览" subtitle="加载项目数据…" />
        <div className="metric-grid">
          {[0, 1, 2, 3].map((index) => (
            <Card key={index}>
              <Skeleton lines={2} />
            </Card>
          ))}
        </div>
      </div>
    )
  }

  const data = overview.data
  const coverage = data.coverage
  const latest = data.runs[0]
  const passRate = latest?.pass_rate ?? 0
  const pendingFindings = data.findings.by_status.pending ?? 0
  const confirmedFindings = data.findings.by_status.confirmed ?? 0
  const target = health.data?.target

  const resultSegments: Segment[] = latest
    ? [
        { label: '通过', value: latest.passed, tone: 'success' },
        { label: '失败', value: latest.failed, tone: 'danger' },
        { label: '不稳定', value: latest.flaky, tone: 'warning' },
      ]
    : []

  const dimensions = Object.entries(data.dimensions).sort((a, b) => b[1].total - a[1].total)
  const setupSteps = [
    { done: (project?.isolation && true) || false, label: '建立项目' },
    { done: !!(health.data?.knowledge.sources ?? 0), label: '导入知识源' },
    { done: data.coverage.total_features > 0, label: '抽取功能点' },
    { done: data.total_cases > 0, label: '生成用例' },
    { done: data.enabled_cases > 0, label: '审核并启用' },
    { done: data.runs.length > 0, label: '完成首次执行' },
  ]
  const setupDone = setupSteps.filter((step) => step.done).length
  const showSetup = setupDone < setupSteps.length

  return (
    <div className="page">
      <PageHead
        eyebrow={
          <span className="crumb-inline">
            <span className={`health-dot tone-${target?.ok === true ? 'success' : target?.ok === false ? 'danger' : 'muted'}`} />
            {project?.base_url || '未配置被测地址'}
            {target?.latency_ms ? <span className="muted"> · {target.latency_ms}ms</span> : null}
          </span>
        }
        title={project?.name ?? '总览'}
        subtitle={project?.description || '被测项目的测试工作台：先看该做什么，再往下钻。'}
        actions={
          <>
            <Button icon="refresh" onClick={() => { overview.reload(); health.reload() }}>
              刷新
            </Button>
            <Button icon="download" onClick={() => window.open(apiUrl(`/projects/${project?.id}/report`), '_blank')}>
              导出报告
            </Button>
          </>
        }
      />

      {target?.ok === false && (
        <div className="notice notice--danger notice--wide">
          <Icon name="alert" size={17} />
          <div>
            <strong>被测环境不可达</strong>
            <span>
              {target.url} · {target.error}
            </span>
          </div>
          <Button size="sm" onClick={() => health.reload()}>重试</Button>
        </div>
      )}

      <div className="overview-grid">
        <div className="overview-main">
          <Card
            title="现在要做什么"
            subtitle={data.attention.length ? '按影响排序，点一下直达处理位置。' : '当前没有需要处理的事项。'}
            className="attention-card"
          >
            {data.attention.length === 0 ? (
              <div className="all-clear">
                <span className="all-clear__icon"><Icon name="check" size={18} /></span>
                <div>
                  <strong>一切正常</strong>
                  <p>用例已审核、缺陷已分诊、最近执行没有失败。可以继续扩充知识源或用例。</p>
                </div>
              </div>
            ) : (
              <div className="attention-list">
                {data.attention.map((item, index) => (
                  <button
                    key={`${item.kind}-${index}`}
                    className={`attention-item attention-item--${item.level}`}
                    onClick={() => navigate(`/projects/${project?.id}/${item.path}`)}
                  >
                    <span className="attention-item__bar" />
                    <span className="attention-item__content">
                      <strong>{item.title}</strong>
                      <span>{item.detail}</span>
                    </span>
                    <span className="attention-item__action">
                      {item.action}
                      <Icon name="arrowRight" size={14} />
                    </span>
                  </button>
                ))}
              </div>
            )}
          </Card>

          <div className="metric-grid metric-grid--4">
            <Metric
              label="功能覆盖率"
              value={percent(coverage.coverage_rate)}
              hint={`${coverage.covered_features} / ${coverage.total_features} 个功能点有正常流用例`}
              icon="target"
              tone={coverage.coverage_rate >= 0.8 ? 'success' : 'warning'}
            />
            <Metric
              label="最近通过率"
              value={latest ? percent(passRate) : '—'}
              hint={latest ? `#${latest.id} · ${latest.total} 条用例` : '还没有执行'}
              icon="gauge"
              tone={!latest ? 'muted' : passRate >= 0.9 ? 'success' : passRate >= 0.7 ? 'warning' : 'danger'}
            />
            <Metric
              label="待审核用例"
              value={data.pending_review}
              hint={`共 ${data.total_cases} 条，已启用 ${data.enabled_cases} 条`}
              icon="beaker"
              tone={data.pending_review ? 'warning' : 'muted'}
            />
            <Metric
              label="缺陷线索"
              value={data.findings.total}
              hint={`待确认 ${pendingFindings} · 已确认 ${confirmedFindings}`}
              icon="bug"
              tone={pendingFindings ? 'danger' : 'muted'}
            />
          </div>

          {showSetup && (
            <Card title="完成初始化" subtitle={`${setupDone} / ${setupSteps.length} 步 · 新项目建议按顺序走一遍`}>
              <div className="setup-steps">
                {setupSteps.map((step, index) => (
                  <div key={step.label} className={`setup-step ${step.done ? 'is-done' : ''}`}>
                    <span className="setup-step__index">{step.done ? <Icon name="check" size={13} /> : index + 1}</span>
                    <span>{step.label}</span>
                  </div>
                ))}
              </div>
              <div className="setup-actions">
                <Button size="sm" onClick={() => navigate(`/projects/${project?.id}/knowledge/sources`)}>导入知识源</Button>
                <Button size="sm" loading={busy === 'plan'} onClick={() => trigger('plan', '功能点抽取')}>抽取功能点</Button>
                <Button size="sm" loading={busy === 'generate'} onClick={() => trigger('generate', '用例生成')}>生成用例</Button>
                <Button size="sm" onClick={() => navigate(`/projects/${project?.id}/cases`)}>去审核</Button>
              </div>
            </Card>
          )}

          <Card
            title="执行趋势"
            subtitle="最近 10 次执行的通过率。柱子越低，失败越集中。"
            actions={
              <Link className="link-btn" to={`/projects/${project?.id}/runs`}>
                全部执行 <Icon name="arrowRight" size={13} />
              </Link>
            }
          >
            {data.runs.length ? (
              <div className="trend-wrap">
                <TrendBars runs={data.runs.slice(0, 10)} />
                <div className="trend-summary">
                  <div>
                    <span>平均通过率</span>
                    <strong>
                      {percent(
                        data.runs.reduce((sum, run) => sum + (run.pass_rate > 1 ? run.pass_rate / 100 : run.pass_rate), 0) /
                          data.runs.length,
                      )}
                    </strong>
                  </div>
                  <div>
                    <span>执行次数</span>
                    <strong>{data.runs.length}</strong>
                  </div>
                  <div>
                    <span>最近耗时</span>
                    <strong>{latest ? runDuration(latest) : '—'}</strong>
                  </div>
                </div>
              </div>
            ) : (
              <EmptyState
                icon="play"
                title="还没有执行记录"
                description="先审核并启用用例，然后执行一次。"
                action={<Button onClick={() => navigate(`/projects/${project?.id}/cases`)}>去审核用例</Button>}
              />
            )}
          </Card>

          <Card title="最近执行" actions={<Link className="link-btn" to={`/projects/${project?.id}/runs`}>查看全部 <Icon name="arrowRight" size={13} /></Link>}>
            {data.runs.length ? (
              <div className="run-list">
                {data.runs.slice(0, 6).map((run) => (
                  <Link key={run.id} className="run-row" to={`/projects/${project?.id}/runs/${run.id}`}>
                    <span className="run-row__id">#{run.id}</span>
                    <Status value={run.status} map={RUN_STATUS} />
                    <span className="run-row__bar">
                      <span className="run-row__bar-pass" style={{ width: `${(run.pass_rate > 1 ? run.pass_rate : run.pass_rate * 100)}%` }} />
                    </span>
                    <span className="run-row__rate">{percent(run.pass_rate)}</span>
                    <span className="run-row__meta">{run.passed} 通过 · {run.failed} 失败 · {run.total} 总数</span>
                    <span className="run-row__time">{fromNow(run.created_at)}</span>
                    <Icon name="chevronRight" size={14} />
                  </Link>
                ))}
              </div>
            ) : (
              <div className="muted">还没有执行。</div>
            )}
          </Card>
        </div>

        <aside className="overview-side">
          {latest && (
            <Card title="最近一次结果" actions={<Link className="link-btn" to={`/projects/${project?.id}/runs/${latest.id}`}>详情</Link>}>
              <div className="donut-row">
                <Donut
                  segments={resultSegments}
                  centerLabel={percent(latest.pass_rate)}
                  centerHint="通过率"
                  size={124}
                />
                <Legend segments={resultSegments} />
              </div>
              <div className="run-facts">
                <span>状态 <Status value={latest.status} map={RUN_STATUS} /></span>
                <span>耗时 <strong>{runDuration(latest)}</strong></span>
                <span>时间 <strong>{fromNow(latest.created_at)}</strong></span>
              </div>
            </Card>
          )}

          <Card title="各维度" subtitle="用例数量与启用情况">
            {dimensions.length ? (
              <div className="dimension-list">
                {dimensions.map(([key, stat]) => {
                  const item = meta(DIMENSION, key)
                  return (
                    <div key={key} className="dimension-row">
                      <div className="dimension-row__head">
                        <Pill tone={item.tone}>{item.label}</Pill>
                        <span>{stat.enabled} / {stat.total} 启用</span>
                      </div>
                      <Progress value={stat.total ? stat.enabled / stat.total : 0} tone={item.tone === 'danger' ? 'danger' : 'brand'} />
                      <div className="dimension-row__foot">
                        <span>通过 {stat.passed ?? 0}</span>
                        <span>失败 {stat.failed ?? 0}</span>
                      </div>
                    </div>
                  )
                })}
              </div>
            ) : (
              <div className="muted">还没有用例。</div>
            )}
          </Card>

          <Card
            title="知识理解"
            actions={<Link className="link-btn" to={`/projects/${project?.id}/knowledge/sources`}>管理</Link>}
          >
            <div className="understanding">
              <div>
                <span>知识源</span>
                <strong>{data.source_count}</strong>
                {(health.data?.knowledge.failed ?? 0) > 0 && <em className="text-danger">失败 {health.data?.knowledge.failed}</em>}
              </div>
              <div>
                <span>接口</span>
                <strong>{data.endpoint_count}</strong>
              </div>
              <div>
                <span>三方不一致</span>
                <strong className={data.inconsistent_endpoints ? 'text-danger' : undefined}>{data.inconsistent_endpoints}</strong>
              </div>
            </div>
            <div className="side-actions">
              <Button size="sm" icon="layers" onClick={() => trigger('consistency', '一致性对照')} loading={busy === 'consistency'}>
                重新对照
              </Button>
              <Button size="sm" icon="code" onClick={() => trigger('whitebox', '白盒走查')} loading={busy === 'whitebox'}>
                白盒走查
              </Button>
            </div>
          </Card>

          <Card title="模块覆盖">
            {coverage.modules.length ? (
              <div className="module-list">
                {coverage.modules.map((module) => (
                  <button
                    key={module.module}
                    className="module-row"
                    onClick={() => navigate(`/projects/${project?.id}/cases?module=${encodeURIComponent(module.module)}`)}
                  >
                    <span className="module-row__name">{module.module}</span>
                    <span className="module-row__bar">
                      <Progress value={module.total ? module.covered / module.total : 0} tone={module.covered === module.total ? 'success' : 'warning'} />
                    </span>
                    <span className="module-row__count">{module.covered}/{module.total}</span>
                  </button>
                ))}
              </div>
            ) : (
              <div className="muted">还没有功能点。</div>
            )}
          </Card>

          {data.backtest && data.backtest.bugs > 0 && (
            <Card title="回测有效性">
              <div className="backtest">
                <strong>{percent(data.backtest.hit_rate ?? 0)}</strong>
                <span>命中 {data.backtest.caught} / {data.backtest.bugs} 个历史 bug · 误报 {data.backtest.false_positives}</span>
              </div>
            </Card>
          )}
        </aside>
      </div>
    </div>
  )
}
