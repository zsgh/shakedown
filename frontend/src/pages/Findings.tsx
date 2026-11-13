import { useEffect, useMemo, useState } from 'react'
import { Link, useParams, useSearchParams } from 'react-router-dom'
import { api, apiUrl, qs } from '../lib/api'
import { fromNow, shortPath } from '../lib/format'
import { useAsync } from '../lib/hooks'
import { FINDING_FIX_STATUS, FINDING_STATUS, RESULT_STATUS, SEVERITY, meta } from '../lib/labels'
import type { Finding, FindingRecheckResponse, LatestCaseResult, ResultDetail, Task } from '../lib/types'
import { Icon } from '../ui/icons'
import {
  Button,
  Card,
  CopyButton,
  EmptyState,
  ErrorBox,
  Field,
  PageHead,
  Pill,
  Skeleton,
  Status,
  cn,
} from '../ui/kit'
import { Drawer, Modal, useToast } from '../ui/overlays'

const STATUS_TABS = [
  { value: '', label: '全部' },
  { value: 'pending', label: '待确认' },
  { value: 'confirmed', label: '已确认' },
  { value: 'false_positive', label: '误报' },
  { value: 'accepted', label: '接受风险' },
  { value: 'wont_fix', label: '不修复' },
]

const VERDICT_META: Record<string, { label: string; tone: string; action: string }> = {
  real_bug: { label: '建议修复', tone: 'danger', action: '修复代码' },
  needs_verification: { label: '需运行时验证', tone: 'warning', action: '补验证用例' },
  false_positive: { label: '建议忽略', tone: 'muted', action: '忽略' },
  duplicate: { label: '重复', tone: 'info', action: '合并' },
  untriaged: { label: '未分诊', tone: 'muted', action: '人工复核' },
  accepted: { label: '已接受风险', tone: 'muted', action: '接受风险' },
  wont_fix: { label: '不修复', tone: 'muted', action: '不修复' },
}
const VERDICT_RANK: Record<string, number> = {
  real_bug: 0,
  needs_verification: 1,
  untriaged: 2,
  duplicate: 3,
  false_positive: 4,
  accepted: 5,
  wont_fix: 6,
}
const SEVERITY_RANK: Record<string, number> = { critical: 0, high: 1, medium: 2, low: 3 }
const ACTION_LABEL: Record<string, string> = {
  fix_code: '修复代码',
  add_test: '补验证用例',
  ignore: '忽略',
  merge: '合并重复',
  review: '人工复核',
}

type VerdictFilter = 'all' | 'real_bug' | 'needs_verification' | 'ignore' | 'untriaged'
type FindingView = 'groups' | 'list'
type GroupLevel = 'module' | 'theme'

interface FindingGroup {
  key: string
  title: string
  verdict: string
  severity: string
  confidence: number
  reason: string
  members: Finding[]
  files: string[]
  module?: string
  categories?: Array<{ name: string; count: number }>
}

interface IntegrationStatus {
  github: { configured: boolean; repo: string; api_url: string }
  jira: { configured: boolean; base_url: string; project_key: string; issue_type: string }
}

interface IssuePreview {
  status: string
  draft: { title: string; body: string; group_key: string; finding_ids: number[] }
}

