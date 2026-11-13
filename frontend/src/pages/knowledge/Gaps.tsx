import { useEffect, useMemo, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { api, apiUrl, qs } from '../../lib/api'
import { formatFullTime, safeJson } from '../../lib/format'
import { useAsync } from '../../lib/hooks'
import {
  CASE_STATUS,
  DIMENSION,
  EVIDENCE_SOURCE,
  EXECUTION_STATE,
  GAP_OWNER_KIND,
  GAP_STATUS,
  MISSING_INPUT_KIND,
  meta,
} from '../../lib/labels'
import type {
  AssertionGroupsResponse,
  AssertionMigrationPreview,
  CaseGap,
  CaseGapAudit,
  CaseGapBatchPreview,
  CaseGapResponse,
  Feature,
  ProbeGeneratePreview,
  ReevaluatePreview,
  SlaRecomputePreview,
} from '../../lib/types'
import { Icon } from '../../ui/icons'
import { Button, Card, EmptyState, ErrorBox, Field, Pill, Skeleton, Status, cn } from '../../ui/kit'
import { Modal, useToast } from '../../ui/overlays'

type Filter = 'all' | 'pending' | 'questions' | 'no_steps' | 'missing_input'

const FILTERS: Array<{ key: Filter; label: string }> = [
  { key: 'all', label: '全部缺口' },
  { key: 'pending', label: '待确认断言' },
  { key: 'questions', label: '待确认问题' },
  { key: 'no_steps', label: '没有步骤' },
  { key: 'missing_input', label: '待补全说明' },
]

const GAP_STATUS_OPTIONS = [
  { value: 'open', label: '待处理' },
  { value: 'claimed', label: '已认领' },
  { value: 'fixed', label: '已修复，待复检' },
  { value: 'accepted', label: '接受风险' },
  { value: 'wont_fix', label: '不修复' },
]

const GROUP_BY_OPTIONS = [
  { value: 'endpoint', label: '按接口' },
  { value: 'kind', label: '按断言类型' },
  { value: 'evidence_source', label: '按证据来源' },
  { value: 'feature', label: '按功能点' },
  { value: 'owner_kind', label: '按归属' },
]

function selectionKey(caseId: number, index: number) {
  return `${caseId}:${index}`
}

export default function Gaps() {
  const { projectId } = useParams()
  const toast = useToast()

  const [view, setView] = useState<'cases' | 'assertions'>('cases')
  const [filter, setFilter] = useState<Filter>('all')
  const [includeClosed, setIncludeClosed] = useState(false)
  const [limit, setLimit] = useState(50)

  const [dimension, setDimension] = useState('')
  const [featureId, setFeatureId] = useState('')
  const [evidenceSource, setEvidenceSource] = useState('')
  const [ownerKind, setOwnerKind] = useState('')
  const [missingInputKind, setMissingInputKind] = useState('')
  const [ownerQuery, setOwnerQuery] = useState('')
  const [executionState, setExecutionState] = useState('')

  const [selected, setSelected] = useState<Set<number>>(new Set())
  const [batchStatus, setBatchStatus] = useState('accepted')
  const [batchOwner, setBatchOwner] = useState('')
  const [batchNote, setBatchNote] = useState('')
  const [batchPreview, setBatchPreview] = useState<CaseGapBatchPreview | null>(null)
  const [batchBusy, setBatchBusy] = useState(false)

  const [disposition, setDisposition] = useState<{ item: CaseGap; status: string } | null>(null)
  const [dispositionOwner, setDispositionOwner] = useState('')
  const [dispositionNote, setDispositionNote] = useState('')
  const [auditCase, setAuditCase] = useState<CaseGap | null>(null)
  const [audits, setAudits] = useState<CaseGapAudit[] | null>(null)
  const [saving, setSaving] = useState(false)

  const [slaPreview, setSlaPreview] = useState<SlaRecomputePreview | null>(null)
  const [reevalPreview, setReevalPreview] = useState<ReevaluatePreview | null>(null)
  const [enrichPreview, setEnrichPreview] = useState<Record<string, any> | null>(null)
  const [migrationPreview, setMigrationPreview] = useState<AssertionMigrationPreview | null>(null)
  const [probePreview, setProbePreview] = useState<ProbeGeneratePreview | null>(null)
  const [reviewBusy, setReviewBusy] = useState(false)

  const [groupBy, setGroupBy] = useState('endpoint')
  const [groupSelection, setGroupSelection] = useState<Set<string>>(new Set())
  const [groupNote, setGroupNote] = useState('')
  const [groupBusy, setGroupBusy] = useState(false)

  const filters = useMemo(
    () => ({
      kind: filter,
      include_closed: includeClosed,
      dimension,
      feature_id: featureId ? Number(featureId) : undefined,
      evidence_source: evidenceSource,
      owner: ownerQuery.trim(),
      missing_input_kind: missingInputKind,
      owner_kind: ownerKind,
      execution_state: executionState,
    }),
    [
      filter,
      includeClosed,
      dimension,
      featureId,
      evidenceSource,
      ownerQuery,
      missingInputKind,
      ownerKind,
      executionState,
    ],
  )

  const gaps = useAsync<CaseGapResponse>(
    () =>
      api.get(
        `/projects/${projectId}/case-gaps${qs({
          kind: filters.kind,
          include_closed: filters.include_closed ? 'true' : undefined,
          dimension: filters.dimension,
          feature_id: filters.feature_id,
          evidence_source: filters.evidence_source,
          owner: filters.owner,
          missing_input_kind: filters.missing_input_kind,
          owner_kind: filters.owner_kind,
          execution_state: filters.execution_state,
        })}`,
      ),
    [
      projectId,
      filters.kind,
      filters.include_closed,
      filters.dimension,
      filters.feature_id,
      filters.evidence_source,
      filters.owner,
      filters.missing_input_kind,
      filters.owner_kind,
      filters.execution_state,
    ],
  )
  const features = useAsync<Feature[]>(() => api.get(`/projects/${projectId}/features`), [projectId])
  const groups = useAsync<AssertionGroupsResponse>(
    () =>
      api.get(
        `/projects/${projectId}/case-gaps/assertions/groups${qs({
          group_by: groupBy,
          include_closed: includeClosed ? 'true' : undefined,
        })}`,
      ),
    [projectId, groupBy, includeClosed],
  )

  const summary = gaps.data?.summary
  const facets = gaps.data?.facets
  const items = gaps.data?.items ?? []
  const visible = items.slice(0, limit)
  const visibleIds = useMemo(() => new Set(visible.map((item) => item.case_id)), [visible])
  const selectedVisible = useMemo(
    () => [...selected].filter((caseId) => visibleIds.has(caseId)),
    [selected, visibleIds],
  )

  useEffect(() => {
    if (!auditCase) {
      setAudits(null)
      return
    }
    let alive = true
    api
      .get<{ items: CaseGapAudit[] }>(`/projects/${projectId}/case-gaps/${auditCase.case_id}/audits`)
      .then((data) => {
        if (alive) setAudits(data.items)
      })
      .catch(() => {
        if (alive) setAudits([])
      })
    return () => {
      alive = false
    }
  }, [auditCase, projectId])

  async function enrich(caseId: number) {
    try {
      await api.post(`/cases/${caseId}/enrich`)
      toast.success('已排队 AI 补全用例', `用例 #${caseId} · 只补用例，不改文档；完成后刷新本页查看`)
    } catch (error) {
      toast.error('AI 补全没有排上', String(error))
    }
  }

  function toggleSelected(caseId: number) {
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(caseId)) next.delete(caseId)
      else next.add(caseId)
      return next
    })
  }

  function toggleAllVisible() {
    setSelected((prev) => {
      const allSelected = visible.length > 0 && visible.every((item) => prev.has(item.case_id))
      if (allSelected) return new Set()
      return new Set(visible.map((item) => item.case_id))
    })
  }

  async function saveDisposition() {
    if (!disposition) return
    setSaving(true)
    try {
      await api.patch(`/projects/${projectId}/case-gaps/${disposition.item.case_id}`, {
        status: disposition.status,
        owner: dispositionOwner,
        note: dispositionNote,
      })
      toast.success('缺口状态已更新', `用例 #${disposition.item.case_id}`)
      setDisposition(null)
      gaps.reload()
      groups.reload()
    } catch (error) {
      toast.error('更新缺口状态失败', String(error))
    } finally {
      setSaving(false)
    }
  }

  function openDisposition(item: CaseGap, status: string) {
    setDisposition({ item, status })
    setDispositionOwner(item.gap_owner || '')
    setDispositionNote(item.gap_note || '')
  }

  async function previewBatch() {
    setBatchBusy(true)
    try {
      const preview = await api.post<CaseGapBatchPreview>(`/projects/${projectId}/case-gaps/batch`, {
        status: batchStatus,
        filters,
        case_ids: selectedVisible.length ? selectedVisible : undefined,
        owner: batchOwner.trim() || undefined,
        note: batchNote.trim() || undefined,
        dry_run: true,
      })
      setBatchPreview(preview)
    } catch (error) {
      toast.error('预览批量处置失败', String(error))
    } finally {
      setBatchBusy(false)
    }
  }

  async function applyBatch() {
    if (!batchPreview) return
    setBatchBusy(true)
    try {
      const result = await api.post<{ matched: number; updated: number }>(
        `/projects/${projectId}/case-gaps/batch`,
        {
          status: batchStatus,
          filters,
          case_ids: selectedVisible.length ? selectedVisible : undefined,
          owner: batchOwner.trim() || undefined,
          note: batchNote.trim() || undefined,
          dry_run: false,
        },
      )
      toast.success('批量处置完成', `影响 ${result.updated} 条用例`)
      setBatchPreview(null)
      setSelected(new Set())
      gaps.reload()
      groups.reload()
    } catch (error) {
      toast.error('批量处置失败', String(error))
    } finally {
      setBatchBusy(false)
    }
  }

  async function openSlaPreview() {
    setReviewBusy(true)
    try {
      setSlaPreview(await api.get<SlaRecomputePreview>(`/projects/${projectId}/cases/sla-recompute`))
    } catch (error) {
      toast.error('读取 SLA 重算预览失败', String(error))
    } finally {
      setReviewBusy(false)
    }
  }

  async function applySla() {
    setReviewBusy(true)
    try {
      const result = await api.post<{ applied: number; skipped: unknown[] }>(
        `/projects/${projectId}/cases/sla-recompute`,
        { dry_run: false },
      )
      toast.success('SLA 重算完成', `更新 ${result.applied} 条用例${result.skipped.length ? `，跳过 ${result.skipped.length} 条` : ''}`)
      setSlaPreview(null)
      gaps.reload()
      groups.reload()
    } catch (error) {
      toast.error('SLA 重算失败', String(error))
    } finally {
      setReviewBusy(false)
    }
  }

  async function openEnrichPreview() {
    setReviewBusy(true)
    try {
      setEnrichPreview(
        await api.post<Record<string, any>>(`/projects/${projectId}/cases/enrich`, {
          dry_run: true,
          filters,
          case_ids: selectedVisible.length ? selectedVisible : undefined,
          limit: 200,
        }),
      )
    } catch (error) {
      toast.error('读取重新评估预览失败', String(error))
    } finally {
      setReviewBusy(false)
    }
  }

  async function applyEnrich() {
    setReviewBusy(true)
    try {
      const result = await api.post<Record<string, any>>(`/projects/${projectId}/cases/enrich`, {
        dry_run: false,
        filters,
        case_ids: selectedVisible.length ? selectedVisible : undefined,
        limit: 200,
      })
      toast.success(
        '已提交重新评估',
        `入队 ${result.enqueued ?? 0} 条${result.skipped_active ? `，跳过 ${result.skipped_active} 条正在执行的任务` : ''}`,
      )
      setEnrichPreview(null)
      gaps.reload()
    } catch (error) {
      toast.error('提交重新评估失败', String(error))
    } finally {
      setReviewBusy(false)
    }
  }

  async function openReevalPreview() {
    setReviewBusy(true)
    try {
      setReevalPreview(await api.get<ReevaluatePreview>(`/projects/${projectId}/cases/reevaluate`))
    } catch (error) {
      toast.error('读取重评估预览失败', String(error))
    } finally {
      setReviewBusy(false)
    }
  }

  async function applyReeval() {
    setReviewBusy(true)
    try {
      const result = await api.post<{ processed: number; confirmed_assertions: number; unlocked_cases: number[] }>(
        `/projects/${projectId}/cases/reevaluate`,
        { dry_run: false },
      )
      toast.success(
        '重评估完成',
        `处理 ${result.processed} 条用例，确认 ${result.confirmed_assertions} 条断言${result.unlocked_cases.length ? `，解锁 ${result.unlocked_cases.length} 条` : ''}`,
      )
      setReevalPreview(null)
      gaps.reload()
      groups.reload()
    } catch (error) {
      toast.error('重评估失败', String(error))
    } finally {
      setReviewBusy(false)
    }
  }

  async function openMigrationPreview() {
    setReviewBusy(true)
    try {
      setMigrationPreview(await api.get<AssertionMigrationPreview>(`/projects/${projectId}/cases/assertions/migrate`))
    } catch (error) {
      toast.error('读取断言迁移预览失败', String(error))
    } finally {
      setReviewBusy(false)
    }
  }

  async function applyMigration() {
    setReviewBusy(true)
    try {
      const result = await api.post<{ applied: number; to_policy: number; to_question: number }>(
        `/projects/${projectId}/cases/assertions/migrate`,
        { dry_run: false },
      )
      toast.success(
        '无依据断言已迁移',
        `处理 ${result.applied} 条用例：${result.to_policy} 条转通用策略检查，${result.to_question} 条转待确认问题`,
      )
      setMigrationPreview(null)
      gaps.reload()
      groups.reload()
    } catch (error) {
      toast.error('断言迁移失败', String(error))
    } finally {
      setReviewBusy(false)
    }
  }

  async function openProbePreview() {
    setReviewBusy(true)
    try {
      setProbePreview(
        await api.post<ProbeGeneratePreview>(`/projects/${projectId}/probes/generate`, { dry_run: true }),
      )
    } catch (error) {
      toast.error('读取 probe 预览失败', String(error))
    } finally {
      setReviewBusy(false)
    }
  }

  async function applyProbes() {
    setReviewBusy(true)
    try {
      const result = await api.post<ProbeGeneratePreview>(`/projects/${projectId}/probes/generate`, {
        dry_run: false,
      })
      toast.success('通用 probe 已生成', `新增 ${result.created ?? 0} 条待审核用例（只挂通用策略检查）`)
      setProbePreview(null)
      gaps.reload()
    } catch (error) {
      toast.error('生成 probe 失败', String(error))
    } finally {
      setReviewBusy(false)
    }
  }

  function toggleGroupAssertion(key: string, checked: boolean) {
    setGroupSelection((prev) => {
      const next = new Set(prev)
      if (checked) next.add(key)
      else next.delete(key)
      return next
    })
  }

  function toggleGroup(group: AssertionGroupsResponse['groups'][number]) {
    const keys = group.assertions.filter((item) => item.confirmable).map((item) => selectionKey(item.case_id, item.index))
    const allSelected = keys.length > 0 && keys.every((key) => groupSelection.has(key))
    setGroupSelection((prev) => {
      const next = new Set(prev)
      keys.forEach((key) => (allSelected ? next.delete(key) : next.add(key)))
      return next
    })
  }

  async function confirmSelected() {
    const selections = [...groupSelection].map((key) => {
      const [caseId, index] = key.split(':').map(Number)
      return { case_id: caseId, index }
    })
    if (!selections.length) {
      toast.info('请先选择要确认的断言', '只有带 openapi / code / doc 依据的断言可批量确认')
      return
    }
    setGroupBusy(true)
    try {
      const result = await api.post<{ confirmed_count: number; skipped: unknown[] }>(
        `/projects/${projectId}/case-gaps/assertions/confirm`,
        { selections, note: groupNote.trim() },
      )
      toast.success('批量确认完成', `确认 ${result.confirmed_count} 条${result.skipped.length ? `，跳过 ${result.skipped.length} 条（无依据或已确认）` : ''}`)
      setGroupSelection(new Set())
      gaps.reload()
      groups.reload()
    } catch (error) {
      toast.error('批量确认失败', String(error))
    } finally {
      setGroupBusy(false)
    }
  }

  async function acceptRisk(caseIds: number[]) {
    if (!caseIds.length) return
    setGroupBusy(true)
    try {
      const result = await api.post<{ updated: number }>(
        `/projects/${projectId}/case-gaps/assertions/accept-risk`,
        { case_ids: caseIds, note: groupNote.trim() },
      )
      toast.success('已接受风险', `${result.updated} 条用例的缺口已标记为 accepted`)
      gaps.reload()
      groups.reload()
    } catch (error) {
      toast.error('接受风险失败', String(error))
    } finally {
      setGroupBusy(false)
    }
  }

  const selectedGroupCaseIds = useMemo(() => {
    const ids = new Set<number>()
    ;(groups.data?.groups ?? []).forEach((group) => {
      group.assertions.forEach((item) => {
        if (groupSelection.has(selectionKey(item.case_id, item.index))) ids.add(item.case_id)
      })
    })
    return [...ids]
  }, [groups.data, groupSelection])

  return (
    <div className="stack">
      <div className="section-bar">
        <div className="section-bar__stats">
          <span><strong>{summary?.cases ?? 0}</strong> 条用例有缺口</span>
          <span className="text-warning"><strong>{summary?.pending_assertions ?? 0}</strong> 条待确认断言</span>
          <span><strong>{summary?.contract_questions ?? 0}</strong> 个待确认问题</span>
          <span><strong>{summary?.cases_without_steps ?? 0}</strong> 条没有步骤</span>
          <span><strong>{summary?.cases_with_missing_input ?? 0}</strong> 条有待补全说明</span>
          <span><strong>{(summary?.by_status?.accepted ?? 0) + (summary?.by_status?.wont_fix ?? 0)}</strong> 条已关闭</span>
        </div>
        <div className="section-bar__actions">
          <label className="checkbox-label">
            <input type="checkbox" checked={includeClosed} onChange={(event) => setIncludeClosed(event.target.checked)} />
            显示已关闭
          </label>
          <a className="btn btn--secondary btn--sm" href={apiUrl(`/projects/${projectId}/case-gaps/export.md`)}>
            <Icon name="download" size={14} /> 导出全部缺口
          </a>
          <a
            className="btn btn--secondary btn--sm"
            href={apiUrl(`/projects/${projectId}/case-gaps/export.md${qs({ owner_kind: 'sut-contract', include_closed: includeClosed ? 'true' : undefined })}`)}
          >
            <Icon name="download" size={14} /> 导出被测方契约缺口
          </a>
          <Button size="sm" icon="refresh" loading={gaps.loading} onClick={gaps.reload}>刷新</Button>
        </div>
      </div>

      <div className="notice notice--info">
        <Icon name="info" size={16} />
        <span>
          待确认断言只记录、不判失败，代表「缺少可引用的契约 / 文档依据」，不是产品缺陷。
          <strong>待确认问题</strong>没有期望值、不进入断言统计，等于「先请被测方回答期望行为」。
          缺口按归属分流：<strong>测试侧资产</strong>留在 Shakedown 内部处理，<strong>被测方契约</strong>才会导出给被测系统维护者，
          <strong>环境</strong>缺口由测试环境负责人处理。Shakedown 不会直接修改被测系统的 OpenAPI / 文档 / 代码。
        </span>
      </div>

      <Card>
        <div className="stack">
          <div className="form-row">
            <Field label="缺口类型">
              <select className="input" value={filter} onChange={(event) => { setFilter(event.target.value as Filter); setLimit(50) }}>
                {FILTERS.map((item) => (
                  <option key={item.key} value={item.key}>{item.label}</option>
                ))}
              </select>
            </Field>
            <Field label="维度">
              <select className="input" value={dimension} onChange={(event) => setDimension(event.target.value)}>
                <option value="">全部维度</option>
                {(Object.keys(DIMENSION) as string[]).map((key) => (
                  <option key={key} value={key}>{meta(DIMENSION, key).label}</option>
                ))}
              </select>
            </Field>
            <Field label="功能点">
              <select className="input" value={featureId} onChange={(event) => setFeatureId(event.target.value)}>
                <option value="">全部功能点</option>
                {(features.data ?? []).map((feature) => (
                  <option key={feature.id} value={feature.id}>{feature.name}</option>
                ))}
              </select>
            </Field>
            <Field label="证据来源">
              <select className="input" value={evidenceSource} onChange={(event) => setEvidenceSource(event.target.value)}>
                <option value="">全部来源</option>
                {(Object.keys(EVIDENCE_SOURCE) as string[]).map((key) => (
                  <option key={key} value={key}>{meta(EVIDENCE_SOURCE, key).label}</option>
                ))}
              </select>
            </Field>
          </div>
          <div className="form-row">
            <Field label="缺输入分类">
              <select className="input" value={missingInputKind} onChange={(event) => setMissingInputKind(event.target.value)}>
                <option value="">全部分类</option>
                {Object.entries(facets?.by_missing_input_kind ?? {}).map(([key, count]) => (
                  <option key={key} value={key}>{meta(MISSING_INPUT_KIND, key).label}（{count}）</option>
                ))}
              </select>
            </Field>
            <Field label="执行状态">
              <select className="input" value={executionState} onChange={(event) => setExecutionState(event.target.value)}>
                <option value="">全部状态</option>
                <option value="never_executed">从未执行</option>
                <option value="executed">已执行</option>
                <option value="enabled_no_result">已启用但无结果</option>
                <option value="pending_not_enabled">待审核未启用</option>
                <option value="stale_after_source">知识源更新后未复核</option>
              </select>
            </Field>
            <Field label="归属">
              <select className="input" value={ownerKind} onChange={(event) => setOwnerKind(event.target.value)}>
                <option value="">全部归属</option>
                {Object.entries(facets?.by_owner_kind ?? {}).map(([key, count]) => (
                  <option key={key} value={key}>{meta(GAP_OWNER_KIND, key).label}（{count}）</option>
                ))}
              </select>
            </Field>
            <Field label="负责人">
              <input className="input" value={ownerQuery} onChange={(event) => setOwnerQuery(event.target.value)} placeholder="按负责人筛选" />
            </Field>
            <Field label="用例动作">
              <div className="row-actions">
                <Button size="sm" icon="clock" loading={reviewBusy} onClick={openSlaPreview}>按 SLA 重算</Button>
                <Button size="sm" icon="refresh" loading={reviewBusy} onClick={openReevalPreview}>按 spec 重评估</Button>
                <Button size="sm" icon="zap" loading={reviewBusy} onClick={openEnrichPreview}>重新评估缺口用例</Button>
                <Button size="sm" icon="shield" loading={reviewBusy} onClick={openMigrationPreview}>清理无依据断言</Button>
                <Button size="sm" icon="zap" loading={reviewBusy} onClick={openProbePreview}>生成通用 probe</Button>
              </div>
            </Field>
          </div>
        </div>
      </Card>

      <div className="segmented segmented--sm">
        <button className={cn('segmented__item', view === 'cases' && 'is-active')} onClick={() => setView('cases')}>
          用例缺口
        </button>
        <button className={cn('segmented__item', view === 'assertions' && 'is-active')} onClick={() => setView('assertions')}>
          待确认断言分组
        </button>
      </div>

      {gaps.error && <ErrorBox>{gaps.error}</ErrorBox>}
      {gaps.loading && !gaps.data && <Card><Skeleton lines={6} /></Card>}

      {view === 'cases' && (
        <>
          <Card>
            <div className="stack">
              <div className="row-actions">
                <label className="checkbox-label">
                  <input
                    type="checkbox"
                    checked={visible.length > 0 && visible.every((item) => selected.has(item.case_id))}
                    onChange={toggleAllVisible}
                  />
                  全选当前列表
                </label>
                <span className="muted small">已选 {selectedVisible.length} 条</span>
              </div>
              <div className="form-row">
                <Field label="批量设置为">
                  <select className="input" value={batchStatus} onChange={(event) => setBatchStatus(event.target.value)}>
                    {GAP_STATUS_OPTIONS.map((item) => (
                      <option key={item.value} value={item.value}>{item.label}</option>
                    ))}
                  </select>
                </Field>
                <Field label="负责人" hint="可选；批量填写">
                  <input className="input" value={batchOwner} onChange={(event) => setBatchOwner(event.target.value)} placeholder="如 qa-team" />
                </Field>
                <Field label="备注" hint="处置原因 / 工单号，写入审计">
                  <input className="input" value={batchNote} onChange={(event) => setBatchNote(event.target.value)} />
                </Field>
                <Field label="操作">
                  <Button size="sm" variant="primary" loading={batchBusy} onClick={previewBatch}>
                    预览影响范围
                  </Button>
                </Field>
              </div>
              <span className="muted small">
                未勾选用例时，批量处置会作用于当前筛选条件下的全部缺口；勾选后只作用于已选用例。
              </span>
            </div>
          </Card>

          {!gaps.loading && items.length === 0 && (
            <Card>
              <EmptyState
                icon="check"
                title={filter === 'all' ? '没有需要补全的用例' : '当前筛选下没有缺口'}
                description="所有用例都有可执行步骤，且断言都有可引用依据。"
              />
            </Card>
          )}

          {visible.map((item) => {
            const dim = meta(DIMENSION, item.dimension)
            const gapMeta = meta(GAP_STATUS, item.gap_status)
            const ownerMeta = meta(GAP_OWNER_KIND, item.owner_kind)
            const inputMeta = item.missing_input_kind ? meta(MISSING_INPUT_KIND, item.missing_input_kind) : null
            return (
              <Card key={item.case_id}>
                <div className="gap-card">
                  <div className="gap-card__head">
                    <div className="gap-card__title">
                      <label className="checkbox-label">
                        <input
                          type="checkbox"
                          checked={selected.has(item.case_id)}
                          onChange={() => toggleSelected(item.case_id)}
                        />
                        <Link to={`/projects/${projectId}/cases/${item.case_id}`}>
                          #{item.case_id} {item.title}
                        </Link>
                      </label>
                      <div className="gap-card__meta">
                        <Status value={item.status} map={CASE_STATUS} />
                        <Pill tone={gapMeta.tone}>{gapMeta.label}</Pill>
                        <Pill tone={ownerMeta.tone}>{ownerMeta.label}</Pill>
                        {inputMeta && <Pill tone={inputMeta.tone}>{inputMeta.label}</Pill>}
                        {(item.execution_states ?? []).map((state: string) => {
                          const stateMeta = meta(EXECUTION_STATE, state)
                          return <Pill key={state} tone={stateMeta.tone}>{stateMeta.label}</Pill>
                        })}
                        {item.probe && <Pill tone="info">探针</Pill>}
                        <Pill tone={dim.tone}>{dim.label}</Pill>
                        <span className="muted small">{item.feature_name ?? '未映射功能点'}</span>
                        <span className="muted small">{item.step_count} 步 · {item.assertion_count} 条断言</span>
                        {item.gap_owner && <span className="muted small">负责人：{item.gap_owner}</span>}
                      </div>
                    </div>
                    <div className="gap-card__actions">
                      <Button size="sm" icon="edit" onClick={() => openDisposition(item, item.gap_status)}>
                        处置
                      </Button>
                      <Button size="sm" icon="clock" onClick={() => setAuditCase(item)}>
                        审计
                      </Button>
                      <Link className="btn btn--secondary btn--sm" to={`/projects/${projectId}/cases/${item.case_id}`}>
                        打开用例
                      </Link>
                      <Button
                        size="sm"
                        icon="sparkles"
                        onClick={() => enrich(item.case_id)}
                        title="只补全这条用例的步骤、断言和依据，不会修改 OpenAPI 或文档"
                      >
                        AI 补全用例
                      </Button>
                    </div>
                  </div>

                  {item.gap_note && (
                    <div className="notice notice--info">
                      <Icon name="info" size={15} />
                      <span><strong>处置备注</strong>{item.gap_note}</span>
                    </div>
                  )}

                  {item.missing_input && (
                    <div className="notice notice--info">
                      <Icon name="info" size={15} />
                      <span><strong>待补全说明</strong>{item.missing_input}</span>
                    </div>
                  )}
                  {item.no_steps && (
                    <div className="notice notice--warning">
                      <Icon name="info" size={15} />
                      <span><strong>没有可执行步骤</strong>请先补全接口、参数和请求步骤，否则该用例不会进入执行集。</span>
                    </div>
                  )}

                  {item.pending_assertions.length > 0 && (
                    <div className="gap-assertions">
                      {item.pending_assertions.map((assertion) => (
                        <div className="gap-assertion" key={`${item.case_id}-${assertion.index}`}>
                          <div className="gap-assertion__head">
                            <Pill tone="warning">待确认</Pill>
                            <code>{assertion.kind}</code>
                            {assertion.endpoint && <span className="mono muted small">{assertion.endpoint}</span>}
                          </div>
                          <div className="gap-assertion__desc">
                            {assertion.description || '未填写断言说明'}
                          </div>
                          <div className="gap-assertion__meta">
                            <span>期望 <code>{safeJson(assertion.expected)}</code></span>
                            {assertion.evidence?.ref && (
                              <span>依据 <code>{assertion.evidence.ref}</code></span>
                            )}
                          </div>
                        </div>
                      ))}
                    </div>
                  )}

                  {item.contract_questions.length > 0 && (
                    <div className="gap-assertions">
                      {item.contract_questions.map((question, index) => {
                        const questionOwner = meta(GAP_OWNER_KIND, question.owner_kind)
                        return (
                          <div className="gap-assertion" key={`${item.case_id}-question-${index}`}>
                            <div className="gap-assertion__head">
                              <Pill tone="violet">待确认问题</Pill>
                              <Pill tone={questionOwner.tone}>{questionOwner.label}</Pill>
                              {question.step_index != null && (
                                <span className="mono muted small">步骤 {question.step_index + 1}</span>
                              )}
                            </div>
                            <div className="gap-assertion__desc">{question.question}</div>
                            {question.missing_contract && (
                              <div className="gap-assertion__meta">
                                <span>缺少契约 <code>{question.missing_contract}</code></span>
                              </div>
                            )}
                          </div>
                        )
                      })}
                    </div>
                  )}
                </div>
              </Card>
            )
          })}

          {items.length > limit && (
            <div className="stack" style={{ alignItems: 'center' }}>
              <Button size="sm" onClick={() => setLimit((value) => value + 50)}>
                加载更多（剩余 {items.length - limit} 条）
              </Button>
            </div>
          )}
        </>
      )}

      {view === 'assertions' && (
        <>
          <Card>
            <div className="row-actions">
              <span className="muted small">分组方式</span>
              {GROUP_BY_OPTIONS.map((option) => (
                <button
                  key={option.value}
                  className={cn('btn btn--secondary btn--sm', groupBy === option.value && 'is-active')}
                  onClick={() => setGroupBy(option.value)}
                >
                  {option.label}
                </button>
              ))}
              <div className="spacer" />
              <span className="muted small">
                共 <strong>{groups.data?.total ?? 0}</strong> 条待确认，其中 <strong>{groups.data?.confirmable ?? 0}</strong> 条可批量确认
              </span>
            </div>
          </Card>

          {groups.error && <ErrorBox>{groups.error}</ErrorBox>}
          {groups.loading && !groups.data && <Card><Skeleton lines={5} /></Card>}
          {!groups.loading && (groups.data?.groups.length ?? 0) === 0 && (
            <Card><EmptyState icon="check" title="没有待确认断言" description="当前筛选下所有断言都有可引用依据。" /></Card>
          )}

          {(groups.data?.groups ?? []).map((group) => {
            const confirmableKeys = group.assertions
              .filter((item) => item.confirmable)
              .map((item) => selectionKey(item.case_id, item.index))
            const allSelected = confirmableKeys.length > 0 && confirmableKeys.every((key) => groupSelection.has(key))
            return (
              <Card key={group.key}>
                <div className="stack">
                  <div className="gap-card__head">
                    <div className="gap-card__title">
                      <label className="checkbox-label">
                        <input type="checkbox" checked={allSelected} disabled={!confirmableKeys.length} onChange={() => toggleGroup(group)} />
                        <strong>{group.key}</strong>
                      </label>
                      <div className="gap-card__meta">
                        <span className="muted small">{group.count} 条待确认</span>
                        <Pill tone="success">{group.confirmable} 条有依据</Pill>
                        <Pill tone="muted">{group.count - group.confirmable} 条需人工</Pill>
                      </div>
                    </div>
                    <Button
                      size="sm"
                      onClick={() => acceptRisk([...new Set(group.assertions.map((item) => item.case_id))])}
                      loading={groupBusy}
                    >
                      接受整组风险
                    </Button>
                  </div>
                  <div className="gap-assertions">
                    {group.assertions.map((assertion) => {
                      const key = selectionKey(assertion.case_id, assertion.index)
                      return (
                        <div className="gap-assertion" key={key}>
                          <div className="gap-assertion__head">
                            <label className="checkbox-label">
                              <input
                                type="checkbox"
                                checked={groupSelection.has(key)}
                                disabled={!assertion.confirmable}
                                onChange={(event) => toggleGroupAssertion(key, event.target.checked)}
                              />
                              <Pill tone={assertion.confirmable ? 'success' : 'warning'}>
                                {assertion.confirmable ? '有依据' : '需人工'}
                              </Pill>
                            </label>
                            <code>{assertion.kind}</code>
                            <Link className="mono small" to={`/projects/${projectId}/cases/${assertion.case_id}`}>
                              #{assertion.case_id} {assertion.case_title}
                            </Link>
                          </div>
                          <div className="gap-assertion__desc">
                            {assertion.description || '未填写断言说明'}
                          </div>
                          <div className="gap-assertion__meta">
                            <span>期望 <code>{safeJson(assertion.expected)}</code></span>
                            <span>
                              依据{' '}
                              <code>
                                {assertion.evidence?.source_type || 'none'}
                                {assertion.evidence?.ref ? ` · ${assertion.evidence.ref}` : ''}
                              </code>
                            </span>
                          </div>
                        </div>
                      )
                    })}
                  </div>
                </div>
              </Card>
            )
          })}

          {groupSelection.size > 0 && (
            <Card>
              <div className="stack">
                <div className="row-actions">
                  <span>已选 <strong>{groupSelection.size}</strong> 条断言，涉及 <strong>{selectedGroupCaseIds.length}</strong> 条用例</span>
                  <div className="spacer" />
                  <Button size="sm" variant="primary" loading={groupBusy} onClick={confirmSelected}>
                    批量确认有依据断言
                  </Button>
                  <Button size="sm" loading={groupBusy} onClick={() => acceptRisk(selectedGroupCaseIds)}>
                    已选用例接受风险
                  </Button>
                  <Button size="sm" variant="secondary" onClick={() => setGroupSelection(new Set())}>清空选择</Button>
                </div>
                <Field label="备注" hint="写入断言确认 / 处置审计">
                  <input className="input" value={groupNote} onChange={(event) => setGroupNote(event.target.value)} />
                </Field>
              </div>
            </Card>
          )}
        </>
      )}

      <Modal
        open={!!disposition}
        onClose={() => setDisposition(null)}
        title="处置契约缺口"
        subtitle={disposition ? `#${disposition.item.case_id} ${disposition.item.title}` : ''}
        footer={
          <>
            <div className="spacer" />
            <Button onClick={() => setDisposition(null)}>取消</Button>
            <Button variant="primary" loading={saving} onClick={saveDisposition}>保存</Button>
          </>
        }
      >
        <div className="stack">
          <Field label="状态">
            <select
              className="input"
              value={disposition?.status ?? 'open'}
              onChange={(event) =>
                setDisposition((prev) => (prev ? { ...prev, status: event.target.value } : prev))
              }
            >
              {GAP_STATUS_OPTIONS.map((item) => (
                <option key={item.value} value={item.value}>{item.label}</option>
              ))}
            </select>
          </Field>
          <Field label="负责人" hint="可选；用于交接和筛选">
            <input className="input" value={dispositionOwner} onChange={(event) => setDispositionOwner(event.target.value)} />
          </Field>
          <Field label="备注" hint="接受原因、工单号、计划或复检说明">
            <textarea className="input" rows={4} value={dispositionNote} onChange={(event) => setDispositionNote(event.target.value)} />
          </Field>
          <div className="notice notice--info">
            <Icon name="info" size={15} />
            <span>这里只记录测试方对缺口的处置，不会修改被选用例的 spec，也不会修改被测系统的文档或代码。</span>
          </div>
        </div>
      </Modal>

      <Modal
        open={!!batchPreview}
        onClose={() => setBatchPreview(null)}
        title="确认批量处置"
        subtitle={batchPreview ? `将把 ${batchPreview.matched} 条用例的缺口设为「${meta(GAP_STATUS, batchPreview.status).label}」` : ''}
        footer={
          <>
            <div className="spacer" />
            <Button onClick={() => setBatchPreview(null)}>取消</Button>
            <Button variant="primary" loading={batchBusy} disabled={!batchPreview?.matched} onClick={applyBatch}>
              确认执行（{batchPreview?.matched ?? 0} 条）
            </Button>
          </>
        }
      >
        <div className="stack">
          <div className="notice notice--warning">
            <Icon name="info" size={15} />
            <span>
              将影响 <strong>{batchPreview?.matched ?? 0}</strong> 条用例；只写入缺口处置状态和审计，不修改用例 spec。
              {batchPreview?.owner ? ` 负责人：${batchPreview.owner}。` : ''}
              {batchPreview?.note ? ` 备注：${batchPreview.note}。` : ''}
            </span>
          </div>
          <div className="gap-assertions">
            {(batchPreview?.cases ?? []).map((row) => (
              <div className="gap-assertion" key={row.case_id}>
                <div className="gap-assertion__head">
                  <span className="mono muted small">#{row.case_id}</span>
                  <span>{row.title}</span>
                  <Pill tone={meta(GAP_STATUS, row.gap_status).tone}>{meta(GAP_STATUS, row.gap_status).label}</Pill>
                  <Pill tone={meta(GAP_OWNER_KIND, row.owner_kind).tone}>{meta(GAP_OWNER_KIND, row.owner_kind).label}</Pill>
                </div>
              </div>
            ))}
          </div>
          {(batchPreview?.matched ?? 0) > (batchPreview?.cases.length ?? 0) && (
            <span className="muted small">仅展示前 {batchPreview?.cases.length} 条，实际影响 {batchPreview?.matched} 条。</span>
          )}
        </div>
      </Modal>

      <Modal
        open={!!auditCase}
        onClose={() => setAuditCase(null)}
        title="缺口处置审计"
        subtitle={auditCase ? `#${auditCase.case_id} ${auditCase.title}` : ''}
      >
        <div className="stack">
          {audits === null && <Skeleton lines={3} />}
          {audits && audits.length === 0 && <EmptyState icon="clock" title="还没有处置记录" description="对该用例做一次处置后，这里会记录谁、何时、为什么。" />}
          {(audits ?? []).map((audit) => (
            <div className="gap-assertion" key={audit.id}>
              <div className="gap-assertion__head">
                <Pill tone={meta(GAP_STATUS, audit.to_status).tone}>
                  {meta(GAP_STATUS, audit.from_status).label} → {meta(GAP_STATUS, audit.to_status).label}
                </Pill>
                <span className="muted small">{audit.actor} · {audit.source}</span>
                {audit.created_at && <span className="muted small">{formatFullTime(audit.created_at)}</span>}
              </div>
              <div className="gap-assertion__desc">
                {audit.note || '未填写原因'}
                {audit.owner ? ` · 负责人：${audit.owner}` : ''}
              </div>
            </div>
          ))}
        </div>
      </Modal>

      <Modal
        open={!!slaPreview}
        onClose={() => setSlaPreview(null)}
        title="按项目 SLA 重算性能断言"
        subtitle={slaPreview ? `项目 SLA：${slaPreview.project_sla_ms ?? '未配置'} ms · 受影响 ${slaPreview.count} 条` : ''}
        footer={
          <>
            <div className="spacer" />
            <Button onClick={() => setSlaPreview(null)}>取消</Button>
            <Button
              variant="primary"
              loading={reviewBusy}
              disabled={!slaPreview?.project_sla_ms || !slaPreview?.count}
              onClick={applySla}
            >
              确认重算（{slaPreview?.count ?? 0} 条）
            </Button>
          </>
        }
      >
        <div className="stack">
          {slaPreview?.message && (
            <div className="notice notice--warning">
              <Icon name="info" size={15} />
              <span>{slaPreview.message}</span>
            </div>
          )}
          {(slaPreview?.cases ?? []).map((row) => (
            <div className="gap-assertion" key={row.case_id}>
              <div className="gap-assertion__head">
                <span className="mono muted small">#{row.case_id}</span>
                <span>{row.title}</span>
                {row.reasons.map((reason) => (
                  <Pill key={reason} tone="warning">{reason === 'missing_input' ? '缺少 SLA' : '旧断言过期'}</Pill>
                ))}
              </div>
              <div className="gap-assertion__meta">
                {row.old_assertions.length ? (
                  row.old_assertions.map((assertion, index) => (
                    <span key={index}>旧断言 <code>{assertion.operator ?? 'lt'} {safeJson(assertion.expected)}</code></span>
                  ))
                ) : (
                  <span className="muted small">没有 timing 断言，将按项目 SLA 补充</span>
                )}
                {!row.has_steps && <span className="muted small">没有步骤，会按项目接口补齐一个请求步骤</span>}
              </div>
            </div>
          ))}
          {slaPreview && !slaPreview.cases.length && <span className="muted small">当前没有需要重算的性能用例。</span>}
        </div>
      </Modal>

      <Modal
        open={!!reevalPreview}
        onClose={() => setReevalPreview(null)}
        title="按最新 spec 重评估受影响用例"
        subtitle={reevalPreview ? `快照 ${reevalPreview.snapshot_version || '—'} · 受影响 ${reevalPreview.count} 条` : ''}
        footer={
          <>
            <div className="spacer" />
            <Button onClick={() => setReevalPreview(null)}>取消</Button>
            <Button
              variant="primary"
              loading={reviewBusy}
              disabled={!reevalPreview?.count}
              onClick={applyReeval}
            >
              确认重评估（{reevalPreview?.count ?? 0} 条）
            </Button>
          </>
        }
      >
        <div className="stack">
          {reevalPreview?.message && (
            <div className="notice notice--warning">
              <Icon name="info" size={15} />
              <span>{reevalPreview.message}</span>
            </div>
          )}
          <div className="notice notice--info">
            <Icon name="info" size={15} />
            <span>只处理引用受影响接口的用例，不重新生成整个项目的用例；能由新 schema 确认的 pending 断言会变为 confirmed，其余保留 pending。</span>
          </div>
          {(reevalPreview?.cases ?? []).map((row) => (
            <div className="gap-assertion" key={row.case_id}>
              <div className="gap-assertion__head">
                <span className="mono muted small">#{row.case_id}</span>
                <span>{row.title}</span>
                <Status value={row.status} map={CASE_STATUS} />
              </div>
              <div className="gap-assertion__meta">
                <span>受影响接口 {row.endpoints.join('、')}</span>
                <span>{row.pending_assertions.length} 条 pending 断言</span>
                {row.removed_endpoints.length > 0 && (
                  <span className="text-warning">已移除：{row.removed_endpoints.join('、')}</span>
                )}
              </div>
            </div>
          ))}
          {reevalPreview && !reevalPreview.cases.length && <span className="muted small">没有需要重评估的用例。</span>}
        </div>
      </Modal>

      <Modal
        open={!!enrichPreview}
        onClose={() => setEnrichPreview(null)}
        title="重新评估缺口用例"
        subtitle={
          enrichPreview
            ? `当前筛选影响 ${enrichPreview.count ?? 0} 条用例${
                (enrichPreview.count ?? 0) > 200 ? '；单次最多入队 200 条' : ''
              }`
            : ''
        }
        footer={
          <>
            <div className="spacer" />
            <Button onClick={() => setEnrichPreview(null)}>取消</Button>
            <Button
              variant="primary"
              loading={reviewBusy}
              disabled={!enrichPreview?.count}
              onClick={applyEnrich}
            >
              确认入队（{Math.min(enrichPreview?.count ?? 0, 200)} 条）
            </Button>
          </>
        }
      >
        <div className="stack">
          <div className="notice notice--info">
            <Icon name="info" size={15} />
            <span>按当前缺口筛选批量重新评估用例；只入队任务，不直接修改 spec。已在队列中的同用例任务会跳过。</span>
          </div>
          {(enrichPreview?.cases ?? []).slice(0, 20).map((row: any) => (
            <div className="gap-assertion" key={row.case_id}>
              <div className="gap-assertion__head">
                <span className="mono muted small">#{row.case_id}</span>
                <span>{row.title}</span>
                <Status value={row.status} map={CASE_STATUS} />
              </div>
              <div className="gap-assertion__meta">
                <span>{row.missing_input ? String(row.missing_input).slice(0, 100) : '缺少步骤/说明'}</span>
                {row.owner_kind && <span>归属：{meta(GAP_OWNER_KIND, row.owner_kind).label}</span>}
                {(row.execution_states ?? []).length > 0 && (
                  <span>
                    执行状态：
                    {(row.execution_states ?? []).map((state: string) => meta(EXECUTION_STATE, state).label).join('、')}
                  </span>
                )}
              </div>
            </div>
          ))}
          {enrichPreview && !(enrichPreview.cases ?? []).length && (
            <span className="muted small">当前筛选下没有需要重新评估的用例。</span>
          )}
        </div>
      </Modal>

      <Modal
        open={!!migrationPreview}
        onClose={() => setMigrationPreview(null)}
        title="清理无依据断言"
        subtitle={
          migrationPreview
            ? `影响 ${migrationPreview.affected_cases} 条用例 · ${migrationPreview.assertions} 条断言`
            : ''
        }
        footer={
          <>
            <div className="spacer" />
            <Button onClick={() => setMigrationPreview(null)}>取消</Button>
            <Button
              variant="primary"
              loading={reviewBusy}
              disabled={!migrationPreview?.assertions}
              onClick={applyMigration}
            >
              确认迁移（{migrationPreview?.assertions ?? 0} 条）
            </Button>
          </>
        }
      >
        <div className="stack">
          <div className="notice notice--warning">
            <Icon name="info" size={15} />
            <span>
              只处理没有可引用依据的断言：命中通用安全 / 可靠性策略的转成 <strong>policy 检查</strong>，
              其余转成 <strong>待确认问题</strong>（没有期望值，不进入断言统计，也不会判失败）。
              不会修改被测系统的 OpenAPI、文档或代码。
            </span>
          </div>
          {migrationPreview && (
            <div className="row-actions">
              <Pill tone="danger">{migrationPreview.to_policy} 条转策略检查</Pill>
              <Pill tone="violet">{migrationPreview.to_question} 条转待确认问题</Pill>
            </div>
          )}
          {(migrationPreview?.cases ?? []).map((row) => (
            <div className="gap-assertion" key={row.case_id}>
              <div className="gap-assertion__head">
                <span className="mono muted small">#{row.case_id}</span>
                <span>{row.title}</span>
                <span className="muted small">{row.assertions.length} 条</span>
              </div>
              {row.assertions.map((assertion, index) => (
                <div className="gap-assertion__meta" key={index}>
                  <code>{assertion.kind || 'assert'}</code>
                  <Pill tone={assertion.action === 'policy' ? 'danger' : 'violet'}>
                    {assertion.action === 'policy' ? '策略检查' : '待确认问题'}
                  </Pill>
                  <span>
                    {assertion.action === 'policy'
                      ? `${assertion.policy_id ?? ''} ${assertion.policy_title ?? ''}`
                      : assertion.question}
                  </span>
                </div>
              ))}
            </div>
          ))}
          {migrationPreview && !migrationPreview.cases.length && (
            <span className="muted small">当前没有无依据断言，所有断言都有可引用依据或明确人工理由。</span>
          )}
        </div>
      </Modal>

      <Modal
        open={!!probePreview}
        onClose={() => setProbePreview(null)}
        title="生成通用 probe"
        subtitle={
          probePreview
            ? `扫描 ${probePreview.endpoints} 个接口 · 计划新增 ${probePreview.planned} 条 · 已存在 ${probePreview.skipped} 条`
            : ''
        }
        footer={
          <>
            <div className="spacer" />
            <Button onClick={() => setProbePreview(null)}>取消</Button>
            <Button variant="primary" loading={reviewBusy} disabled={!probePreview?.planned} onClick={applyProbes}>
              确认生成（{probePreview?.planned ?? 0} 条）
            </Button>
          </>
        }
      >
        <div className="stack">
          <div className="notice notice--info">
            <Icon name="info" size={15} />
            <span>
              不依赖项目文档：对每个接口按「未带鉴权头 / 注入 payload / 路径穿越 / 负数分页」发起观测，
              只挂通用策略检查（不得 5xx、不得泄露堆栈与 SQL），不写业务期望值。
              生成的用例是<strong>待审核</strong>状态，只增加发现面，不会把猜测当断言。
            </span>
          </div>
          <div className="stack" style={{ gap: 6, maxHeight: 320, overflow: 'auto' }}>
            {(probePreview?.cases ?? []).map((item) => (
              <div className="gap-assertion" key={item.title}>
                <div className="gap-assertion__head">
                  <Pill tone={item.dimension === 'security' ? 'danger' : 'warning'}>
                    {item.tag}
                  </Pill>
                  <span>{item.title}</span>
                </div>
                <div className="gap-assertion__meta">{item.note}</div>
              </div>
            ))}
          </div>
          {probePreview && !probePreview.cases.length && (
            <span className="muted small">没有可生成的 probe：接口已全部覆盖，或项目还没有解析出接口。</span>
          )}
        </div>
      </Modal>
    </div>
  )
}
