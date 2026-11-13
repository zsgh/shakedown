import { useEffect, useRef, useState } from 'react'
import { useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { api, ApiError } from '../lib/api'
import { fromNow, percent, safeJson } from '../lib/format'
import { useAsync, useDebounced } from '../lib/hooks'
import {
  ASSERTION_KIND,
  ATTRIBUTION,
  CASE_STATUS,
  CASE_VERIFICATION_MODE,
  CASE_VERIFICATION_STATUS,
  DIMENSION,
  FEEDBACK_ACTION,
  meta,
} from '../lib/labels'
import type { CasePage, CoverageTree, ObservedSchemaDrift, Task, TestCase } from '../lib/types'
import { Icon } from '../ui/icons'
import {
  Button,
  EmptyState,
  ErrorBox,
  Field,
  IconButton,
  PageHead,
  Pill,
  Progress,
  Skeleton,
  Status,
  cn,
} from '../ui/kit'
import { ConfirmDialog, Modal, useToast } from '../ui/overlays'

const PAGE_SIZE = 50

type ReviewFilter = 'review' | 'enabled' | 'disabled' | 'all'

export default function Design() {
  const { projectId, caseId } = useParams()
  const navigate = useNavigate()
  const [searchParams, setSearchParams] = useSearchParams()
  const toast = useToast()

  const [filter, setFilter] = useState<ReviewFilter>('review')
  const [dimension, setDimension] = useState('')
  const [executionStatus, setExecutionStatus] = useState('')
  const [featureIds, setFeatureIds] = useState<number[]>([])
  const [treeQuery, setTreeQuery] = useState('')
  const [collapsedModules, setCollapsedModules] = useState<Set<string>>(new Set())
  const [query, setQuery] = useState('')
  const debouncedQuery = useDebounced(query, 280)
  const [page, setPage] = useState(0)
  const [selectedIds, setSelectedIds] = useState<number[]>([])
  const [activeId, setActiveId] = useState<number | null>(caseId ? Number(caseId) : null)
  const [editing, setEditing] = useState<TestCase | null>(null)
  const [confirm, setConfirm] = useState<{ kind: 'enable'; ids: number[] } | null>(null)
  const [busy, setBusy] = useState(false)
  const [enrichTaskId, setEnrichTaskId] = useState<number | null>(null)
  const [generateOpen, setGenerateOpen] = useState(false)
  const [generateDimensions, setGenerateDimensions] = useState<string[]>(['functional'])
  const [generateScope, setGenerateScope] = useState<'missing_dimensions' | 'missing_features' | 'all'>('missing_dimensions')
  const searchRef = useRef<HTMLInputElement>(null)

  const moduleFilter = searchParams.get('module') ?? ''

  const coverage = useAsync<CoverageTree>(() => api.get(`/projects/${projectId}/coverage`), [projectId])
  const drift = useAsync<ObservedSchemaDrift>(
    () => api.get(`/projects/${projectId}/observed/schema-drift`),
    [projectId],
  )
  const cases = useAsync<CasePage>(
    () => {
      const params = new URLSearchParams()
      if (filter === 'review') params.set('status', 'pending,needs_review')
      if (filter === 'enabled') params.set('status', 'enabled')
    if (filter === 'disabled') params.set('status', 'disabled')
      if (filter === 'disabled') params.set('status', 'disabled')
      if (dimension) params.set('dimension', dimension)
      if (executionStatus) params.set('execution_status', executionStatus)
      if (featureIds.length) params.set('feature_ids', featureIds.join(','))
      if (moduleFilter) params.set('module', moduleFilter)
      if (debouncedQuery.trim()) params.set('q', debouncedQuery.trim())
      params.set('sort', 'priority')
      params.set('offset', String(page * PAGE_SIZE))
      params.set('limit', String(PAGE_SIZE))
      return api.get<CasePage>(`/projects/${projectId}/cases/page?${params.toString()}`)
    },
    [projectId, filter, dimension, executionStatus, featureIds, moduleFilter, debouncedQuery, page],
  )

  const counts = cases.data?.counts ?? {}
  const reviewCount = (counts.pending ?? 0) + (counts.needs_review ?? 0)
  const enabledCount = counts.enabled ?? 0
  const disabledCount = counts.disabled ?? 0
  const allCount = Object.values(counts).reduce((sum, value) => sum + value, 0)
  const items = cases.data?.items ?? []
  const total = cases.data?.total ?? 0
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE))
  const hasFilteredRows = total > 0
  const canEnableFiltered =
    hasFilteredRows && filter !== 'enabled' && items.some((item) => item.status !== 'enabled' && (item.steps?.length ?? 0) > 0)

  const activeDetail = useAsync<TestCase | null>(
    () => (activeId ? api.get<TestCase>(`/cases/${activeId}`) : Promise.resolve(null)),
    [activeId],
  )
  const enrichTask = useAsync<Task | null>(
    () => (enrichTaskId ? api.get<Task>(`/tasks/${enrichTaskId}`) : Promise.resolve(null)),
    [enrichTaskId],
    { refreshMs: enrichTaskId ? 2_000 : undefined },
  )
  const feedback = useAsync<Array<{ id: number; action: string; note: string; created_at: string }> | null>(
    () => (activeId ? api.get(`/cases/${activeId}/feedback`) : Promise.resolve(null)),
    [activeId],
  )

  useEffect(() => {
    if (caseId && Number(caseId) !== activeId) {
      setActiveId(Number(caseId))
    }
  }, [caseId])

  useEffect(() => {
    if (!activeId && items.length) {
      setActiveId(items[0].id)
    }
  }, [activeId, items])

  useEffect(() => {
    setPage(0)
    setSelectedIds([])
  }, [filter, dimension, executionStatus, featureIds, moduleFilter, debouncedQuery])

  useEffect(() => {
    if (filter === 'review') setExecutionStatus('')
  }, [filter])

  function selectCase(item: TestCase) {
    setActiveId(item.id)
    navigate(`/projects/${projectId}/cases/${item.id}`, { replace: true })
  }

  function toggleSelected(id: number) {
    setSelectedIds((prev) => (prev.includes(id) ? prev.filter((item) => item !== id) : [...prev, id]))
  }

  async function fetchFilteredCases(): Promise<TestCase[]> {
    const params = new URLSearchParams()
    if (filter === 'review') params.set('status', 'pending,needs_review')
    if (filter === 'enabled') params.set('status', 'enabled')
    if (dimension) params.set('dimension', dimension)
    if (executionStatus) params.set('execution_status', executionStatus)
    if (featureIds.length) params.set('feature_ids', featureIds.join(','))
    if (moduleFilter) params.set('module', moduleFilter)
    if (debouncedQuery.trim()) params.set('q', debouncedQuery.trim())
    params.set('sort', 'id')
    params.set('limit', '200')

    const all: TestCase[] = []
    let offset = 0
    while (offset < 5000) {
      params.set('offset', String(offset))
      const pageData = await api.get<CasePage>(`/projects/${projectId}/cases/page?${params.toString()}`)
      all.push(...pageData.items)
      if (!pageData.items.length || all.length >= pageData.total) break
      offset += pageData.items.length
    }
    return all
  }

  async function selectAllFiltered() {
    try {
      const rows = await fetchFilteredCases()
      if (!rows.length) {
        toast.info('当前筛选下没有用例')
        return
      }
      setSelectedIds(rows.map((item) => item.id))
      toast.info(`已选择 ${rows.length} 条`, '可以批量启用或停用')
    } catch (error) {
      toast.error('读取用例失败', String(error))
    }
  }

  async function enableAllFiltered() {
    try {
      const rows = await fetchFilteredCases()
      if (!rows.length) {
        toast.info('当前筛选下没有用例')
        return
      }
      const executable = rows.filter((item) => (item.steps?.length ?? 0) > 0)
      const skipped = rows.length - executable.length
      if (!executable.length) {
        toast.info('没有可启用的用例', '当前结果都没有可执行步骤，先补全后再启用')
        return
      }
      if (skipped > 0) {
        toast.info(`已跳过 ${skipped} 条没有步骤的用例`, '补全步骤后再启用')
      }
      await applyStatus(executable.map((item) => item.id), 'enabled')
    } catch (error) {
      toast.error('批量启用失败', String(error))
    }
  }

  async function applyStatus(ids: number[], status: string, confirmRisk = false) {
    if (!ids.length) return
    setBusy(true)
    try {
      if (ids.length === 1) {
        await api.post(`/cases/${ids[0]}/status`, { status, confirm_risk: confirmRisk })
      } else {
        await api.post(`/projects/${projectId}/cases/batch-status`, {
          case_ids: ids,
          status,
          confirm_risk: confirmRisk,
        })
      }
      toast.success(status === 'enabled' ? '已启用用例' : '已停用用例', `${ids.length} 条`)
      setSelectedIds([])
      setConfirm(null)
      cases.reload()
      coverage.reload()
      activeDetail.reload()
    } catch (error) {
      if (error instanceof ApiError && error.status === 409 && status === 'enabled' && !confirmRisk) {
        setConfirm({ kind: 'enable', ids })
      } else {
        toast.error('操作失败', String(error))
      }
    } finally {
      setBusy(false)
    }
  }

  async function startRunFor(ids: number[], label: string) {
    if (!ids.length) {
      toast.info(`没有可执行的${label}`, '只有已启用、且有可执行步骤的用例会执行')
      return
    }
    setBusy(true)
    try {
      const run = await api.post<{ id: number; case_ids?: number[]; stats?: { total?: number } }>(
        `/projects/${projectId}/runs`,
        { case_ids: ids },
      )
      const count = run.case_ids?.length || run.stats?.total || 0
      if (!count) {
        toast.info('没有可执行的用例', '选中的用例可能未启用或没有可执行步骤')
        return
      }
      toast.success('已排队执行', `运行 #${run.id} · ${count} 条用例`)
      setSelectedIds([])
    } catch (error) {
      toast.error('执行没有排上', String(error))
    } finally {
      setBusy(false)
    }
  }

  async function runSelected(ids: number[]) {
    // 不能只用当前页 items 判断：全选筛选可能跨页选中。
    // 后端会按 enabled + 可执行步骤过滤，这里把完整 id 集合交给它。
    await startRunFor(ids, '选中用例')
  }

  useEffect(() => {
    const task = enrichTask.data
    if (!task || !['succeeded', 'failed', 'cancelled'].includes(task.status)) return
    setEnrichTaskId(null)
    cases.reload()
    activeDetail.reload()
    if (task.status === 'succeeded') {
      toast.success('AI 补全完成', '请检查步骤、断言和依据后自行启用')
    } else {
      toast.error('AI 补全失败', task.error?.slice(0, 160) ?? '任务未完成')
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enrichTask.data?.id, enrichTask.data?.status])

  async function enrichCase() {
    if (!activeId) return
    setBusy(true)
    try {
      const result = await api.post<{ task_id: number }>(`/cases/${activeId}/enrich`)
      setEnrichTaskId(result.task_id)
      toast.info('AI 补全已排队', '只补充步骤/断言，不会自动启用')
    } catch (error) {
      toast.error('没有启动补全', String(error))
    } finally {
      setBusy(false)
    }
  }

  async function generateCases() {
    if (!generateDimensions.length) {
      toast.info('至少选择一个维度')
      return
    }
    setBusy(true)
    try {
      const params = new URLSearchParams()
      generateDimensions.forEach((dimension) => params.append('dimensions', dimension))
      if (generateScope === 'missing_dimensions') params.set('only_missing_dimensions', 'true')
      if (generateScope === 'missing_features') params.set('only_missing', 'true')
      await api.post(`/projects/${projectId}/cases/generate?${params.toString()}`)
      toast.success(
        '用例生成已排队',
        generateScope === 'missing_dimensions'
          ? '只补缺失维度，不会重跑已有维度'
          : generateScope === 'missing_features'
            ? '只补完全没有用例的功能点'
            : '全量重新生成，可能产生重复用例',
      )
      setGenerateOpen(false)
      window.setTimeout(() => {
        cases.reload()
        coverage.reload()
      }, 2500)
    } catch (error) {
      toast.error('没有启动', String(error))
    } finally {
      setBusy(false)
    }
  }

  function newCase() {
    const draft: TestCase = {
      id: 0,
      project_id: Number(projectId),
      feature_id: featureIds.length === 1 ? featureIds[0] : null,
      title: '',
      dimension: 'functional',
      case_type: 'api',
      status: 'pending',
      generation: 'manual',
      priority: 100,
      tags: [],
      preconditions: { items: [] },
      steps: [],
      expected: { assertions: [] },
      evidence: [],
      spec: {
        title: '',
        dimension: 'functional',
        type: 'api',
        steps: [],
        assertions: [],
        evidence: [],
        preconditions: [],
        cleanup: [],
        missing_input: '',
      },
      missing_input: null,
      verification_mode: 'api',
      created_at: '',
      updated_at: '',
    }
    setEditing(draft)
  }

  async function trigger(kind: 'plan' | 'generate') {
    setBusy(true)
    try {
      await api.post(
        kind === 'plan'
          ? `/projects/${projectId}/agent/plan-features`
          : `/projects/${projectId}/cases/generate`,
      )
      toast.success(kind === 'plan' ? '功能点抽取已排队' : '用例生成已排队', '完成后自动刷新')
      window.setTimeout(() => {
        cases.reload()
        coverage.reload()
      }, 2500)
    } catch (error) {
      toast.error('没有启动', String(error))
    } finally {
      setBusy(false)
    }
  }

  // 键盘：J/K 切换，A 启用，X 停用，E 编辑，/ 搜索
  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      const target = event.target as HTMLElement
      if (['INPUT', 'TEXTAREA', 'SELECT'].includes(target.tagName) || target.isContentEditable) return
      if (event.metaKey || event.ctrlKey || event.altKey) return
      const index = items.findIndex((item) => item.id === activeId)
      if (event.key === 'j' || event.key === 'ArrowDown') {
        event.preventDefault()
        const next = items[Math.min(items.length - 1, index + 1)]
        if (next) selectCase(next)
      } else if (event.key === 'k' || event.key === 'ArrowUp') {
        event.preventDefault()
        const prev = items[Math.max(0, index - 1)]
        if (prev) selectCase(prev)
      } else if (event.key === 'a' && activeId) {
        applyStatus([activeId], 'enabled')
      } else if (event.key === 'x' && activeId) {
        applyStatus([activeId], 'disabled')
      } else if (event.key === 'e' && activeDetail.data) {
        setEditing(activeDetail.data)
      } else if (event.key === '/') {
        event.preventDefault()
        searchRef.current?.focus()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  })

  const moduleFeatures = coverage.data?.modules.flatMap((module) => module.features) ?? []
  const selectedFeature = featureIds.length === 1
    ? moduleFeatures.find((feature) => feature.id === featureIds[0]) ?? null
    : null

  const treeTerm = treeQuery.trim().toLowerCase()
  const treeModules = (coverage.data?.modules ?? [])
    .map((module) => ({
      ...module,
      features: treeTerm
        ? module.features.filter((feature) =>
            `${module.module} ${feature.name} ${feature.description}`.toLowerCase().includes(treeTerm),
          )
        : module.features,
    }))
    .filter((module) => !treeTerm || module.features.length > 0)

  function selectFeature(feature: (typeof moduleFeatures)[number], additive = false) {
    if (moduleFilter) {
      const next = new URLSearchParams(searchParams)
      next.delete('module')
      setSearchParams(next, { replace: true })
    }
    setFeatureIds((prev) => {
      if (!additive) return [feature.id]
      return prev.includes(feature.id)
        ? prev.filter((id) => id !== feature.id)
        : [...prev, feature.id]
    })
    // 默认筛选是「待审核」；如果这个功能点只有已启用用例，直接切到全部，
    // 否则左侧显示有 N 条、右侧却是空列表，很容易误以为没有用例。
    if (!additive && filter === 'review' && feature.total_cases > 0 && feature.total_cases === feature.enabled_cases) {
      setFilter('all')
    }
  }

  return (
    <div className="page page--workbench">
      <PageHead
        title="测试设计"
        subtitle="审核 Agent 生成的用例：看依据、改断言、启用后才会进入执行集。"
        actions={
          <>
            <Button icon="refresh" onClick={() => { cases.reload(); coverage.reload() }}>
              刷新
            </Button>
            <Button icon="beaker" loading={busy} onClick={() => trigger('plan')}>
              抽取功能点
            </Button>
            <Button icon="plus" onClick={newCase}>
              新建用例
            </Button>
            <Button variant="primary" icon="sparkles" loading={busy} onClick={() => setGenerateOpen(true)}>
              生成用例
            </Button>
          </>
        }
      />

      <div className="workbench">
        <aside className="workbench__tree">
          <div className="tree-head">
            <span className="tree-head__title">功能覆盖</span>
            <span className="tree-head__rate">{percent(coverage.data?.coverage_rate ?? 0)}</span>
          </div>
          <Progress value={coverage.data?.coverage_rate ?? 0} tone="brand" />
          <p className="tree-hint">
            {coverage.data?.covered_features ?? 0} / {coverage.data?.total_features ?? 0} 个功能点有正常流用例
          </p>
          {coverage.data?.risk && (
            <div className="stack" style={{ gap: 6, marginBottom: 10 }}>
              <div className="tree-head">
                <span className="tree-head__title">风险覆盖</span>
                <span className="tree-head__rate">{percent(coverage.data.risk.coverage_rate)}</span>
              </div>
              <Progress value={coverage.data.risk.coverage_rate} tone="warning" />
              <p className="tree-hint">
                {coverage.data.risk.covered} / {coverage.data.risk.total} 类风险有覆盖（鉴权、注入、幂等、隔离等），
                与契约覆盖分开统计。
              </p>
              <div className="row-actions">
                {coverage.data.risk.categories
                  .filter((item) => !item.covered)
                  .slice(0, 6)
                  .map((item) => (
                    <Pill key={item.key} tone="muted">{item.label}</Pill>
                  ))}
              </div>
            </div>
          )}
          {coverage.data?.oracles && (
            <p className="tree-hint">
              断言口径：契约 {coverage.data.oracles.oracle_mix.contract} / 策略 {coverage.data.oracles.oracle_mix.policy} /
              人工 {coverage.data.oracles.oracle_mix.manual}
              {coverage.data.oracles.oracle_mix.none > 0
                ? ` / 无依据 ${coverage.data.oracles.oracle_mix.none}`
                : ''}
              ，待确认 {coverage.data.oracles.pending_assertions}，探针用例 {coverage.data.oracles.probe_cases}
            </p>
          )}
          {drift.data && (
            <div className="stack" style={{ gap: 4, marginBottom: 10 }}>
              <p className="tree-hint">
                响应结构漂移：{drift.data.drift_count > 0 ? `${drift.data.drift_count} 个接口` : '未发现'}
                （{drift.data.endpoints} 个接口 / {drift.data.samples} 条成功响应样本，仅作风险线索，不直接判缺陷）
              </p>
              {drift.data.drift_count === 0 && drift.data.message && (
                <p className="tree-hint">{drift.data.message}</p>
              )}
              {drift.data.drift.length > 0 && (
                <details>
                  <summary className="tree-hint" style={{ cursor: 'pointer' }}>
                    查看漂移明细
                  </summary>
                  <div className="stack" style={{ gap: 4, marginTop: 6 }}>
                    {drift.data.drift.slice(0, 10).map((row) => (
                      <p className="tree-hint" key={`${row.endpoint}-${row.status_code}`}>
                        {row.endpoint} · {row.status_code}
                        {row.added.length ? `｜新增 ${row.added.join(', ')}` : ''}
                        {row.removed.length ? `｜缺失 ${row.removed.join(', ')}` : ''}
                        {row.type_changed.length
                          ? `｜类型 ${row.type_changed.map((item) => `${item.field}:${item.from}→${item.to}`).join(', ')}`
                          : ''}
                        {row.root_changed ? `｜根类型 ${row.root_changed.from}→${row.root_changed.to}` : ''}
                      </p>
                    ))}
                  </div>
                </details>
              )}
            </div>
          )}
          <div className="tree-search">
            <Icon name="search" size={13} />
            <input
              value={treeQuery}
              onChange={(event) => setTreeQuery(event.target.value)}
              placeholder="搜索功能点"
              aria-label="搜索功能点"
            />
          </div>
          <button
            className={cn('tree-all', !featureIds.length && !moduleFilter && 'is-active')}
            onClick={() => {
              setFeatureIds([])
              setFilter('all')
              searchParams.delete('module')
              setSearchParams(searchParams, { replace: true })
            }}
          >
            全部用例
            <span>{allCount}</span>
          </button>
          {coverage.loading && <Skeleton lines={5} />}
          {treeModules.map((module) => {
            const collapsed = collapsedModules.has(module.module) && !treeQuery.trim()
            return (
              <div key={module.module} className={cn('tree-module', moduleFilter === module.module && 'is-active')}>
                <button
                  className="tree-module__head"
                  onClick={() => {
                    const next = new URLSearchParams(searchParams)
                    if (moduleFilter === module.module) next.delete('module')
                    else next.set('module', module.module)
                    setFeatureIds([])
                    setSearchParams(next, { replace: true })
                  }}
                >
                  <Icon name="layers" size={14} />
                  <span>{module.module}</span>
                  <em>{module.covered}/{module.total}</em>
                  <span
                    className="tree-module__chevron"
                    role="button"
                    tabIndex={0}
                    aria-label={collapsed ? '展开模块' : '折叠模块'}
                    onClick={(event) => {
                      event.stopPropagation()
                      setCollapsedModules((prev) => {
                        const next = new Set(prev)
                        if (next.has(module.module)) next.delete(module.module)
                        else next.add(module.module)
                        return next
                      })
                    }}
                    onKeyDown={(event) => {
                      if (event.key === 'Enter' || event.key === ' ') {
                        event.preventDefault()
                        event.stopPropagation()
                        setCollapsedModules((prev) => {
                          const next = new Set(prev)
                          if (next.has(module.module)) next.delete(module.module)
                          else next.add(module.module)
                          return next
                        })
                      }
                    }}
                  >
                    <Icon name={collapsed ? 'chevronRight' : 'chevronDown'} size={13} />
                  </span>
                </button>
                {!collapsed && (
                  <div className="tree-features">
                    {module.features.map((feature) => (
                      <div
                        key={feature.id}
                        className={cn(
                          'tree-feature',
                          featureIds.includes(feature.id) && 'is-active',
                          !feature.covered && 'is-uncovered',
                        )}
                        onClick={() => selectFeature(feature)}
                        onKeyDown={(event) => {
                          if (event.key === 'Enter' || event.key === ' ') {
                            event.preventDefault()
                            selectFeature(feature)
                          }
                        }}
                        role="button"
                        tabIndex={0}
                        title={feature.description}
                      >
                        <input
                          type="checkbox"
                          className="tree-feature__check"
                          checked={featureIds.includes(feature.id)}
                          onClick={(event) => event.stopPropagation()}
                          onChange={() => selectFeature(feature, true)}
                          aria-label={`多选功能点 ${feature.name}`}
                        />
                        <span className={cn('tree-feature__dot', feature.covered ? 'is-ok' : 'is-bad')} />
                        <span className="tree-feature__name">{feature.name}</span>
                        <span
                          className="tree-feature__count"
                          title={`${feature.total_cases} 条用例（已启用 ${feature.enabled_cases}）`}
                        >
                          {feature.total_cases}
                        </span>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            )
          })}
          {coverage.data && treeModules.length === 0 && (
            <div className="tree-empty">
              <Icon name="alert" size={14} />
              {treeQuery.trim() ? '没有匹配的功能点' : '还没有功能点。先点上方「抽取功能点」。'}
            </div>
          )}
        </aside>

        <section className="workbench__list">
          <div className="list-toolbar">
            <div className="segmented segmented--sm">
              {([
                ['review', '待审核', reviewCount],
                ['enabled', '已启用', enabledCount],
                ['disabled', '已停用', disabledCount],
                ['all', '全部', allCount],
              ] as Array<[ReviewFilter, string, number]>).map(([value, label, count]) => (
                <button
                  key={value}
                  className={cn('segmented__item', filter === value && 'is-active')}
                  onClick={() => {
                    setFilter(value)
                    if (value === 'review') setExecutionStatus('')
                  }}
                >
                  {label}
                  <span className="segmented__count">{count}</span>
                </button>
              ))}
            </div>
            <div className="list-search">
              <Icon name="search" size={14} />
              <input
                ref={searchRef}
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder="搜索标题 / 用例 ID / 缺陷 ID"
              />
            </div>
            <div className="list-filters">
              <select className="input input--sm" value={dimension} onChange={(event) => setDimension(event.target.value)}>
                <option value="">全部维度</option>
                {Object.entries(DIMENSION).map(([key, item]) => (
                  <option key={key} value={key}>{item.label}</option>
                ))}
              </select>
              <select className="input input--sm" value={executionStatus} onChange={(event) => setExecutionStatus(event.target.value)}>
                <option value="">执行历史：全部</option>
                <option value="never_executed">从未执行</option>
                <option value="executed">已执行</option>
                <option value="enabled_no_result">已启用但无结果</option>
                <option value="pending_not_enabled">待审核未启用</option>
                <option value="stale_after_source">知识源更新后未复核</option>
              </select>
              {(moduleFilter || featureIds.length > 0) && (
                <button
                  className="filter-chip"
                  onClick={() => {
                    setFeatureIds([])
                    searchParams.delete('module')
                    setSearchParams(searchParams, { replace: true })
                  }}
                >
                  {featureIds.length === 1
                    ? selectedFeature?.name ?? '1 个功能点'
                    : featureIds.length > 1
                      ? `${featureIds.length} 个功能点`
                      : moduleFilter}
                  <Icon name="close" size={12} />
                </button>
              )}
              {hasFilteredRows && (
                <>
                  <Button size="sm" variant="ghost" onClick={selectAllFiltered}>
                    全选筛选
                  </Button>
                  <Button
                    size="sm"
                    icon="check"
                    loading={busy}
                    disabled={!canEnableFiltered}
                    onClick={enableAllFiltered}
                  >
                    全部启用
                  </Button>
                </>
              )}
            </div>
          </div>

          {selectedIds.length > 0 && (
            <div className="bulkbar">
              <strong>{selectedIds.length} 条已选</strong>
              <Button size="sm" variant="primary" icon="check" loading={busy} onClick={() => applyStatus(selectedIds, 'enabled')}>
                启用
              </Button>
              <Button size="sm" icon="x" loading={busy} onClick={() => applyStatus(selectedIds, 'disabled')}>
                停用
              </Button>
              <Button size="sm" icon="play" loading={busy} onClick={() => runSelected(selectedIds)}>
                执行选中
              </Button>
              <div className="spacer" />
              <button className="link-btn" onClick={() => setSelectedIds([])}>清除选择</button>
            </div>
          )}

          <div className="case-list">
            {cases.error && <ErrorBox>{cases.error}</ErrorBox>}
            {cases.loading && !cases.data && <Skeleton lines={8} />}
            {!cases.loading && items.length === 0 && (
              <EmptyState
                icon="beaker"
                title={filter === 'review' ? '没有待审核用例' : '没有符合条件的用例'}
                description={
                  filter === 'review'
                    ? 'Agent 生成的用例会先进入这里；也可以现在生成一批。'
                    : '换个筛选条件，或清除功能点过滤。'
                }
                action={
                  filter === 'review' ? (
                    <Button size="sm" variant="primary" loading={busy} onClick={() => setGenerateOpen(true)}>
                      生成用例
                    </Button>
                  ) : undefined
                }
              />
            )}
            {items.map((item) => {
              const dim = meta(DIMENSION, item.dimension)
              return (
                <div
                  key={item.id}
                  className={cn('case-item', activeId === item.id && 'is-active')}
                  onClick={() => selectCase(item)}
                  role="button"
                  tabIndex={0}
                  onKeyDown={(event) => {
                    if (event.key === 'Enter') selectCase(item)
                  }}
                >
                  <input
                    type="checkbox"
                    className="case-item__check"
                    checked={selectedIds.includes(item.id)}
                    onClick={(event) => event.stopPropagation()}
                    onChange={() => toggleSelected(item.id)}
                    aria-label={`选择 ${item.title}`}
                  />
                  <div className="case-item__main">
                    <div className="case-item__title">{item.title}</div>
                    <div className="case-item__meta">
                      <span className="mono muted small">#{item.id}</span>
                      <Pill tone={dim.tone}>{dim.label}</Pill>
                      <Status value={item.status} map={CASE_STATUS} />
                      {(item.status === 'disabled' || item.status === 'needs_review') &&
                        item.verification?.attribution && (
                          <Pill tone={meta(ATTRIBUTION, item.verification.attribution.category).tone}>
                            {meta(ATTRIBUTION, item.verification.attribution.category).label}
                          </Pill>
                        )}
                      <span>{item.feature_name ?? '未映射功能点'}</span>
                      {item.evidence?.length > 0 && (
                        <span className="case-item__evidence">
                          <Icon name="link" size={11} /> {item.evidence.length} 条依据
                        </span>
                      )}
                      {item.missing_input && (
                        <span className="text-warning">{item.steps?.length ? '待补全' : '缺少输入'}</span>
                      )}
                    </div>
                  </div>
                  <Icon name="chevronRight" size={14} className="case-item__arrow" />
                </div>
              )
            })}
          </div>

          <footer className="list-foot">
            <span>共 {total} 条</span>
            <div className="spacer" />
            <Button size="sm" icon="chevronLeft" disabled={page === 0} onClick={() => setPage((value) => Math.max(0, value - 1))}>
              上一页
            </Button>
            <span className="list-foot__page">{page + 1} / {pages}</span>
            <Button size="sm" disabled={page + 1 >= pages} onClick={() => setPage((value) => value + 1)}>
              下一页 <Icon name="chevronRight" size={13} />
            </Button>
          </footer>
        </section>

        <section className="workbench__detail">
          {!activeId && (
            <EmptyState icon="beaker" title="选择一条用例" description="从左侧列表选一条，查看步骤、断言和依据。" />
          )}
          {activeId && activeDetail.loading && !activeDetail.data && <Skeleton lines={10} />}
          {activeDetail.data && (
            <CaseDetailPane
              item={activeDetail.data}
              feedback={feedback.data ?? []}
              busy={busy}
              onEdit={() => setEditing(activeDetail.data!)}
              onEnrich={enrichCase}
              enriching={!!enrichTaskId}
              onStatus={(status) => applyStatus([activeDetail.data!.id], status)}
              onRun={() => runSelected([activeDetail.data!.id])}
              onClose={() => {
                setActiveId(null)
                navigate(`/projects/${projectId}/cases`, { replace: true })
              }}
            />
          )}
        </section>
      </div>

      <CaseEditModal
        item={editing}
        onClose={() => setEditing(null)}
        onSaved={() => {
          setEditing(null)
          cases.reload()
          activeDetail.reload()
          toast.success('用例已保存')
        }}
      />

      <ConfirmDialog
        open={!!confirm}
        title="启用高风险用例？"
        description="这条用例包含删除数据 / 修改配置等高风险操作，执行时可能改变被测系统状态。确认要启用吗？"
        confirmLabel="仍然启用"
        danger
        busy={busy}
        onCancel={() => setConfirm(null)}
        onConfirm={() => confirm && applyStatus(confirm.ids, 'enabled', true)}
      />

      <Modal
        open={generateOpen}
        onClose={() => setGenerateOpen(false)}
        title="生成用例"
        subtitle="先选维度和范围，避免一次跑 60 个批次。"
        footer={
          <>
            <div className="spacer" />
            <Button onClick={() => setGenerateOpen(false)}>取消</Button>
            <Button
              variant="primary"
              icon="sparkles"
              loading={busy}
              disabled={!generateDimensions.length}
              onClick={generateCases}
            >
              开始生成
            </Button>
          </>
        }
      >
        <div className="stack">
          <Field label="生成维度">
            <div className="crumb-inline">
              {Object.entries(DIMENSION).map(([key, value]) => (
                <label key={key} className="checkbox-label">
                  <input
                    type="checkbox"
                    checked={generateDimensions.includes(key)}
                    onChange={() =>
                      setGenerateDimensions((prev) =>
                        prev.includes(key) ? prev.filter((item) => item !== key) : [...prev, key],
                      )
                    }
                  />
                  {value.label}
                </label>
              ))}
            </div>
          </Field>

          <Field label="生成范围">
            <select className="input" value={generateScope} onChange={(event) => setGenerateScope(event.target.value as typeof generateScope)}>
              <option value="missing_dimensions">只补缺失维度（推荐，不重复已有用例）</option>
              <option value="missing_features">只补完全没有用例的功能点</option>
              <option value="all">全部重新生成（可能重复）</option>
            </select>
          </Field>

          <div className={cn('notice', generateDimensions.length ? 'notice--info' : 'notice--warning')}>
            <Icon name={generateDimensions.length ? 'info' : 'alert'} size={16} />
            <span>
              {generateDimensions.length
                ? `${generateDimensions.map((key) => DIMENSION[key]?.label ?? key).join(' / ')} · ${
                    generateScope === 'missing_dimensions'
                      ? '只补缺失维度，不重复已有用例'
                      : generateScope === 'missing_features'
                        ? '只补完全没有用例的功能点'
                        : '全部重新生成，可能产生重复用例'
                  }`
                : '请至少选择一个生成维度。'}
            </span>
          </div>
        </div>
      </Modal>
    </div>
  )
}

function CaseDetailPane({
  item,
  feedback,
  busy,
  onEdit,
  onEnrich,
  enriching,
  onStatus,
  onRun,
  onClose,
}: {
  item: TestCase
  feedback: Array<{ id: number; action: string; note: string; created_at: string }>
  busy: boolean
  onEdit: () => void
  onEnrich: () => void
  enriching: boolean
  onStatus: (status: string) => void
  onRun: () => void
  onClose: () => void
}) {
  const spec = item.spec ?? {}
  const steps = spec.steps ?? item.steps ?? []
  // 断言主要写在步骤的 assert 里；后端会把「步骤内联 + 顶层」汇总到 expected.assertions。
  const assertions = (item.expected?.assertions?.length ?? 0) > 0
    ? item.expected.assertions
    : (spec.assertions ?? [])
  const evidence = item.evidence ?? spec.evidence ?? []
  const dim = meta(DIMENSION, item.dimension)

  return (
    <div className="detail-pane">
      <header className="detail-head">
        <div className="detail-head__top">
          <div className="detail-head__badges">
            <Pill tone={dim.tone}>{dim.label}</Pill>
            <Status value={item.status} map={CASE_STATUS} />
            <span className="muted small">{item.case_type === 'web' ? 'Web' : '接口'}</span>
            <span className="muted small">优先级 {item.priority}</span>
            <span className="muted small">#{item.id}</span>
          </div>
          <IconButton icon="close" label="关闭详情" onClick={onClose} />
        </div>
        <h2>{item.title}</h2>
        <p className="detail-head__feature">
          <span className="mono">#{item.id}</span>
          <span className="muted"> · </span>
          <Icon name="layers" size={13} />
          {item.feature_name ?? '未映射功能点'}
          {(item.finding_ids ?? []).map((findingId) => (
            <a
              key={findingId}
              className="link-btn"
              href={`/projects/${item.project_id}/findings?finding=${findingId}`}
            >
              缺陷 #{findingId}
            </a>
          ))}
          <span className="muted"> · 更新于 {fromNow(item.updated_at)}</span>
        </p>
        {item.verification?.attribution && (item.status === 'disabled' || item.status === 'needs_review') && (
          <div className="notice notice--info">
            <Icon name="info" size={16} />
            <span>
              <strong>
                {item.status === 'disabled' ? '停用原因' : '待复核原因'} ·{' '}
                {meta(ATTRIBUTION, item.verification.attribution.category).label}
              </strong>
              {item.verification.attribution.reason}
            </span>
          </div>
        )}
        {item.missing_input && (
          <div className={cn('notice', steps.length > 0 ? 'notice--info' : 'notice--warning')}>
            <Icon name="info" size={16} />
            <span>
              <strong>{steps.length > 0 ? '待补全说明（不影响执行）' : '缺少输入，暂时无法执行'}</strong>
              {item.missing_input}
            </span>
            <button className="link-btn notice__action" onClick={onEdit}>修改</button>
          </div>
        )}
        {spec.change_impact && (
          <div className="notice notice--info">
            <Icon name="activity" size={16} />
            <span>
              <strong>受变更影响，需要复核</strong>
              {Array.isArray(spec.change_impact.reasons) && spec.change_impact.reasons.slice(0, 3).map((reason: any, index: number) => (
                <span key={index} className="muted small"> · {String(reason.detail ?? reason.kind ?? '')}</span>
              ))}
            </span>
          </div>
        )}
        {Array.isArray(spec.change_candidates) && spec.change_candidates.length > 0 && (
          <div className="notice notice--info">
            <Icon name="search" size={16} />
            <span>
              <strong>疑似受文档语义变化影响</strong>
              {spec.change_candidates.slice(0, 2).map((candidate: any, index: number) => (
                <span key={index} className="muted small"> · {String(candidate.reason ?? '')}</span>
              ))}
            </span>
          </div>
        )}
      </header>

      <div className="detail-actions">
        {item.status !== 'enabled' ? (
          <Button variant="primary" icon="check" loading={busy} onClick={() => onStatus('enabled')}>启用</Button>
        ) : (
          <Button icon="pause" loading={busy} onClick={() => onStatus('disabled')}>停用</Button>
        )}
        <Button
          icon="play"
          loading={busy}
          onClick={onRun}
          disabled={item.status !== 'enabled' || steps.length === 0}
          title={
            item.status !== 'enabled'
              ? '请先启用用例'
              : steps.length === 0
                ? item.missing_input ?? '没有可执行步骤，请先补全'
                : item.missing_input
                  ? `仍可执行；待补全说明：${item.missing_input}`
                  : '只执行这一条用例'
          }
        >
          单独执行
        </Button>
        <Button icon="edit" onClick={onEdit}>编辑</Button>
        <Button
          icon="sparkles"
          loading={enriching}
          onClick={onEnrich}
          title="基于接口清单、代码和文档补全这条用例的步骤 / 断言 / 依据；不会修改文档"
        >
          AI 补全
        </Button>
        <div className="spacer" />
        <span className="kbd-hint"><kbd>J</kbd><kbd>K</kbd> 切换 <kbd>A</kbd> 启用 <kbd>E</kbd> 编辑</span>
      </div>

      <div className="detail-body">
        <section className="detail-section">
          <h3>验证方式</h3>
          <div className="crumb-inline">
            <Pill tone="blue">{CASE_VERIFICATION_MODE[item.verification_mode || 'api'] ?? item.verification_mode}</Pill>
            {(item.verification_status || 'unverified') !== 'unverified' && (
              <Pill tone={(CASE_VERIFICATION_STATUS[item.verification_status || 'unverified'] ?? CASE_VERIFICATION_STATUS.unverified).tone}>
                {(CASE_VERIFICATION_STATUS[item.verification_status || 'unverified'] ?? CASE_VERIFICATION_STATUS.unverified).label}
              </Pill>
            )}
            {item.verification?.reported && (
              <Pill tone={item.verification.reported.passed ? 'info' : 'warning'}>
                {item.verification.reported.passed ? '开发自报通过' : '开发自报失败'}
              </Pill>
            )}
            {item.verified_by && <span className="muted small">验证者：{item.verified_by}</span>}
            {item.verified_at && <span className="muted small">· {fromNow(item.verified_at)}</span>}
          </div>
          {(item.verification_status || 'unverified') !== 'unverified' && item.verification && (
            <div className="stack" style={{ marginTop: 8 }}>
              {item.verification.command && <pre className="code code--compact">{String(item.verification.command)}</pre>}
              {item.verification.result && <pre className="code code--compact">{String(item.verification.result)}</pre>}
              {Array.isArray(item.verification.artifacts) && item.verification.artifacts.length > 0 && (
                <p className="muted small">产物：{item.verification.artifacts.join(' · ')}</p>
              )}
              {item.verification.note && <p className="muted small">{String(item.verification.note)}</p>}
            </div>
          )}
          {(item.verification_status || 'unverified') === 'unverified' && item.verification?.reported && (
            <div className="stack" style={{ marginTop: 8 }}>
              {item.verification.reported.actor && (
                <p className="muted small">开发 Agent 报告：{String(item.verification.reported.actor)}</p>
              )}
              {item.verification.reported.command && (
                <pre className="code code--compact">{String(item.verification.reported.command)}</pre>
              )}
              {item.verification.reported.result && (
                <pre className="code code--compact">{String(item.verification.reported.result)}</pre>
              )}
              {item.verification.reported.note && <p className="muted small">{String(item.verification.reported.note)}</p>}
            </div>
          )}
        </section>

        {spec.description && <p className="detail-desc">{spec.description}</p>}

        {(spec.preconditions ?? []).length > 0 && (
          <section className="detail-section">
            <h3>前置条件</h3>
            <div className="preconditions">
              {(spec.preconditions ?? []).map((precondition: any, index: number) => (
                <div key={index} className="precondition">
                  <Icon name="zap" size={13} />
                  <span>{precondition.name ?? precondition.kind}</span>
                  <code>{precondition.kind}</code>
                  {precondition.account_ref && <span className="muted">账号 {precondition.account_ref}</span>}
                </div>
              ))}
            </div>
          </section>
        )}

        <section className="detail-section">
          <h3>步骤 <span className="detail-section__count">{steps.length}</span></h3>
          {steps.length === 0 && <div className="muted small">这条用例还没有可执行步骤（可能等待人工补全）。</div>}
          <div className="steps">
            {steps.map((step: any, index: number) => (
              <div key={index} className="step">
                <span className="step__index">{index + 1}</span>
                <div className="step__body">
                  <div className="step__head">
                    {step.method && <Pill tone={step.method === 'GET' ? 'blue' : step.method === 'DELETE' ? 'danger' : 'success'}>{step.method}</Pill>}
                    <span className="mono">{step.path ?? step.name ?? step.action ?? '步骤'}</span>
                    {(step.assert ?? []).length > 0 && (
                      <span className="muted small">{step.assert.length} 条断言（见下方）</span>
                    )}
                  </div>
                  {step.action === 'request' && (
                    <div className="step__details">
                      {step.body && (
                        <div>
                          <span className="step__label">请求体</span>
                          <pre className="code">{safeJson(step.body)}</pre>
                        </div>
                      )}
                      {step.headers && Object.keys(step.headers).length > 0 && (
                        <div>
                          <span className="step__label">Headers</span>
                          <pre className="code">{safeJson(step.headers)}</pre>
                        </div>
                      )}
                    </div>
                  )}
                  {step.action !== 'request' && (
                    <pre className="code">{safeJson(step)}</pre>
                  )}
                </div>
              </div>
            ))}
          </div>
        </section>

        <section className="detail-section">
          <h3>断言与依据 <span className="detail-section__count">{assertions.length}</span></h3>
          {assertions.length === 0 && <div className="muted small">没有断言；未确认（无依据）的断言不会判失败。</div>}
          <div className="assertions">
            {assertions.map((assertion: any, index: number) => (
              <div key={index} className={cn('assertion', assertion.status === 'confirmed' ? 'is-confirmed' : 'is-pending')}>
                <div className="assertion__top">
                  {typeof assertion.step_index === 'number' && (
                    <Pill tone="blue">步骤 {assertion.step_index + 1}</Pill>
                  )}
                  <Pill tone={assertion.status === 'confirmed' ? 'success' : 'warning'}>
                    {assertion.status === 'confirmed' ? '已确认' : '待确认'}
                  </Pill>
                  <code title={assertion.kind}>{ASSERTION_KIND[assertion.kind] ?? assertion.kind}</code>
                  <span className="assertion__desc">{assertion.description}</span>
                </div>
                <div className="assertion__grid">
                  <div>
                    <span>期望</span>
                    <code>{safeJson(assertion.expected)}</code>
                  </div>
                  {assertion.target && (
                    <div>
                      <span>目标</span>
                      <code>{assertion.target}</code>
                    </div>
                  )}
                  <div>
                    <span>依据</span>
                    {assertion.evidence ? (
                      <div className="evidence-inline">
                        <Pill tone="blue">{assertion.evidence.source_type ?? 'source'}</Pill>
                        <code>{assertion.evidence.ref ?? assertion.evidence.note ?? '—'}</code>
                      </div>
                    ) : (
                      <span className="text-warning">无依据（不判失败）</span>
                    )}
                  </div>
                </div>
                {assertion.evidence?.quote && <blockquote>{assertion.evidence.quote}</blockquote>}
              </div>
            ))}
          </div>
        </section>

        {evidence.length > 0 && (
          <section className="detail-section">
            <h3>用例来源 <span className="detail-section__count">{evidence.length}</span></h3>
            <div className="evidence-list">
              {evidence.map((item2, index) => (
                <div key={index} className="evidence">
                  <Pill tone="blue">{item2.source_type ?? 'source'}</Pill>
                  <code>{item2.ref ?? item2.note ?? '—'}</code>
                  {item2.quote && <p>{item2.quote}</p>}
                </div>
              ))}
            </div>
          </section>
        )}

        {(spec.cleanup ?? []).length > 0 && (
          <section className="detail-section">
            <h3>清理步骤</h3>
            <pre className="code">{safeJson(spec.cleanup)}</pre>
          </section>
        )}

        {feedback.length > 0 && (
          <section className="detail-section">
            <h3>人工记录</h3>
            <div className="feedback-list">
              {feedback.slice(0, 5).map((record) => (
                <div key={record.id}>
                  <span>{FEEDBACK_ACTION[record.action] ?? record.action}</span>
                  <span className="muted">{fromNow(record.created_at)}</span>
                  {record.note && <em>{record.note}</em>}
                </div>
              ))}
            </div>
          </section>
        )}
      </div>
    </div>
  )
}

type StepForm = {
  raw: Record<string, any>
  action: string
  name: string
  method: string
  path: string
  bodyText: string
  saveText: string
}

type AssertionForm = {
  raw: Record<string, any>
  kind: string
  target: string
  operator: string
  expectedText: string
  status: string
  sourceType: string
  ref: string
  quote: string
}

function parseLooseJson(text: string): any {
  const trimmed = text.trim()
  if (!trimmed) return undefined
  try {
    return JSON.parse(trimmed)
  } catch {
    return text
  }
}

function toJsonText(value: any): string {
  if (value === undefined || value === null || value === '') return ''
  return typeof value === 'string' ? value : JSON.stringify(value, null, 2)
}

function toStepForm(step: Record<string, any>): StepForm {
  return {
    raw: { ...step },
    action: String(step.action ?? 'request'),
    name: String(step.name ?? ''),
    method: String(step.method ?? ''),
    path: String(step.path ?? ''),
    bodyText: toJsonText(step.body),
    saveText: toJsonText(step.save),
  }
}

function toAssertionForm(assertion: Record<string, any>): AssertionForm {
  const evidence = assertion.evidence && typeof assertion.evidence === 'object' ? assertion.evidence : {}
  return {
    raw: { ...assertion },
    kind: String(assertion.kind ?? 'status_code'),
    target: String(assertion.target ?? ''),
    operator: String(assertion.operator ?? 'eq'),
    expectedText: toJsonText(assertion.expected),
    status: String(assertion.status ?? 'pending'),
    sourceType: String(evidence.source_type ?? 'manual'),
    ref: String(evidence.ref ?? ''),
    quote: String(evidence.quote ?? ''),
  }
}

function CaseEditModal({
  item,
  onClose,
  onSaved,
}: {
  item: TestCase | null
  onClose: () => void
  onSaved: () => void
}) {
  const toast = useToast()
  const [title, setTitle] = useState('')
  const [dimension, setDimension] = useState('functional')
  const [verificationMode, setVerificationMode] = useState('api')
  const [priority, setPriority] = useState(100)
  const [json, setJson] = useState('')
  const [mode, setMode] = useState<'form' | 'json'>('form')
  const [preconditions, setPreconditions] = useState('')
  const [missingInput, setMissingInput] = useState('')
  const [steps, setSteps] = useState<StepForm[]>([])
  const [assertions, setAssertions] = useState<AssertionForm[]>([])
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    if (!item) return
    const spec = item.spec ?? {}
    setTitle(item.title)
    setDimension(item.dimension)
    setVerificationMode(item.verification_mode || (item.case_type === 'web' ? 'frontend' : 'api'))
    setPriority(item.priority)
    setJson(safeJson(spec))
    setPreconditions((spec.preconditions ?? []).map((value: any) => String(value)).join('\n'))
    setMissingInput(String(spec.missing_input ?? item.missing_input ?? ''))
    setSteps((spec.steps ?? []).filter((step: any) => step && typeof step === 'object').map(toStepForm))
    setAssertions(
      (spec.assertions ?? []).filter((assertion: any) => assertion && typeof assertion === 'object').map(toAssertionForm),
    )
    setMode('form')
  }, [item])

  function buildSpec(): Record<string, any> {
    if (mode === 'json') return JSON.parse(json)
    const builtSteps = steps.map((step) => {
      const next: Record<string, any> = { ...step.raw, action: step.action, name: step.name, method: step.method, path: step.path }
      const body = parseLooseJson(step.bodyText)
      if (body === undefined) delete next.body
      else next.body = body
      const save = parseLooseJson(step.saveText)
      if (save === undefined) delete next.save
      else next.save = save
      return next
    })
    const builtAssertions = assertions.map((assertion) => {
      const expected = parseLooseJson(assertion.expectedText)
      const next: Record<string, any> = {
        ...assertion.raw,
        kind: assertion.kind,
        target: assertion.target,
        operator: assertion.operator,
        status: assertion.status,
        evidence: {
          source_type: assertion.sourceType,
          ref: assertion.ref,
          quote: assertion.quote,
        },
      }
      if (expected === undefined) delete next.expected
      else next.expected = expected
      return next
    })
    return {
      ...(item?.spec ?? {}),
      preconditions: preconditions.split('\n').map((line) => line.trim()).filter(Boolean),
      steps: builtSteps,
      assertions: builtAssertions,
      missing_input: missingInput.trim() || undefined,
    }
  }

  function switchMode(next: 'form' | 'json') {
    if (next === mode) return
    if (next === 'json') {
      try {
        setJson(JSON.stringify(buildSpec(), null, 2))
      } catch (error) {
        toast.error('当前表单无法转换为 JSON', String(error))
        return
      }
    } else {
      try {
        const spec = JSON.parse(json)
        setPreconditions((spec.preconditions ?? []).map((value: any) => String(value)).join('\n'))
        setMissingInput(String(spec.missing_input ?? ''))
        setSteps((spec.steps ?? []).filter((step: any) => step && typeof step === 'object').map(toStepForm))
        setAssertions(
          (spec.assertions ?? [])
            .filter((assertion: any) => assertion && typeof assertion === 'object')
            .map(toAssertionForm),
        )
      } catch (error) {
        toast.error('JSON 格式不合法，无法切换表单', String(error))
        return
      }
    }
    setMode(next)
  }

  function updateStep(index: number, patch: Partial<StepForm>) {
    setSteps((prev) => prev.map((step, idx) => (idx === index ? { ...step, ...patch } : step)))
  }

  function updateAssertion(index: number, patch: Partial<AssertionForm>) {
    setAssertions((prev) =>
      prev.map((assertion, idx) => (idx === index ? { ...assertion, ...patch } : assertion)),
    )
  }

  async function save() {
    if (!item) return
    let spec: Record<string, any>
    try {
      spec = buildSpec()
    } catch (error) {
      toast.error(mode === 'json' ? 'JSON 格式不合法' : '表单内容不合法', String(error))
      return
    }
    if (!title.trim()) {
      toast.error('标题不能为空')
      return
    }
    setBusy(true)
    try {
      if (item.id === 0) {
        await api.post(`/projects/${item.project_id}/cases`, {
          title: title.trim(),
          dimension,
          case_type: item.case_type || 'api',
          priority,
          tags: item.tags ?? [],
          status: 'pending',
          verification_mode: verificationMode,
          spec: { ...spec, title: title.trim(), dimension, type: item.case_type || 'api' },
        })
      } else {
        await api.patch(`/cases/${item.id}`, {
          title,
          dimension,
          priority,
          verification_mode: verificationMode,
          spec: { ...spec, title, dimension },
        })
      }
      onSaved()
    } catch (error) {
      toast.error(item.id === 0 ? '创建失败' : '保存失败', String(error))
    } finally {
      setBusy(false)
    }
  }

  const stepAssertionCount = steps.reduce(
    (count, step) => count + (Array.isArray(step.raw?.assert) ? step.raw.assert.length : 0),
    0,
  )

  return (
    <Modal
      open={!!item}
      onClose={onClose}
      title={item?.id ? '编辑用例' : '新建用例'}
      subtitle="结构化编辑步骤和断言；新建后用高级字段可切到 JSON。保存前会做 schema 校验。"
      size="lg"
      footer={
        <>
          <div className="spacer" />
          <Button onClick={onClose}>取消</Button>
          <Button variant="primary" loading={busy} onClick={save}>保存</Button>
        </>
      }
    >
      <div className="stack">
        <div className="segmented segmented--sm">
          <button className={cn('segmented__item', mode === 'form' && 'is-active')} onClick={() => switchMode('form')}>
            结构化编辑
          </button>
          <button className={cn('segmented__item', mode === 'json' && 'is-active')} onClick={() => switchMode('json')}>
            高级 JSON
          </button>
        </div>

        <Field label="标题">
          <input className="input" value={title} onChange={(event) => setTitle(event.target.value)} />
        </Field>
        <div className="form-row">
          <Field label="维度">
            <select className="input" value={dimension} onChange={(event) => setDimension(event.target.value)}>
              {Object.entries(DIMENSION).map(([key, value]) => (
                <option key={key} value={key}>{value.label}</option>
              ))}
            </select>
          </Field>
          <Field label="优先级" hint="数字越小越先执行">
            <input
              className="input"
              type="number"
              value={priority}
              onChange={(event) => setPriority(Number(event.target.value))}
            />
          </Field>
        </div>
        <Field label="验证方式" hint="决定自动验证时走 API、进程、构建产物、浏览器还是人工。">
          <select className="input" value={verificationMode} onChange={(event) => setVerificationMode(event.target.value)}>
            {Object.entries(CASE_VERIFICATION_MODE).map(([key, label]) => (
              <option key={key} value={key}>{label}</option>
            ))}
          </select>
        </Field>

        {mode === 'json' ? (
          <Field label="用例 JSON" hint="steps / assertions / evidence / preconditions；保存前会做 schema 校验。">
            <textarea className="input code-input" rows={20} value={json} onChange={(event) => setJson(event.target.value)} />
          </Field>
        ) : (
          <>
            <Field label="前置条件" hint="每行一条">
              <textarea className="input" rows={2} value={preconditions} onChange={(event) => setPreconditions(event.target.value)} />
            </Field>
            <Field
              label="待补全说明（missing_input）"
              hint="仅作为说明记录，不影响有步骤的用例执行；没有步骤的用例会被排除在执行集之外。"
            >
              <input className="input" value={missingInput} onChange={(event) => setMissingInput(event.target.value)} />
            </Field>

            <section className="detail-section">
              <h3>步骤 <span className="detail-section__count">{steps.length}</span></h3>
              <div className="stack">
                {steps.map((step, index) => (
                  <div key={index} className="case-form-card">
                    <div className="case-form-card__head">
                      <strong>步骤 {index + 1}</strong>
                      <Button size="sm" variant="ghost" icon="trash" onClick={() => setSteps((prev) => prev.filter((_, i) => i !== index))}>
                        删除
                      </Button>
                    </div>
                    <div className="form-row">
                      <Field label="动作">
                        <select className="input" value={step.action} onChange={(event) => updateStep(index, { action: event.target.value })}>
                          <option value="request">request</option>
                          <option value="browser">browser</option>
                          <option value="db">db</option>
                          <option value="sleep">sleep</option>
                        </select>
                      </Field>
                      <Field label="名称">
                        <input className="input" value={step.name} onChange={(event) => updateStep(index, { name: event.target.value })} />
                      </Field>
                    </div>
                    <div className="form-row">
                      <Field label="方法">
                        <input className="input mono" value={step.method} onChange={(event) => updateStep(index, { method: event.target.value })} />
                      </Field>
                      <Field label="路径">
                        <input className="input mono" value={step.path} onChange={(event) => updateStep(index, { path: event.target.value })} />
                      </Field>
                    </div>
                    <Field label="请求体 JSON / 文本">
                      <textarea className="input code-input" rows={3} value={step.bodyText} onChange={(event) => updateStep(index, { bodyText: event.target.value })} />
                    </Field>
                    <Field label="保存变量 JSON" hint='例如 {"user_id": "body.id"}'>
                      <textarea className="input code-input" rows={2} value={step.saveText} onChange={(event) => updateStep(index, { saveText: event.target.value })} />
                    </Field>
                  </div>
                ))}
                <Button
                  size="sm"
                  icon="plus"
                  onClick={() =>
                    setSteps((prev) => [
                      ...prev,
                      { raw: {}, action: 'request', name: '', method: 'GET', path: '', bodyText: '', saveText: '' },
                    ])
                  }
                >
                  添加步骤
                </Button>
              </div>
            </section>

            <section className="detail-section">
              <h3>断言 <span className="detail-section__count">{assertions.length + stepAssertionCount}</span></h3>
              {stepAssertionCount > 0 && (
                <p className="muted small">
                  其中 {stepAssertionCount} 条是步骤内联断言，结构化表单暂不单独列出；切到「高级 JSON」可修改。
                </p>
              )}
              <div className="stack">
                {assertions.map((assertion, index) => (
                  <div key={index} className="case-form-card">
                    <div className="case-form-card__head">
                      <strong>断言 {index + 1}</strong>
                      <Button size="sm" variant="ghost" icon="trash" onClick={() => setAssertions((prev) => prev.filter((_, i) => i !== index))}>
                        删除
                      </Button>
                    </div>
                    <div className="form-row">
                      <Field label="类型">
                        <select className="input" value={assertion.kind} onChange={(event) => updateAssertion(index, { kind: event.target.value })}>
                          <option value="status_code">status_code</option>
                          <option value="json_path">json_path</option>
                          <option value="db_state">db_state</option>
                          <option value="cross_api">cross_api</option>
                          <option value="timing">timing</option>
                          <option value="text_contains">text_contains</option>
                        </select>
                      </Field>
                      <Field label="目标">
                        <input className="input mono" value={assertion.target} onChange={(event) => updateAssertion(index, { target: event.target.value })} />
                      </Field>
                      <Field label="操作符">
                        <input className="input mono" value={assertion.operator} onChange={(event) => updateAssertion(index, { operator: event.target.value })} />
                      </Field>
                    </div>
                    <div className="form-row">
                      <Field label="期望值" hint="支持 JSON / 文本">
                        <textarea className="input code-input" rows={2} value={assertion.expectedText} onChange={(event) => updateAssertion(index, { expectedText: event.target.value })} />
                      </Field>
                      <Field label="状态">
                        <select className="input" value={assertion.status} onChange={(event) => updateAssertion(index, { status: event.target.value })}>
                          <option value="pending">待确认</option>
                          <option value="confirmed">已确认</option>
                        </select>
                      </Field>
                    </div>
                    <div className="form-row">
                      <Field label="依据类型">
                        <select className="input" value={assertion.sourceType} onChange={(event) => updateAssertion(index, { sourceType: event.target.value })}>
                          <option value="manual">manual</option>
                          <option value="doc">doc</option>
                          <option value="openapi">openapi</option>
                          <option value="code">code</option>
                        </select>
                      </Field>
                      <Field label="依据位置">
                        <input className="input mono" value={assertion.ref} onChange={(event) => updateAssertion(index, { ref: event.target.value })} />
                      </Field>
                      <Field label="原文片段">
                        <input className="input" value={assertion.quote} onChange={(event) => updateAssertion(index, { quote: event.target.value })} />
                      </Field>
                    </div>
                  </div>
                ))}
                <Button
                  size="sm"
                  icon="plus"
                  onClick={() =>
                    setAssertions((prev) => [
                      ...prev,
                      {
                        raw: {},
                        kind: 'status_code',
                        target: '',
                        operator: 'eq',
                        expectedText: '',
                        status: 'pending',
                        sourceType: 'manual',
                        ref: '',
                        quote: '',
                      },
                    ])
                  }
                >
                  添加断言
                </Button>
              </div>
            </section>
          </>
        )}
      </div>
    </Modal>
  )
}