export default function Findings() {
  const { projectId } = useParams()
  const [searchParams, setSearchParams] = useSearchParams()
  const toast = useToast()
  const [status, setStatus] = useState('')
  const [source, setSource] = useState('')
  const [severity, setSeverity] = useState('')
  const [fixStatus, setFixStatus] = useState('')
  const [query, setQuery] = useState('')
  const [selectedId, setSelectedId] = useState<number | null>(searchParams.get('finding') ? Number(searchParams.get('finding')) : null)
  const [busy, setBusy] = useState(false)
  const [view, setView] = useState<FindingView>('groups')
  const [groupLevel, setGroupLevel] = useState<GroupLevel>('module')
  const [verdictFilter, setVerdictFilter] = useState<VerdictFilter>('all')
  const [expandedGroups, setExpandedGroups] = useState<Set<string>>(new Set())
  const [triageTaskId, setTriageTaskId] = useState<number | null>(null)
  const [whiteboxTaskId, setWhiteboxTaskId] = useState<number | null>(null)
  const [integrationGroup, setIntegrationGroup] = useState<FindingGroup | null>(null)
  const [integrationProvider, setIntegrationProvider] = useState<'github' | 'jira'>('github')
  const [integrationPreview, setIntegrationPreview] = useState<IssuePreview | null>(null)
  const [integrationBusy, setIntegrationBusy] = useState(false)
  const [integrationSyncing, setIntegrationSyncing] = useState(false)
  const [disposition, setDisposition] = useState<{ finding: Finding; status: string } | null>(null)
  const [dispositionNote, setDispositionNote] = useState('')
  const [dispositionAssignee, setDispositionAssignee] = useState('')
  const [recheck, setRecheck] = useState<FindingRecheckResponse | null>(null)
  const [recheckBusy, setRecheckBusy] = useState(false)

  const findings = useAsync<Finding[]>(
    () =>
      api.get(
        `/projects/${projectId}/findings?limit=500${
          fixStatus ? `&fix_status=${encodeURIComponent(fixStatus)}` : ''
        }`,
      ),
    [projectId, fixStatus],
    { refreshMs: 20_000 },
  )
  const whiteboxTasks = useAsync<Task[]>(() => api.get(`/projects/${projectId}/tasks?limit=30`), [projectId], {
    refreshMs: 5_000,
  })
  const integrationStatus = useAsync<IntegrationStatus>(
    () => api.get(`/projects/${projectId}/findings/integrations/status`),
    [projectId],
  )
  const latestWhitebox = useMemo(
    () => (whiteboxTasks.data ?? []).find((task) => task.kind === 'whitebox_review') ?? null,
    [whiteboxTasks.data],
  )
  const activeWhitebox = latestWhitebox && ['queued', 'running'].includes(latestWhitebox.status) ? latestWhitebox : null
  const canResumeWhitebox =
    !!latestWhitebox &&
    ['cancelled', 'failed'].includes(latestWhitebox.status) &&
    Number(latestWhitebox.checkpoint?.whitebox?.done_modules?.length ?? 0) > 0
  const trackedWhiteboxTaskId = whiteboxTaskId ?? activeWhitebox?.id ?? null
  const whiteboxTask = useAsync<Task | null>(
    () =>
      trackedWhiteboxTaskId
        ? api.get<Task>(`/tasks/${trackedWhiteboxTaskId}`)
        : Promise.resolve(null),
    [trackedWhiteboxTaskId],
    { refreshMs: trackedWhiteboxTaskId ? 2_000 : undefined },
  )
  const whiteboxRunning =
    !!whiteboxTaskId ||
    !!activeWhitebox ||
    (!!whiteboxTask.data && ['queued', 'running'].includes(whiteboxTask.data.status))
  const all = findings.data ?? []

  const selected = all.find((item) => item.id === selectedId) ?? null
  const result = useAsync<ResultDetail | null>(
    () => (selected?.result_id ? api.get<ResultDetail>(`/results/${selected.result_id}`) : Promise.resolve(null)),
    [selected?.result_id],
  )
  const latest = useAsync<LatestCaseResult | null>(
    () =>
      selected?.source === 'whitebox' && selected.case_id
        ? api.get<LatestCaseResult>(`/cases/${selected.case_id}/latest-result`)
        : Promise.resolve(null),
    [selected?.source, selected?.case_id],
  )
  const triageTask = useAsync<Task | null>(
    () => (triageTaskId ? api.get<Task>(`/tasks/${triageTaskId}`) : Promise.resolve(null)),
    [triageTaskId],
    { refreshMs: triageTaskId ? 2_000 : undefined },
  )

  useEffect(() => {
    const task = triageTask.data
    if (!task) return
    if (task.status === 'succeeded') {
      toast.success('AI 分诊完成', '已按根因分组，原始条目全部保留')
      setTriageTaskId(null)
      findings.reload()
    } else if (task.status === 'failed' || task.status === 'cancelled') {
      toast.error('AI 分诊失败', task.error?.slice(0, 160) ?? '任务未完成')
      setTriageTaskId(null)
      findings.reload()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [triageTask.data?.id, triageTask.data?.status])

  useEffect(() => {
    const task = whiteboxTask.data
    if (!task || !['succeeded', 'failed', 'cancelled'].includes(task.status)) return
    setWhiteboxTaskId(null)
    whiteboxTasks.reload()
    findings.reload()
    if (task.status === 'succeeded') {
      toast.success('白盒走查完成', `已处理 ${task.checkpoint?.whitebox?.done ?? '—'} 个模块`)
    } else if (task.status === 'cancelled') {
      toast.info('白盒走查已停止', '已完成模块的发现已保留，可以继续走查')
    } else {
      toast.error('白盒走查失败', task.error?.slice(0, 160) ?? '任务未完成')
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [whiteboxTask.data?.id, whiteboxTask.data?.status])

  useEffect(() => {
    const fromUrl = searchParams.get('finding')
    const next = fromUrl ? Number(fromUrl) : null
    if (next !== selectedId) setSelectedId(next)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchParams])

  const filtered = useMemo(() => {
    return all.filter((item) => {
      if (status && item.status !== status) return false
      if (source && item.source !== source) return false
      if (severity && item.severity !== severity) return false
      if (fixStatus && (item.fix_status || 'open') !== fixStatus) return false
      if (
        query.trim() &&
        !`#${item.id} ${item.title} ${item.description} ${item.module}`
          .toLowerCase()
          .includes(query.trim().toLowerCase())
      )
        return false
      return true
    })
  }, [all, status, source, severity, query])

  const groups = useMemo<FindingGroup[]>(() => {
    const map = new Map<string, FindingGroup>()
    filtered.forEach((finding) => {
      const key = finding.cluster_key || `unclustered:${finding.id}`
      const triage = finding.triage || {}
      const verdict = finding.triage_status !== 'untriaged' && triage.verdict ? String(triage.verdict) : 'untriaged'
      const file = String(finding.code_location?.file_path || '')
      const existing = map.get(key)
      if (existing) {
        existing.members.push(finding)
        if (file && !existing.files.includes(file)) existing.files.push(file)
        if ((SEVERITY_RANK[finding.severity] ?? 9) < (SEVERITY_RANK[existing.severity] ?? 9)) {
          existing.severity = finding.severity
        }
        if (existing.verdict === 'untriaged' && verdict !== 'untriaged') existing.verdict = verdict
      } else {
        map.set(key, {
          key,
          title: String(triage.group_title || triage.title || finding.risk_type || finding.title),
          verdict,
          severity: finding.severity,
          confidence: Number(triage.confidence || 0),
          reason: String(triage.reason || ''),
          members: [finding],
          files: file ? [file] : [],
        })
      }
    })
    return [...map.values()]
      .map((group) => ({
        ...group,
        members: [...group.members].sort(
          (a, b) => (SEVERITY_RANK[a.severity] ?? 9) - (SEVERITY_RANK[b.severity] ?? 9) || a.id - b.id,
        ),
      }))
      .sort(
        (a, b) =>
          (VERDICT_RANK[a.verdict] ?? 9) - (VERDICT_RANK[b.verdict] ?? 9) ||
          (SEVERITY_RANK[a.severity] ?? 9) - (SEVERITY_RANK[b.severity] ?? 9) ||
          b.members.length - a.members.length,
      )
  }, [filtered])

  const moduleGroups = useMemo<FindingGroup[]>(() => {
    const map = new Map<string, { group: FindingGroup; categories: Map<string, number>; confidence: number[] }>()
    filtered.forEach((finding) => {
      const path = String(finding.code_location?.file_path || finding.module || '未分诊')
      const module = path.includes('/') ? path.split('/')[0] : path
      const triage = finding.triage || {}
      const verdict = finding.triage_status !== 'untriaged' && triage.verdict ? String(triage.verdict) : 'untriaged'
      const key = `${module}|${verdict}`
      let bucket = map.get(key)
      if (!bucket) {
        bucket = {
          group: {
            key,
            title: module,
            module,
            verdict,
            severity: finding.severity,
            confidence: 0,
            reason: '',
            members: [],
            files: [],
          },
          categories: new Map<string, number>(),
          confidence: [],
        }
        map.set(key, bucket)
      }
      bucket.group.members.push(finding)
      const file = String(finding.code_location?.file_path || '')
      if (file && !bucket.group.files.includes(file)) bucket.group.files.push(file)
      if ((SEVERITY_RANK[finding.severity] ?? 9) < (SEVERITY_RANK[bucket.group.severity] ?? 9)) {
        bucket.group.severity = finding.severity
      }
      const category = String(finding.risk_type || '其他')
      bucket.categories.set(category, (bucket.categories.get(category) ?? 0) + 1)
      if (triage.confidence) bucket.confidence.push(Number(triage.confidence))
    })
    return [...map.values()]
      .map((bucket) => {
        const categories = [...bucket.categories.entries()]
          .map(([name, count]) => ({ name, count }))
          .sort((a, b) => b.count - a.count)
        const confidence = bucket.confidence.length
          ? bucket.confidence.reduce((sum, value) => sum + value, 0) / bucket.confidence.length
          : 0
        return {
          ...bucket.group,
          categories,
          confidence,
          reason: categories.slice(0, 5).map((item) => `${item.name} ${item.count}`).join(' · '),
          members: [...bucket.group.members].sort(
            (a, b) => (SEVERITY_RANK[a.severity] ?? 9) - (SEVERITY_RANK[b.severity] ?? 9) || a.id - b.id,
          ),
        }
      })
      .sort(
        (a, b) =>
          (VERDICT_RANK[a.verdict] ?? 9) - (VERDICT_RANK[b.verdict] ?? 9) ||
          (SEVERITY_RANK[a.severity] ?? 9) - (SEVERITY_RANK[b.severity] ?? 9) ||
          b.members.length - a.members.length,
      )
  }, [filtered])

  const activeGroups = groupLevel === 'module' ? moduleGroups : groups

  const visibleGroups = useMemo(
    () =>
      activeGroups.filter((group) => {
        if (verdictFilter === 'all') return true
        if (verdictFilter === 'ignore') {
          return ['false_positive', 'duplicate', 'accepted', 'wont_fix'].includes(group.verdict)
        }
        return group.verdict === verdictFilter
      }),
    [activeGroups, verdictFilter],
  )

  const verdictCounts = useMemo(() => {
    return {
      all: activeGroups.length,
      real_bug: activeGroups.filter((group) => group.verdict === 'real_bug').length,
      needs_verification: activeGroups.filter((group) => group.verdict === 'needs_verification').length,
      ignore: activeGroups.filter((group) =>
        ['false_positive', 'duplicate', 'accepted', 'wont_fix'].includes(group.verdict),
      ).length,
      untriaged: activeGroups.filter((group) => group.verdict === 'untriaged').length,
    }
  }, [activeGroups])

  const counts = useMemo(() => {
    const map: Record<string, number> = {}
    all.forEach((item) => {
      map[item.status] = (map[item.status] ?? 0) + 1
    })
    return map
  }, [all])

  async function startTriage() {
    setBusy(true)
    try {
      const task = await api.post<{ task_id: number }>(
        `/projects/${projectId}/findings/triage?source=${source || 'all'}`,
        {},
      )
      setTriageTaskId(task.task_id)
      toast.info('AI 分诊已排队', '只写建议、不删除原始 finding；完成后自动刷新')
    } catch (error) {
      toast.error('AI 分诊没有启动', String(error))
    } finally {
      setBusy(false)
    }
  }

  async function batchStatus(ids: number[], nextStatus: string, label: string) {
    if (!ids.length) return
    setBusy(true)
    try {
      await api.post(`/projects/${projectId}/findings/batch-status`, {
        finding_ids: ids,
        status: nextStatus,
      })
      toast.success(label, `${ids.length} 条`)
      findings.reload()
    } catch (error) {
      toast.error('批量操作失败', String(error))
    } finally {
      setBusy(false)
    }
  }

  function toggleGroup(key: string) {
    setExpandedGroups((prev) => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }

  function renderFindingRow(finding: Finding) {
    const sev = meta(SEVERITY, finding.severity)
    return (
      <button key={finding.id} className="finding-row" onClick={() => openFinding(finding)}>
        <span className={`finding-row__severity tone-${sev.tone}`} />
        <span className="finding-row__main">
          <strong>{finding.title}</strong>
          <span className="finding-row__meta">
            <span className="mono muted small">#{finding.id}</span>
            <Pill tone={finding.source === 'whitebox' ? 'violet' : finding.source === 'external' ? 'warning' : 'blue'}>
              {finding.source === 'whitebox' ? '白盒' : finding.source === 'external' ? '外部验证' : '黑盒'}
            </Pill>
            <Pill tone={sev.tone}>{sev.label}</Pill>
            <Status value={finding.status} map={FINDING_STATUS} />
            {finding.fix_status && finding.fix_status !== 'open' && (
              <Pill tone={(FINDING_FIX_STATUS[finding.fix_status] ?? FINDING_FIX_STATUS.open).tone}>
                {(FINDING_FIX_STATUS[finding.fix_status] ?? FINDING_FIX_STATUS.open).label}
              </Pill>
            )}
            {finding.module && <span>{finding.module}</span>}
            {finding.risk_type && <span className="muted">{finding.risk_type}</span>}
            <span>{fromNow(finding.created_at)}</span>
          </span>
          {finding.description && <p>{finding.description}</p>}
        </span>
        {finding.code_location?.file_path && (
          <span className="finding-row__location mono">
            {shortPath(finding.code_location.file_path, 34)}:{finding.code_location.start_line}
          </span>
        )}
        <Icon name="chevronRight" size={14} />
      </button>
    )
  }

  function openFinding(finding: Finding) {
    setSelectedId(finding.id)
    const next = new URLSearchParams(searchParams)
    next.set('finding', String(finding.id))
    setSearchParams(next, { replace: true })
  }

  function closeFinding() {
    setSelectedId(null)
    const next = new URLSearchParams(searchParams)
    next.delete('finding')
    setSearchParams(next, { replace: true })
  }

  async function update(finding: Finding, nextStatus: string) {
    setBusy(true)
    try {
      await api.patch(`/findings/${finding.id}`, { status: nextStatus })
      const message =
        nextStatus === 'confirmed'
          ? '已确认为缺陷'
          : nextStatus === 'false_positive'
            ? '已标记误报'
            : nextStatus === 'accepted'
              ? '已接受风险'
              : nextStatus === 'wont_fix'
                ? '已标记不修复'
                : '已重置'
      toast.success(message)
      findings.reload()
    } catch (error) {
      toast.error('更新失败', String(error))
    } finally {
      setBusy(false)
    }
  }

  async function saveDisposition() {
    if (!disposition) return
    setBusy(true)
    try {
      await api.patch(`/findings/${disposition.finding.id}`, {
        status: disposition.status,
        note: dispositionNote,
        assignee: dispositionAssignee,
      })
      toast.success(
        disposition.status === 'accepted' ? '已接受风险' : '已标记不修复',
        dispositionAssignee ? `负责人：${dispositionAssignee}` : undefined,
      )
      setDisposition(null)
      setDispositionNote('')
      setDispositionAssignee('')
      findings.reload()
    } catch (error) {
      toast.error('处置失败', String(error))
    } finally {
      setBusy(false)
    }
  }

  function openDisposition(finding: Finding, status: 'accepted' | 'wont_fix') {
    setDisposition({ finding, status })
    setDispositionNote(String(finding.resolution?.note || ''))
    setDispositionAssignee(finding.assignee || '')
  }

  async function openRecheck() {
    setRecheckBusy(true)
    try {
      setRecheck(await api.get<FindingRecheckResponse>(`/projects/${projectId}/findings/recheck-candidates`))
    } catch (error) {
      toast.error('读取误报复核清单失败', String(error))
    } finally {
      setRecheckBusy(false)
    }
  }

  async function reopenFinding(findingId: number) {
    setRecheckBusy(true)
    try {
      await api.patch(`/findings/${findingId}`, { status: 'pending' })
      toast.success('已重新打开误报', `缺陷 #${findingId} 回到待确认，可重新复核`)
      setRecheck((prev) =>
        prev ? { ...prev, count: prev.count - 1, items: prev.items.filter((item) => item.finding_id !== findingId) } : prev,
      )
      findings.reload()
    } catch (error) {
      toast.error('重新打开失败', String(error))
    } finally {
      setRecheckBusy(false)
    }
  }

  async function autoVerifyFixes() {
    setBusy(true)
    try {
      await api.post(`/projects/${projectId}/findings/auto-verify`, {})
      toast.info('自动验证已排队', '可执行 API 用例会回归；构建/进程/权限类会转外部 Agent 验证')
      window.setTimeout(() => findings.reload(), 3000)
    } catch (error) {
      toast.error('自动验证没有启动', String(error))
    } finally {
      setBusy(false)
    }
  }

  async function verifyFix(finding: Finding, verified: boolean) {
    setBusy(true)
    try {
      await api.post(`/findings/${finding.id}/fix/verify`, {
        verified,
        note: verified ? '人工/回归验证通过' : '验证失败，重新打开',
      })
      toast.success(verified ? '修复已验证' : '已重新打开', `Finding #${finding.id}`)
      findings.reload()
    } catch (error) {
      toast.error('提交验证失败', String(error))
    } finally {
      setBusy(false)
    }
  }

  async function whitebox(resume = false) {
    setBusy(true)
    try {
      const result = await api.post<{ task_id: number; resumed?: boolean }>(
        `/projects/${projectId}/agent/whitebox${resume ? '?resume=true' : ''}`,
      )
      setWhiteboxTaskId(result.task_id)
      whiteboxTasks.reload()
      toast.success(
        result.resumed ? '继续白盒走查已排队' : '白盒走查已排队',
        '已完成模块会保留，可在本页停止或继续',
      )
    } catch (error) {
      toast.error('没有启动', String(error))
    } finally {
      setBusy(false)
    }
  }

  async function stopWhitebox() {
    const taskId = whiteboxTaskId ?? activeWhitebox?.id
    if (!taskId) return
    setBusy(true)
    try {
      await api.post(`/tasks/${taskId}/cancel`)
      toast.info('已请求停止白盒走查', '当前模块结束后停止，已完成模块的发现会保留')
      whiteboxTasks.reload()
    } catch (error) {
      toast.error('停止失败', String(error))
    } finally {
      setBusy(false)
    }
  }

  function openIntegration(group: FindingGroup) {
    const githubReady = integrationStatus.data?.github.configured
    const jiraReady = integrationStatus.data?.jira.configured
    setIntegrationProvider(githubReady ? 'github' : jiraReady ? 'jira' : 'github')
    setIntegrationGroup(group)
  }

  useEffect(() => {
    if (!integrationGroup) return
    let alive = true
    setIntegrationPreview(null)
    api
      .post<IssuePreview>(`/projects/${projectId}/findings/integrations/${integrationProvider}`, {
        group_key: integrationGroup.key,
        dry_run: true,
      })
      .then((result) => {
        if (alive) setIntegrationPreview(result)
      })
      .catch((error) => {
        if (alive) toast.error('工单预览失败', String(error))
      })
    return () => {
      alive = false
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [integrationGroup?.key, integrationProvider, projectId])

  async function syncIntegrations() {
    setIntegrationSyncing(true)
    try {
      const result = await api.post<{ synced: number; failed: number }>(
        `/projects/${projectId}/findings/integrations/sync`,
        {},
      )
      toast.success('工单状态已同步', `成功 ${result.synced} 个${result.failed ? ` · 失败 ${result.failed} 个` : ''}`)
      integrationStatus.reload()
    } catch (error) {
      toast.error('同步工单状态失败', String(error))
    } finally {
      setIntegrationSyncing(false)
    }
  }

  async function createIntegration() {
    if (!integrationGroup) return
    setIntegrationBusy(true)
    try {
      const result = await api.post<{
        status: string
        external_key?: string
        external_url?: string
      }>(`/projects/${projectId}/findings/integrations/${integrationProvider}`, {
        group_key: integrationGroup.key,
      })
      if (result.external_url) {
        window.open(result.external_url, '_blank', 'noopener,noreferrer')
      }
      toast.success(
        result.status === 'existing' ? '工单已存在' : '工单已创建',
        result.external_url ? `已打开 ${result.external_url}` : '外部工单已创建',
      )
      setIntegrationGroup(null)
      integrationStatus.reload()
    } catch (error) {
      toast.error('创建工单失败', String(error))
    } finally {
      setIntegrationBusy(false)
    }
  }

  const pending = counts.pending ?? 0
  const confirmed = counts.confirmed ?? 0
  const pendingFixes = all.filter((finding) =>
    ['claimed_fixed', 'reopened', 'needs_external_verification'].includes(finding.fix_status || ''),
  ).length
  const exportVerdict =
    verdictFilter === 'all' ? 'actionable' : verdictFilter === 'ignore' ? 'closed' : verdictFilter
  const exportHref = apiUrl(
    `/projects/${projectId}/findings/export.md${qs({
      status: status || undefined,
      source: source || undefined,
      severity: severity || undefined,
      q: query.trim() || undefined,
      verdict: exportVerdict,
    })}`,
  )

  return (
    <div className="page">
      <PageHead
        title="缺陷"
        subtitle="黑盒执行与白盒走查发现的线索。确认真假会回流给 Agent，用于后续归因优化。"
        actions={
          <>
            <a className="btn btn--secondary btn--md" href={exportHref} download title="按当前筛选导出 AI 可执行的 Markdown 修复清单">
              <Icon name="download" size={15} /> 导出修复清单
            </a>
            <Button icon="shield" loading={recheckBusy} onClick={openRecheck} title="按最新 spec 变化筛出可能需要重新打开的误报">
              误报复核
            </Button>
            <Button icon="refresh" onClick={() => findings.reload()}>刷新</Button>
            {whiteboxRunning ? (
              <Button variant="danger" icon="stop" loading={busy} onClick={stopWhitebox}>
                停止走查
              </Button>
            ) : (
              <Button icon="code" loading={busy} onClick={() => whitebox()}>
                白盒走查
              </Button>
            )}
            {canResumeWhitebox && !whiteboxRunning && (
              <Button icon="play" loading={busy} onClick={() => whitebox(true)}>
                继续走查
              </Button>
            )}
            {pendingFixes > 0 && (
              <Button icon="shield" loading={busy} onClick={autoVerifyFixes}>
                自动验证 {pendingFixes}
              </Button>
            )}
            {(integrationStatus.data?.github.configured || integrationStatus.data?.jira.configured) && (
              <Button icon="link" loading={integrationSyncing} onClick={syncIntegrations}>
                同步工单状态
              </Button>
            )}
            <Button variant="primary" icon="sparkles" loading={busy || !!triageTaskId} onClick={startTriage}>
              AI 分诊
            </Button>
          </>
        }
      />

      <div className="finding-summary">
        <div>
          <span>待确认</span>
          <strong className="text-warning">{pending}</strong>
        </div>
        <div>
          <span>已确认</span>
          <strong className="text-danger">{confirmed}</strong>
        </div>
        <div>
          <span>误报</span>
          <strong>{counts.false_positive ?? 0}</strong>
        </div>
        <div>
          <span>已关闭</span>
          <strong>{(counts.accepted ?? 0) + (counts.wont_fix ?? 0)}</strong>
        </div>
        <div>
          <span>确认率</span>
          <strong>{confirmed + (counts.false_positive ?? 0) ? Math.round((confirmed / (confirmed + (counts.false_positive ?? 0))) * 100) : 0}%</strong>
        </div>
      </div>

      {triageTaskId && triageTask.data && ['queued', 'running'].includes(triageTask.data.status) && (
        <div className="notice notice--info notice--wide">
          <span className="spinner" />
          <div>
            <strong>AI 正在分诊</strong>
            <span>
              {triageTask.data.checkpoint?.triage
                ? `模块 ${triageTask.data.checkpoint.triage.done}/${triageTask.data.checkpoint.triage.total} · ${triageTask.data.checkpoint.triage.module}`
                : '排队 / 读取代码中'}
              ，原始 finding 不会被删除。
            </span>
          </div>
        </div>
      )}

      <div className="finding-viewbar">
        <div className="segmented segmented--sm">
          <button className={cn('segmented__item', view === 'groups' && 'is-active')} onClick={() => setView('groups')}>
            <Icon name="layers" size={13} /> AI 分组
          </button>
          <button className={cn('segmented__item', view === 'list' && 'is-active')} onClick={() => setView('list')}>
            <Icon name="list" size={13} /> 原始列表
          </button>
        </div>
        {view === 'groups' && (
          <div className="segmented segmented--sm">
            <button className={cn('segmented__item', groupLevel === 'module' && 'is-active')} onClick={() => setGroupLevel('module')}>
              按模块
            </button>
            <button className={cn('segmented__item', groupLevel === 'theme' && 'is-active')} onClick={() => setGroupLevel('theme')}>
              按主题
            </button>
          </div>
        )}
        {view === 'groups' && (
          <div className="finding-verdict-tabs">
            {([
              ['all', '全部', verdictCounts.all],
              ['real_bug', '建议修复', verdictCounts.real_bug],
              ['needs_verification', '需验证', verdictCounts.needs_verification],
              ['ignore', '建议忽略', verdictCounts.ignore],
              ['untriaged', '未分诊', verdictCounts.untriaged],
            ] as Array<[VerdictFilter, string, number]>).map(([value, label, count]) => (
              <button
                key={value}
                className={cn('finding-verdict', verdictFilter === value && 'is-active')}
                onClick={() => setVerdictFilter(value)}
              >
                {label}
                <span>{count}</span>
              </button>
            ))}
          </div>
        )}
      </div>

      <Card flush>
        <div className="list-toolbar">
          <div className="segmented segmented--sm">
            {STATUS_TABS.map((tab) => (
              <button
                key={tab.value}
                className={cn('segmented__item', status === tab.value && 'is-active')}
                onClick={() => setStatus(tab.value)}
              >
                {tab.label}
                <span className="segmented__count">
                  {tab.value ? counts[tab.value] ?? 0 : all.length}
                </span>
              </button>
            ))}
          </div>
          <div className="list-search">
            <Icon name="search" size={14} />
            <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索标题、模块" />
          </div>
          <div className="list-filters">
            <select className="input input--sm" value={source} onChange={(event) => setSource(event.target.value)}>
              <option value="">全部来源</option>
              <option value="execution">黑盒执行</option>
              <option value="whitebox">白盒走查</option>
              <option value="external">外部验证</option>
            </select>
            <select className="input input--sm" value={severity} onChange={(event) => setSeverity(event.target.value)}>
              <option value="">全部级别</option>
              {Object.entries(SEVERITY).map(([key, item]) => (
                <option key={key} value={key}>{item.label}</option>
              ))}
            </select>
            <select className="input input--sm" value={fixStatus} onChange={(event) => setFixStatus(event.target.value)}>
              <option value="">全部修复状态</option>
              {Object.entries(FINDING_FIX_STATUS).map(([key, item]) => (
                <option key={key} value={key}>{item.label}</option>
              ))}
            </select>
          </div>
        </div>

        {findings.error && <ErrorBox>{findings.error}</ErrorBox>}
        {findings.loading && !findings.data && <div className="pad"><Skeleton lines={7} /></div>}
        {!findings.loading && filtered.length === 0 && (
          <EmptyState
            icon="bug"
            title={all.length ? '没有符合条件的缺陷' : '还没有缺陷线索'}
            description={all.length ? '换个筛选条件看看。' : '执行失败归因为「疑似缺陷」时会自动生成；也可以跑一次白盒走查。'}
            action={all.length ? undefined : <Button onClick={() => whitebox()}>触发白盒走查</Button>}
          />
        )}

        {view === 'list' && filtered.length > 0 && (
          <div className="finding-list">
            {filtered.map(renderFindingRow)}
          </div>
        )}

        {view === 'groups' && filtered.length > 0 && visibleGroups.length === 0 && (
          <EmptyState
            icon="layers"
            title="这个筛选下没有分组"
            description="AI 还没分诊，或当前筛选把这一组过滤掉了。可以点右上「AI 分诊」。"
            action={<Button onClick={startTriage} icon="sparkles">开始 AI 分诊</Button>}
          />
        )}

        {view === 'groups' && visibleGroups.length > 0 && (
          <div className="finding-clusters">
            {visibleGroups.map((group) => {
              const verdict = VERDICT_META[group.verdict] ?? VERDICT_META.untriaged
              const severity = meta(SEVERITY, group.severity)
              const expanded = expandedGroups.has(group.key)
              const ids = group.members.map((member) => member.id)
              return (
                <article key={group.key} className={cn('finding-cluster', `is-${verdict.tone}`)}>
                  <div
                    className="finding-cluster__head"
                    onClick={() => toggleGroup(group.key)}
                    role="button"
                    tabIndex={0}
                    onKeyDown={(event) => {
                      if (event.key === 'Enter' || event.key === ' ') {
                        event.preventDefault()
                        toggleGroup(group.key)
                      }
                    }}
                  >
                    <Pill tone={verdict.tone}>{verdict.label}</Pill>
                    <strong className="finding-cluster__title">{group.title}</strong>
                    <Pill tone={severity.tone}>{severity.label}</Pill>
                    <span className="finding-cluster__count">
                      {group.members.length} 条 ·{' '}
                      {group.members
                        .slice(0, 4)
                        .map((member) => `#${member.id}`)
                        .join('、')}
                      {group.members.length > 4 ? '…' : ''}
                    </span>
                    {group.confidence > 0 && (
                      <span className="muted small">置信度 {Math.round(group.confidence * 100)}%</span>
                    )}
                    <span className="finding-cluster__files">
                      {group.files.length ? `${group.files.length} 个文件` : '未定位文件'}
                    </span>
                    <div className="spacer" />
                    <div className="finding-cluster__actions" onClick={(event) => event.stopPropagation()}>
                      <Button size="sm" variant="primary" disabled={busy} onClick={() => batchStatus(ids, 'confirmed', '已确认整组')}>
                        确认整组
                      </Button>
                      <Button size="sm" disabled={busy} onClick={() => batchStatus(ids, 'false_positive', '已标记整组误报')}>
                        误报整组
                      </Button>
                      <Button size="sm" variant="ghost" disabled={busy} onClick={() => batchStatus(ids, 'accepted', '整组已接受风险')}>
                        接受风险
                      </Button>
                      <Button size="sm" variant="ghost" disabled={busy} onClick={() => batchStatus(ids, 'wont_fix', '整组已标记不修复')}>
                        不修复
                      </Button>
                      <Button size="sm" variant="ghost" icon="send" disabled={busy} onClick={() => openIntegration(group)}>
                        建工单
                      </Button>
                      {group.members.some((member) => member.status !== 'pending') && (
                        <Button size="sm" variant="ghost" disabled={busy} onClick={() => batchStatus(ids, 'pending', '已恢复待处理')}>
                          恢复
                        </Button>
                      )}
                    </div>
                    <Icon name={expanded ? 'chevronDown' : 'chevronRight'} size={14} />
                  </div>
                  {group.reason && <p className="finding-cluster__reason">{group.reason}</p>}
                  {!!group.files.length && (
                    <div className="finding-cluster__path mono">{group.files.slice(0, 3).join(' · ')}</div>
                  )}
                  {expanded && (
                    <div className="finding-cluster__members">
                      {group.members.map(renderFindingRow)}
                    </div>
                  )}
                </article>
              )
            })}
          </div>
        )}
      </Card>

      <Drawer
        open={!!selected}
        onClose={closeFinding}
        title={selected?.title ?? ''}
        subtitle={
          selected ? (
            <span className="crumb-inline">
              <Status value={selected.status} map={FINDING_STATUS} />
              <Pill tone={meta(SEVERITY, selected.severity).tone}>{meta(SEVERITY, selected.severity).label}</Pill>
              <span className="mono">#{selected.id}</span>
              <span>{selected.source === 'whitebox' ? '白盒走查' : selected.source === 'external' ? '外部验证' : '黑盒执行'} · {fromNow(selected.created_at)}</span>
            </span>
          ) : ''
        }
        width={720}
        actions={
          selected ? (
            <>
              {selected.status !== 'confirmed' && (
                <Button size="sm" variant="primary" loading={busy} onClick={() => update(selected, 'confirmed')}>确认</Button>
              )}
              {selected.status !== 'false_positive' && (
                <Button size="sm" loading={busy} onClick={() => update(selected, 'false_positive')}>误报</Button>
              )}
              {selected.status !== 'accepted' && (
                <Button size="sm" loading={busy} onClick={() => openDisposition(selected, 'accepted')}>接受风险</Button>
              )}
              {selected.status !== 'wont_fix' && (
                <Button size="sm" loading={busy} onClick={() => openDisposition(selected, 'wont_fix')}>不修复</Button>
              )}
              {selected.status !== 'pending' && (
                <Button size="sm" variant="ghost" loading={busy} onClick={() => update(selected, 'pending')}>重新打开</Button>
              )}
            </>
          ) : undefined
        }
      >
        {selected && (
          <div className="stack">
            <div className="finding-detail-meta">
              <div><span>来源</span><strong>{selected.source === 'whitebox' ? '白盒走查' : '黑盒执行'}</strong></div>
              <div><span>模块</span><strong>{selected.module || '—'}</strong></div>
              <div><span>风险类型</span><strong>{selected.risk_type || '—'}</strong></div>
              <div><span>确认人</span><strong>{selected.confirmed_by || '—'}</strong></div>
            </div>

            {selected.triage?.verdict && (
              <section className="result-block">
                <h3>AI 分诊</h3>
                <div className="triage-box">
                  <div className="triage-box__head">
                    <Pill tone={(VERDICT_META[String(selected.triage.verdict)] ?? VERDICT_META.untriaged).tone}>
                      {(VERDICT_META[String(selected.triage.verdict)] ?? VERDICT_META.untriaged).label}
                    </Pill>
                    {selected.triage.confidence !== undefined && (
                      <span className="muted small">置信度 {Math.round(Number(selected.triage.confidence) * 100)}%</span>
                    )}
                    {selected.triage.recommended_action && (
                      <Pill tone="muted">
                        建议：{ACTION_LABEL[String(selected.triage.recommended_action)] ?? selected.triage.recommended_action}
                      </Pill>
                    )}
                  </div>
                  {selected.triage.reason && <p>{selected.triage.reason}</p>}
                  <span className="muted small">AI 仅提供建议，原始 finding 与证据不会被覆盖；最终确认/误报仍由你决定。</span>
                </div>
              </section>
            )}

            {selected.fix_status && selected.fix_status !== 'open' && (
              <section className="result-block">
                <h3>修复状态</h3>
                <div className="triage-box">
                  <div className="triage-box__head">
                    <Pill tone={(FINDING_FIX_STATUS[selected.fix_status] ?? FINDING_FIX_STATUS.open).tone}>
                      {(FINDING_FIX_STATUS[selected.fix_status] ?? FINDING_FIX_STATUS.open).label}
                    </Pill>
                    {selected.fix_verified_at && (
                      <span className="muted small">验证于 {fromNow(selected.fix_verified_at)}</span>
                    )}
                  </div>
                  {selected.fix_claim?.actor && (
                    <span className="muted small">提交者：{String(selected.fix_claim.actor)}</span>
                  )}
                  {selected.fix_claim?.commit_sha && (
                    <div className="link-row">
                      <span className="muted small">Commit</span>
                      <code>{String(selected.fix_claim.commit_sha)}</code>
                    </div>
                  )}
                  {selected.fix_claim?.pr_url && (
                    <a className="link-btn" href={String(selected.fix_claim.pr_url)} target="_blank" rel="noreferrer">
                      打开 PR / Issue <Icon name="external" size={12} />
                    </a>
                  )}
                  {selected.fix_claim?.files?.length > 0 && (
                    <p className="muted small">修改文件：{selected.fix_claim?.files?.join(' · ')}</p>
                  )}
                  {selected.fix_claim?.note && <p>{String(selected.fix_claim.note)}</p>}
                  {selected.fix_claim?.test_command && (
                    <pre className="code code--compact">{String(selected.fix_claim.test_command)}</pre>
                  )}
                  {selected.fix_claim?.test_result && (
                    <pre className="code code--compact">{String(selected.fix_claim.test_result)}</pre>
                  )}
                  {['claimed_fixed', 'reopened', 'needs_human_verification', 'needs_external_verification'].includes(selected.fix_status) && (
                    <div className="result-block__actions">
                      <Button size="sm" variant="primary" loading={busy} onClick={() => verifyFix(selected, true)}>
                        验证通过
                      </Button>
                      <Button size="sm" variant="danger" loading={busy} onClick={() => verifyFix(selected, false)}>
                        验证失败
                      </Button>
                    </div>
                  )}
                </div>
              </section>
            )}

            {selected && (selected.assignee || selected.resolution?.note || selected.status === 'false_positive') && (
              <section className="result-block">
                <h3>处置信息</h3>
                {selected.assignee && (
                  <p className="muted small">负责人：<strong>{selected.assignee}</strong></p>
                )}
                {selected.status === 'false_positive' && (
                  <p className="muted small">
                    误报原因：{selected.resolution?.note ? String(selected.resolution.note) : '未填写原因'}
                  </p>
                )}
                {selected.resolution?.note && selected.status !== 'false_positive' && (
                  <p>{String(selected.resolution.note)}</p>
                )}
                {selected.resolution?.at && (
                  <p className="muted small">记录于 {fromNow(String(selected.resolution.at))}</p>
                )}
              </section>
            )}

            <section className="result-block">
              <h3>描述</h3>
              <p className="finding-description">{selected.description || '没有描述。'}</p>
            </section>

            {selected.code_location?.file_path && (
              <section className="result-block">
                <h3>代码位置</h3>
                <div className="code-location">
                  <Icon name="code" size={14} />
                  <code>
                    {selected.code_location.file_path}
                    {selected.code_location.start_line ? `:${selected.code_location.start_line}` : ''}
                  </code>
                  <CopyButton
                    text={`${selected.code_location.file_path}:${selected.code_location.start_line ?? ''}`}
                    label="复制位置"
                  />
                </div>
              </section>
            )}

            {selected.evidence?.length > 0 && (
              <section className="result-block">
                <h3>依据</h3>
                <div className="evidence-list">
                  {selected.evidence.map((item, index) => (
                    <div key={index} className="evidence">
                      <Pill tone="blue">{String(item.type ?? item.source_type ?? '证据')}</Pill>
                      {item.detail !== undefined && (
                        <pre className="code code--compact">{typeof item.detail === 'string' ? item.detail : JSON.stringify(item.detail, null, 2)}</pre>
                      )}
                      {item.ref && <code>{String(item.ref)}</code>}
                      {item.quote && <p>{String(item.quote)}</p>}
                      {item.case_id && (
                        <Link className="link-btn" to={`/projects/${projectId}/cases/${item.case_id}`}>
                          关联用例 #{String(item.case_id)} <Icon name="arrowRight" size={12} />
                        </Link>
                      )}
                    </div>
                  ))}
                </div>
              </section>
            )}

            {selected.repro && (
              <section className="result-block">
                <h3>复现命令</h3>
                <pre className="code code--log">{selected.repro}</pre>
                <div className="result-block__actions">
                  <CopyButton text={selected.repro} label="复制命令" />
                </div>
              </section>
            )}

            {(selected.case_id || selected.source === 'whitebox') && (
              <section className="result-block">
                <h3>关联</h3>
                <div className="link-row">
                  {selected.case_id ? (
                    <Link className="btn btn--secondary btn--sm" to={`/projects/${projectId}/cases/${selected.case_id}`}>
                      <Icon name="beaker" size={14} />
                      {selected.source === 'whitebox' ? '查看建议用例' : '查看用例'} #{selected.case_id}
                    </Link>
                  ) : (
                    <span className="muted small">旧发现尚未关联建议用例，重新跑一次白盒走查会自动补齐。</span>
                  )}
                  {selected.source === 'whitebox' ? (
                    latest.data?.result && latest.data.run ? (
                      <>
                        <Link
                          className="btn btn--secondary btn--sm"
                          to={`/projects/${projectId}/runs/${latest.data.run.id}`}
                        >
                          <Icon name="play" size={14} /> 最近执行 #{latest.data.run.id}
                        </Link>
                        <Status value={latest.data.result.status} map={RESULT_STATUS} />
                      </>
                    ) : latest.error ? (
                      <span className="muted small">建议用例已被重新生成或删除，关联暂时失效。</span>
                    ) : latest.loading ? (
                      <span className="muted small">查询最近执行…</span>
                    ) : selected.case_id ? (
                      <span className="muted small">建议用例尚未执行（补全步骤并启用后才会运行）。</span>
                    ) : null
                  ) : result.data?.run ? (
                    <Link className="btn btn--secondary btn--sm" to={`/projects/${projectId}/runs/${result.data.run.id}`}>
                      <Icon name="play" size={14} /> 执行 #{result.data.run.id}
                    </Link>
                  ) : null}
                </div>
                {selected.source === 'whitebox' && (
                  <p className="muted small" style={{ marginTop: 8 }}>
                    白盒走查是静态代码审查，本身不产生执行记录。建议用例补全并启用后，最近一次执行会回链到这里。
                  </p>
                )}
              </section>
            )}

            {result.data && (
              <>
                <section className="result-block">
                  <h3>关联执行结果</h3>
                  <div className="request-line">
                    <Pill tone={result.data.status === 'fail' ? 'danger' : 'success'}>{result.data.case_title ?? '执行结果'}</Pill>
                    <span className="muted small">{result.data.status} · {fromNow(result.data.created_at)}</span>
                  </div>
                  <div className="request-line">
                    <Pill tone="blue">{result.data.request?.method ?? 'GET'}</Pill>
                    <code>{result.data.request?.url ?? '—'}</code>
                  </div>
                  <pre className="code">{`HTTP ${result.data.response?.status_code ?? '—'}\n${JSON.stringify(result.data.response?.body ?? null, null, 2)}`}</pre>
                </section>
                {result.data.attribution?.reason && (
                  <section className="result-block">
                    <h3>模型归因</h3>
                    <p>{result.data.attribution.reason}</p>
                  </section>
                )}
              </>
            )}
          </div>
        )}
      </Drawer>

      <Modal
        open={!!disposition}
        onClose={() => setDisposition(null)}
        title={disposition?.status === 'accepted' ? '接受风险' : '标记不修复'}
        subtitle={disposition?.finding.title}
        footer={
          <>
            <div className="spacer" />
            <Button onClick={() => setDisposition(null)}>取消</Button>
            <Button variant="primary" loading={busy} onClick={saveDisposition}>保存</Button>
          </>
        }
      >
        <div className="stack">
          <Field label="负责人" hint="可选；用于交接和筛选">
            <input
              className="input"
              value={dispositionAssignee}
              onChange={(event) => setDispositionAssignee(event.target.value)}
            />
          </Field>
          <Field label="处置备注" hint="建议写清楚接受原因、工单号或后续计划">
            <textarea
              className="input"
              rows={4}
              value={dispositionNote}
              onChange={(event) => setDispositionNote(event.target.value)}
            />
          </Field>
        </div>
      </Modal>

      <Modal
        open={!!recheck}
        onClose={() => setRecheck(null)}
        title="误报复核"
        subtitle={
          recheck
            ? `基于最新 spec 快照 ${recheck.snapshot_version || '—'}，可能有 ${recheck.count} 条误报需要重新打开`
            : ''
        }
        size="lg"
        footer={
          <>
            <div className="spacer" />
            <Button onClick={() => setRecheck(null)}>关闭</Button>
          </>
        }
      >
        <div className="stack">
          <div className="notice notice--info">
            <Icon name="info" size={16} />
            <span>
              只筛选「误报」且引用接口在最新 spec 中发生变更或删除的缺陷，用于复核当时的误报结论是否仍然成立。
              重新打开不会修改被测系统，只会把缺陷放回待确认。
            </span>
          </div>
          {recheck && recheck.items.length === 0 && (
            <EmptyState icon="check" title="没有需要复核的误报" description="当前 spec 快照没有影响已标记误报的接口。" />
          )}
          {(recheck?.items ?? []).map((candidate) => (
            <div className="result-block" key={candidate.finding_id}>
              <div className="row-actions">
                <Status value={candidate.severity} map={SEVERITY} />
                <strong>#{candidate.finding_id} {candidate.title}</strong>
                <div className="spacer" />
                <Button size="sm" loading={recheckBusy} onClick={() => reopenFinding(candidate.finding_id)}>
                  重新打开
                </Button>
              </div>
              <p className="muted small">
                受影响接口：{candidate.endpoints.join('、') || '—'}
                {candidate.removed_endpoints.length > 0 && `（已移除：${candidate.removed_endpoints.join('、')}）`}
              </p>
              <p className="muted small">
                误报原因：{candidate.resolution_missing ? '未填写原因' : candidate.resolution_note}
                {candidate.assignee ? ` · 负责人：${candidate.assignee}` : ''}
              </p>
            </div>
          ))}
        </div>
      </Modal>

      <Modal
        open={!!integrationGroup}
        onClose={() => setIntegrationGroup(null)}
        title="创建修复工单"
        subtitle={integrationGroup?.title}
        size="lg"
        footer={
          <>
            <Button onClick={() => setIntegrationGroup(null)}>取消</Button>
            <Button
              variant="primary"
              icon="send"
              loading={integrationBusy}
              disabled={!integrationStatus.data?.[integrationProvider]?.configured || !integrationPreview}
              onClick={createIntegration}
            >
              创建 {integrationProvider === 'github' ? 'GitHub Issue' : 'Jira 工单'}
            </Button>
          </>
        }
      >
        <div className="stack">
          <div className="segmented segmented--sm">
            <button
              className={cn('segmented__item', integrationProvider === 'github' && 'is-active')}
              onClick={() => setIntegrationProvider('github')}
            >
              GitHub Issue
            </button>
            <button
              className={cn('segmented__item', integrationProvider === 'jira' && 'is-active')}
              onClick={() => setIntegrationProvider('jira')}
            >
              Jira 工单
            </button>
          </div>

          {!integrationStatus.loading && !integrationStatus.data?.[integrationProvider]?.configured && (
            <div className="notice notice--warning">
              <Icon name="alert" size={16} />
              <span>
                {integrationProvider === 'github'
                  ? 'GitHub 还没有配置完整。请到「项目设置 → 工单集成」填写仓库；Token 在「系统设置 → 缺陷工单凭据」。'
                  : 'Jira 还没有配置完整。请到「项目设置 → 工单集成」填写项目 Key；地址、邮箱和 Token 在系统设置里配置。'}
              </span>
            </div>
          )}

          {integrationPreview ? (
            <>
              <div className="notice notice--info">
                <Icon name="send" size={16} />
                <span>
                  将创建工单：<strong>{integrationPreview.draft.title}</strong>
                  <br />
                  正文包含该修复组的证据、代码位置、复现命令和验收标准。
                </span>
              </div>
              <details>
                <summary className="muted small">预览工单正文</summary>
                <pre className="code code--compact" style={{ maxHeight: 320, overflow: 'auto' }}>
                  {integrationPreview.draft.body}
                </pre>
              </details>
            </>
          ) : (
            <Skeleton lines={5} />
          )}
        </div>
      </Modal>
    </div>
  )
}
