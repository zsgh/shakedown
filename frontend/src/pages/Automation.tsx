import { useEffect, useMemo, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { useProject } from '../app/project'
import { api } from '../lib/api'
import { formatFullTime, percent } from '../lib/format'
import { ATTACK_CATEGORY, EXCEPTION_KIND, EXCEPTION_STATUS, EXCEPTION_TARGET, GATE_BLOCKER, SEVERITY, meta } from '../lib/labels'
import type {
  AssertionQuality,
  AutomationException,
  AutomationHealth,
  GateResult,
  MutationAudit,
  OracleTrendPoint,
  PerformanceHistory,
  ProjectMember,
  ProjectSchedule,
  SlaBreaches,
  SlaEscalationResult,
  VisualBaseline,
} from '../lib/types'
import { Icon } from '../ui/icons'
import { Button, Card, Field, PageHead, Pill, Segmented, Skeleton } from '../ui/kit'
import { useToast } from '../ui/overlays'

type Tab = 'overview' | 'schedules' | 'access' | 'exceptions' | 'quality' | 'attack' | 'performance' | 'visual' | 'settings'

const TABS: Array<{ value: Tab; label: string }> = [
  { value: 'overview', label: '运行健康' },
  { value: 'schedules', label: '调度' },
  { value: 'access', label: '成员 / 角色' },
  { value: 'exceptions', label: '例外 / SLA' },
  { value: 'quality', label: '断言质量' },
  { value: 'attack', label: '攻击用例包' },
  { value: 'performance', label: '性能' },
  { value: 'visual', label: '视觉 / a11y' },
  { value: 'settings', label: '自动化设置' },
]

function settled<T>(result: PromiseSettledResult<T>, fallback: T): T {
  return result.status === 'fulfilled' ? result.value : fallback
}

function summaryValue(value: any): string {
  if (value === null || value === undefined) return '—'
  if (typeof value === 'object') return JSON.stringify(value)
  return String(value)
}

const VALIDATION_REASON: Record<string, string> = {
  no_steps: '无步骤',
  missing_input: '缺输入',
  no_confirmed_assertion: '无有依据断言',
  high_risk: '高风险',
  already_validated: '已验证',
  not_pending: '非待确认',
}

export default function Automation() {
  const { projectId } = useParams()
  const { project, reload } = useProject()
  const toast = useToast()
  const [tab, setTab] = useState<Tab>('overview')
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState('')

  const [health, setHealth] = useState<AutomationHealth | null>(null)
  const [readiness, setReadiness] = useState<Record<string, any> | null>(null)
  const [gate, setGate] = useState<GateResult | null>(null)
  const [gateRunId, setGateRunId] = useState('')
  const [schedules, setSchedules] = useState<ProjectSchedule[]>([])
  const [members, setMembers] = useState<ProjectMember[]>([])
  const [exceptions, setExceptions] = useState<AutomationException[]>([])
  const [breaches, setBreaches] = useState<SlaBreaches | null>(null)
  const [escalation, setEscalation] = useState<SlaEscalationResult | null>(null)
  const [quality, setQuality] = useState<AssertionQuality | null>(null)
  const [mutation, setMutation] = useState<MutationAudit | null>(null)
  const [trend, setTrend] = useState<OracleTrendPoint[]>([])
  const [performance, setPerformance] = useState<PerformanceHistory | null>(null)
  const [baselines, setBaselines] = useState<VisualBaseline[]>([])
  const [retirement, setRetirement] = useState<Record<string, any> | null>(null)
  const [validation, setValidation] = useState<Record<string, any> | null>(null)
  const [attackPack, setAttackPack] = useState<Record<string, any> | null>(null)
  const [attackScanned, setAttackScanned] = useState(false)
  const [a11y, setA11y] = useState<Record<string, any> | null>(null)

  const [scheduleName, setScheduleName] = useState('每日回归')
  const [scheduleInterval, setScheduleInterval] = useState('86400')
  const [scheduleMode, setScheduleMode] = useState('smoke')
  const [scheduleMax, setScheduleMax] = useState('30')
  const [memberActor, setMemberActor] = useState('')
  const [memberRole, setMemberRole] = useState('viewer')
  const [exceptionNote, setExceptionNote] = useState('')
  const [exceptionOwner, setExceptionOwner] = useState('')
  const [perfMode, setPerfMode] = useState('baseline')
  const [perfPath, setPerfPath] = useState('/api/health')
  const [perfConcurrency, setPerfConcurrency] = useState('5')
  const [perfIterations, setPerfIterations] = useState('20')
  const [perfDuration, setPerfDuration] = useState('')
  const [baselineCase, setBaselineCase] = useState('')
  const [baselineName, setBaselineName] = useState('default')
  const [a11yCase, setA11yCase] = useState('')
  const [settings, setSettings] = useState<Record<string, any>>({})
  const [webhookUrl, setWebhookUrl] = useState('')
  const [notificationEmail, setNotificationEmail] = useState('')

  const openExceptions = useMemo(
    () => exceptions.filter((item) => item.status === 'open' || item.status === 'acked'),
    [exceptions],
  )

  useEffect(() => {
    if (!projectId) return
    if (project?.settings) {
      setSettings({ ...(project.settings.automation ?? {}) })
      setWebhookUrl(String(project.settings.webhook_url ?? ''))
      setNotificationEmail(String(project.settings.notification_email ?? ''))
    }
  }, [project?.id, project?.settings])

  useEffect(() => {
    if (projectId) void loadAll()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId])

  useEffect(() => {
    if (tab === 'attack' && !attackScanned) {
      setAttackScanned(true)
      void scanAttackPack(true)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tab, attackScanned])

  async function loadAll() {
    if (!projectId) return
    setLoading(true)
    const base = `/projects/${projectId}`
    const results = await Promise.allSettled([
      api.get<AutomationHealth>(`${base}/automation/health`),
      api.get<Record<string, any>>(`${base}/readiness`),
      api.get<GateResult>(`${base}/automation/gate`),
      api.get<ProjectSchedule[]>(`${base}/schedules`),
      api.get<ProjectMember[]>(`${base}/members`),
      api.get<AutomationException[]>(`${base}/exceptions?status=open`),
      api.get<SlaBreaches>(`${base}/sla/breaches`),
      api.get<AssertionQuality>(`${base}/assertion-quality`),
      api.get<MutationAudit>(`${base}/assertion-quality/mutation`),
      api.get<{ points: OracleTrendPoint[] }>(`${base}/oracle-trend`),
      api.get<PerformanceHistory>(`${base}/performance/history`),
      api.get<VisualBaseline[]>(`${base}/visual/baselines`),
      api.get<Record<string, any>>(`${base}/cases/validation/preview`),
      api.get<Record<string, any>>(`${base}/attack-pack`),
    ])
    setHealth(settled(results[0] as PromiseSettledResult<AutomationHealth>, null as any))
    setReadiness(settled(results[1] as PromiseSettledResult<Record<string, any>>, {}))
    setGate(settled(results[2] as PromiseSettledResult<GateResult>, null as any))
    setSchedules(settled(results[3] as PromiseSettledResult<ProjectSchedule[]>, []))
    setMembers(settled(results[4] as PromiseSettledResult<ProjectMember[]>, []))
    setExceptions(settled(results[5] as PromiseSettledResult<AutomationException[]>, []))
    setBreaches(settled(results[6] as PromiseSettledResult<SlaBreaches>, null as any))
    setQuality(settled(results[7] as PromiseSettledResult<AssertionQuality>, null as any))
    setMutation(settled(results[8] as PromiseSettledResult<MutationAudit>, null as any))
    setTrend(settled(results[9] as PromiseSettledResult<{ points: OracleTrendPoint[] }>, { points: [] }).points)
    setPerformance(settled(results[10] as PromiseSettledResult<PerformanceHistory>, null as any))
    setBaselines(settled(results[11] as PromiseSettledResult<VisualBaseline[]>, []))
    setValidation(settled(results[12] as PromiseSettledResult<Record<string, any>>, null as any))
    setAttackPack(settled(results[13] as PromiseSettledResult<Record<string, any>>, null as any))
    setLoading(false)
  }

  async function refreshGate(runId?: string) {
    if (!projectId) return
    setBusy('gate')
    try {
      setGate(await api.get<GateResult>(`/projects/${projectId}/automation/gate${runId ? `?run_id=${runId}` : ''}`))
    } catch (error) {
      toast.error('读取 gate 失败', String(error))
    } finally {
      setBusy('')
    }
  }

  async function runValidation() {
    if (!projectId) return
    setBusy('validation')
    try {
      const result = await api.post<Record<string, any>>(`/projects/${projectId}/cases/validation/run`, { limit: 50 })
      if (!result.run_id) toast.info('没有可试运行的待确认用例')
      else if (result.created) toast.success(`已排队试运行 ${result.cases} 条用例`)
      else toast.info(`已有试运行在排队（run #${result.run_id}）`)
      await loadAll()
    } catch (error) {
      toast.error('创建试运行失败', String(error))
    } finally {
      setBusy('')
    }
  }

  async function scanAttackPack(silent = false) {
    if (!projectId) return
    setBusy('attack')
    try {
      const result = await api.post<Record<string, any>>(`/projects/${projectId}/attack-pack/scan`, {
        include_findings: true,
      })
      setAttackPack(result)
      if (!silent) {
        toast.success(`扫描完成：${result.summary?.applicable ?? 0} 个模式适用，${result.summary?.findings ?? 0} 条供应链发现`)
      }
    } catch (error) {
      if (!silent) toast.error('扫描攻击包失败', String(error))
    } finally {
      setBusy('')
    }
  }

  async function runSecurityLab() {
    if (!projectId) return
    setBusy('attack-lab')
    try {
      const result = await api.post<Record<string, any>>(`/projects/${projectId}/security-lab/run`)
      const found = result.findings ?? []
      if (found.length) toast.success(`隔离实验室发现：${found.join('、')}`)
      else toast.info('隔离实验室未发现新问题')
      await loadAll()
    } catch (error) {
      toast.error('隔离实验室执行失败', String(error))
    } finally {
      setBusy('')
    }
  }

  async function generateAttackPack(withValidation: boolean) {
    if (!projectId) return
    setBusy(withValidation ? 'attack-validate' : 'attack-generate')
    try {
      const result = await api.post<Record<string, any>>(`/projects/${projectId}/attack-pack/generate`, {
        include_findings: true,
        validate: withValidation,
      })
      setAttackPack(result)
      const created = result.created_case_ids?.length ?? 0
      const findings = result.created_finding_ids?.length ?? 0
      toast.success(
        `已生成 ${created} 条攻击用例、${findings} 条供应链缺陷${result.validation_run_id ? `，验证 run #${result.validation_run_id}` : ''}`,
      )
      await loadAll()
    } catch (error) {
      toast.error('生成攻击用例失败', String(error))
    } finally {
      setBusy('')
    }
  }

  async function createSchedule() {
    if (!projectId) return
    setBusy('schedule')
    try {
      await api.post(`/projects/${projectId}/schedules`, {
        name: scheduleName.trim() || '默认调度',
        interval_seconds: Math.max(60, Number(scheduleInterval) || 86400),
        policy: {
          mode: scheduleMode,
          max_cases: Math.max(1, Number(scheduleMax) || 30),
        },
      })
      toast.success('调度已创建')
      await loadAll()
    } catch (error) {
      toast.error('创建调度失败', String(error))
    } finally {
      setBusy('')
    }
  }

  async function toggleSchedule(item: ProjectSchedule) {
    setBusy(`schedule-${item.id}`)
    try {
      await api.patch(`/schedules/${item.id}`, { enabled: !item.enabled })
      await loadAll()
    } catch (error) {
      toast.error('更新调度失败', String(error))
    } finally {
      setBusy('')
    }
  }

  async function runSchedule(item: ProjectSchedule) {
    setBusy(`schedule-${item.id}`)
    try {
      const result = await api.post<Record<string, any>>(`/schedules/${item.id}/run-now`)
      toast.success(result.created ? '已创建执行' : '未创建执行', result.run_id ? `运行 #${result.run_id}` : result.status)
      await loadAll()
    } catch (error) {
      toast.error('立即执行失败', String(error))
    } finally {
      setBusy('')
    }
  }

  async function deleteSchedule(item: ProjectSchedule) {
    setBusy(`schedule-${item.id}`)
    try {
      await api.del(`/schedules/${item.id}`)
      toast.success('调度已删除')
      await loadAll()
    } catch (error) {
      toast.error('删除调度失败', String(error))
    } finally {
      setBusy('')
    }
  }

  async function addMember() {
    if (!projectId || !memberActor.trim()) return
    setBusy('member')
    try {
      await api.post(`/projects/${projectId}/members`, { actor: memberActor.trim(), role: memberRole })
      setMemberActor('')
      toast.success('成员已添加')
      await loadAll()
    } catch (error) {
      toast.error('添加成员失败', String(error))
    } finally {
      setBusy('')
    }
  }

  async function updateMember(item: ProjectMember, patch: Record<string, any>) {
    setBusy(`member-${item.id}`)
    try {
      await api.patch(`/members/${item.id}`, patch)
      await loadAll()
    } catch (error) {
      toast.error('更新成员失败', String(error))
    } finally {
      setBusy('')
    }
  }

  async function removeMember(item: ProjectMember) {
    setBusy(`member-${item.id}`)
    try {
      await api.del(`/members/${item.id}`)
      toast.success('成员已移除')
      await loadAll()
    } catch (error) {
      toast.error('移除成员失败', String(error))
    } finally {
      setBusy('')
    }
  }

  async function updateException(item: AutomationException, status: string) {
    setBusy(`exception-${item.id}`)
    try {
      await api.patch(`/exceptions/${item.id}`, {
        status,
        owner: exceptionOwner.trim() || item.owner,
        note: exceptionNote.trim() || item.note,
      })
      setExceptionNote('')
      toast.success('例外已更新')
      await loadAll()
    } catch (error) {
      toast.error('更新例外失败', String(error))
    } finally {
      setBusy('')
    }
  }

  async function escalate(force: boolean, dryRun: boolean) {
    if (!projectId) return
    setBusy('sla')
    try {
      const result = await api.post<SlaEscalationResult>(
        `/projects/${projectId}/sla/escalate?dry_run=${dryRun}&force=${force}`,
      )
      setEscalation(result)
      toast.success(dryRun ? 'SLA 预览完成' : 'SLA 升级已执行', result.reason ?? `命中 ${result.count} 条，新建 ${result.created} 个例外`)
      await loadAll()
    } catch (error) {
      toast.error('SLA 升级失败', String(error))
    } finally {
      setBusy('')
    }
  }

  async function previewRetirement() {
    if (!projectId) return
    setBusy('retire')
    try {
      setRetirement(await api.get<Record<string, any>>(`/projects/${projectId}/cases/retire?stale_days=30`))
    } catch (error) {
      toast.error('退役预览失败', String(error))
    } finally {
      setBusy('')
    }
  }

  async function applyRetirement() {
    if (!projectId) return
    setBusy('retire')
    try {
      setRetirement(
        await api.post<Record<string, any>>(
          `/projects/${projectId}/cases/retire?dry_run=false&stale_days=30&actor=console`,
        ),
      )
      toast.success('用例退役完成')
      await loadAll()
    } catch (error) {
      toast.error('用例退役失败', String(error))
    } finally {
      setBusy('')
    }
  }

  async function runPerformance() {
    if (!projectId) return
    setBusy('performance')
    try {
      await api.post(`/projects/${projectId}/performance/run`, {
        mode: perfMode,
        method: 'GET',
        path: perfPath.trim() || '/',
        concurrency: Math.max(1, Number(perfConcurrency) || 5),
        iterations: Math.max(1, Number(perfIterations) || 20),
        duration_seconds: perfDuration.trim() ? Number(perfDuration) : undefined,
      })
      toast.success('性能采样完成')
      await loadAll()
    } catch (error) {
      toast.error('性能采样失败', String(error))
    } finally {
      setBusy('')
    }
  }

  async function setBaseline() {
    if (!projectId || !baselineCase.trim()) return
    setBusy('baseline')
    try {
      await api.post(`/projects/${projectId}/visual/baselines`, {
        case_id: Number(baselineCase),
        name: baselineName.trim() || 'default',
      })
      toast.success('视觉基线已保存')
      await loadAll()
    } catch (error) {
      toast.error('保存视觉基线失败', String(error))
    } finally {
      setBusy('')
    }
  }

  async function loadA11y() {
    if (!projectId || !a11yCase.trim()) return
    setBusy('a11y')
    try {
      setA11y(await api.get<Record<string, any>>(`/projects/${projectId}/ui/a11y?case_id=${Number(a11yCase)}`))
    } catch (error) {
      toast.error('读取 a11y 失败', String(error))
    } finally {
      setBusy('')
    }
  }

  async function auditA11y() {
    if (!projectId) return
    setBusy('a11y')
    try {
      const metrics = (a11y?.metrics ?? {}) as Record<string, any>
      setA11y({
        ...(a11y ?? {}),
        audit: await api.post(`/projects/${projectId}/ui/a11y/audit`, metrics),
      })
    } catch (error) {
      toast.error('a11y 审计失败', String(error))
    } finally {
      setBusy('')
    }
  }

  async function saveSettings() {
    if (!project) return
    setBusy('settings')
    try {
      await api.patch(`/projects/${project.id}`, {
        settings: {
          ...(project.settings ?? {}),
          webhook_url: webhookUrl.trim() || null,
          notification_email: notificationEmail.trim() || null,
          automation: { ...(project.settings?.automation ?? {}), ...settings },
        },
      })
      toast.success('自动化设置已保存')
      reload()
    } catch (error) {
      toast.error('保存自动化设置失败', String(error))
    } finally {
      setBusy('')
    }
  }

  function toggleSetting(key: string) {
    setSettings((prev) => ({ ...prev, [key]: !prev[key] }))
  }

  function toggleDefaultOn(key: string) {
    setSettings((prev) => ({ ...prev, [key]: !(prev[key] !== false) }))
  }

  if (!project) return <div className="page"><Skeleton lines={8} /></div>

  return (
    <div className="page">
      <PageHead
        title="无人值守"
        subtitle="调度、角色、例外、质量、性能与视觉回归的统一控制台。后端 API 已有能力，这里提供可操作入口。"
        actions={<Button icon="refresh" loading={loading} onClick={loadAll}>刷新</Button>}
      />

      <Segmented options={TABS} value={tab} onChange={(value) => setTab(value)} />

      {tab === 'overview' && (
        <div className="settings-grid">
          <Card title="运行健康" subtitle="最近 run、队列、执行状态、例外与 SLA。">
            {health ? (
              <pre className="code code--scroll">{JSON.stringify(health, null, 2)}</pre>
            ) : (
              <span className="muted">暂无健康数据。</span>
            )}
          </Card>
          <Card title="环境就绪" subtitle="readiness 探针结果；环境异常会进入例外队列。">
            <div className="row-actions">
              <Pill tone={readiness?.ok ? 'success' : 'danger'}>{readiness?.ok ? 'ready' : 'not ready'}</Pill>
              <span className="muted small">{summaryValue(readiness?.reason)}</span>
            </div>
            {Array.isArray(readiness?.probes) && (
              <div className="stack">
                {readiness!.probes.map((probe: Record<string, any>, index: number) => (
                  <div key={index} className="row-actions">
                    <Pill tone={probe.ok ? 'success' : 'danger'}>{probe.ok ? 'ok' : 'fail'}</Pill>
                    <code className="small">{probe.url}</code>
                    <span className="muted small">{summaryValue(probe.status_code ?? probe.error)}</span>
                  </div>
                ))}
              </div>
            )}
          </Card>
          <Card title="CI gate" subtitle="把一次 run 收敛成 pass/fail；旧 run 的 SLO 窗口有上界。">
            <div className="form-row">
              <Field label="Run ID（留空取最新）">
                <input className="input mono" value={gateRunId} onChange={(event) => setGateRunId(event.target.value)} placeholder="123" />
              </Field>
              <Field label="操作">
                <Button icon="shield" loading={busy === 'gate'} onClick={() => refreshGate(gateRunId.trim() || undefined)}>
                  重新判定
                </Button>
              </Field>
            </div>
            {gate && (
              <div className="stack">
                <div className="row-actions">
                  <Pill tone={gate.passed ? 'success' : 'danger'}>{gate.status}</Pill>
                  <span className="muted small">run #{gate.run_id ?? '—'} · {gate.run_status ?? '—'}</span>
                </div>
                {gate.blockers.map((item, index) => (
                  <div key={index} className="row-actions">
                    <Pill tone="danger">{GATE_BLOCKER[String(item.kind)] ?? String(item.kind)}</Pill>
                    <span className="small">{String(item.message)}</span>
                  </div>
                ))}
                {!gate.blockers.length && <span className="muted small">没有阻断项。</span>}
              </div>
            )}
          </Card>
        </div>
      )}

      {tab === 'schedules' && (
        <div className="settings-grid">
          <Card title="新建调度" subtitle="定时触发 full / smoke / impacted 运行策略。">
            <div className="settings-fields">
              <Field label="名称"><input className="input" value={scheduleName} onChange={(event) => setScheduleName(event.target.value)} /></Field>
              <div className="form-row">
                <Field label="间隔（秒）"><input className="input mono" type="number" min={60} value={scheduleInterval} onChange={(event) => setScheduleInterval(event.target.value)} /></Field>
                <Field label="模式">
                  <select className="input" value={scheduleMode} onChange={(event) => setScheduleMode(event.target.value)}>
                    <option value="smoke">smoke</option>
                    <option value="full">full</option>
                    <option value="impacted">impacted</option>
                  </select>
                </Field>
                <Field label="最多用例"><input className="input mono" type="number" min={1} value={scheduleMax} onChange={(event) => setScheduleMax(event.target.value)} /></Field>
              </div>
              <Button icon="plus" loading={busy === 'schedule'} onClick={createSchedule}>创建调度</Button>
            </div>
          </Card>
          <Card title="调度列表" subtitle="启用 / 停用、立即执行、删除。">
            {!schedules.length && <span className="muted small">还没有调度。</span>}
            <div className="stack">
              {schedules.map((item) => (
                <div key={item.id} className="row-actions">
                  <Pill tone={item.enabled ? 'success' : 'muted'}>{item.enabled ? 'enabled' : 'disabled'}</Pill>
                  <strong>{item.name}</strong>
                  <span className="muted small">每 {item.interval_seconds}s · {String(item.policy?.mode ?? 'full')}</span>
                  <span className="muted small">下次 {item.next_run_at ? formatFullTime(item.next_run_at) : '—'}</span>
                  {item.last_status && <Pill tone={item.last_status === 'completed' ? 'success' : 'warning'}>{item.last_status}</Pill>}
                  <div className="spacer" />
                  <Button size="sm" onClick={() => runSchedule(item)} loading={busy === `schedule-${item.id}`}>立即执行</Button>
                  <Button size="sm" onClick={() => toggleSchedule(item)} loading={busy === `schedule-${item.id}`}>{item.enabled ? '停用' : '启用'}</Button>
                  <Button size="sm" variant="danger" onClick={() => deleteSchedule(item)} loading={busy === `schedule-${item.id}`}>删除</Button>
                </div>
              ))}
            </div>
          </Card>
        </div>
      )}

      {tab === 'access' && (
        <div className="settings-grid">
          <Card title="添加成员" subtitle="角色用于 MCP / API 权限；owner > maintainer > viewer。">
            <div className="settings-fields">
              <Field label="Actor"><input className="input" value={memberActor} onChange={(event) => setMemberActor(event.target.value)} placeholder="qa-lead / ci-bot" /></Field>
              <Field label="角色">
                <select className="input" value={memberRole} onChange={(event) => setMemberRole(event.target.value)}>
                  <option value="viewer">viewer · 只读</option>
                  <option value="maintainer">maintainer · 读写</option>
                  <option value="owner">owner · 成员管理</option>
                </select>
              </Field>
              <Button icon="plus" loading={busy === 'member'} onClick={addMember}>添加成员</Button>
            </div>
          </Card>
          <Card title="成员 / 角色" subtitle="MCP client 的 actor / name 匹配这里；未匹配时按 token scope 推导。">
            {!members.length && <span className="muted small">还没有成员。</span>}
            <div className="stack">
              {members.map((item) => (
                <div key={item.id} className="row-actions">
                  <Pill tone={item.active ? 'success' : 'muted'}>{item.active ? 'active' : 'inactive'}</Pill>
                  <strong>{item.actor}</strong>
                  <select className="input input--sm" value={item.role} onChange={(event) => updateMember(item, { role: event.target.value })}>
                    <option value="viewer">viewer</option>
                    <option value="maintainer">maintainer</option>
                    <option value="owner">owner</option>
                  </select>
                  <Button size="sm" onClick={() => updateMember(item, { active: !item.active })} loading={busy === `member-${item.id}`}>
                    {item.active ? '停用' : '启用'}
                  </Button>
                  <Button size="sm" variant="danger" onClick={() => removeMember(item)} loading={busy === `member-${item.id}`}>移除</Button>
                </div>
              ))}
            </div>
          </Card>
        </div>
      )}

      {tab === 'exceptions' && (
        <div className="settings-grid">
          <Card title="例外队列" subtitle="环境、低置信度分诊、SLA 超时等需要人处理的事项。">
            {!openExceptions.length && <span className="muted small">没有未关闭例外。</span>}
            <div className="stack">
              {openExceptions.map((item) => (
                <div key={item.id} className="stack">
                  <div className="row-actions">
                    <Pill tone={meta(SEVERITY, item.severity).tone}>{meta(SEVERITY, item.severity).label}</Pill>
                    <strong>{EXCEPTION_KIND[item.kind] ?? item.kind}</strong>
                    <span className="muted small">
                      {EXCEPTION_TARGET[item.target_type] ?? item.target_type} #{item.target_id ?? '—'} ·{' '}
                      {EXCEPTION_STATUS[item.status] ?? item.status}
                    </span>
                    {item.owner && <span className="muted small">负责人 {item.owner}</span>}
                  </div>
                  <span className="small">{item.note}</span>
                  <div className="row-actions">
                    <input className="input input--sm" placeholder="负责人" value={exceptionOwner} onChange={(event) => setExceptionOwner(event.target.value)} />
                    <input className="input input--sm" placeholder="处理备注" value={exceptionNote} onChange={(event) => setExceptionNote(event.target.value)} />
                    <Button size="sm" onClick={() => updateException(item, 'acked')} loading={busy === `exception-${item.id}`}>ack</Button>
                    <Button size="sm" onClick={() => updateException(item, 'resolved')} loading={busy === `exception-${item.id}`}>resolve</Button>
                    <Button size="sm" variant="ghost" onClick={() => updateException(item, 'ignored')} loading={busy === `exception-${item.id}`}>ignore</Button>
                  </div>
                </div>
              ))}
            </div>
          </Card>
          <Card title="SLA 超时" subtitle="项目未开启 SLA 升级时，执行会 no-op；强制执行需要显式 force。">
            <div className="row-actions">
              <Pill tone={breaches?.count ? 'danger' : 'success'}>{breaches?.count ?? 0} 条超时</Pill>
              <span className="muted small">未指派 {breaches?.unassigned ?? 0}</span>
              <div className="spacer" />
              <Button size="sm" onClick={() => escalate(false, true)} loading={busy === 'sla'}>预览升级</Button>
              <Button size="sm" onClick={() => escalate(false, false)} loading={busy === 'sla'}>执行升级</Button>
              <Button size="sm" variant="danger" onClick={() => escalate(true, false)} loading={busy === 'sla'}>强制执行</Button>
            </div>
            <div className="stack">
              {(breaches?.items ?? []).map((item) => (
                <div key={item.finding_id} className="row-actions">
                  <Pill tone={item.severity === 'critical' || item.severity === 'high' ? 'danger' : 'warning'}>{item.severity}</Pill>
                  <span>#{item.finding_id} {item.title}</span>
                  <span className="muted small">超时 {item.overdue_minutes} 分钟 · {item.assignee || '未指派'}</span>
                </div>
              ))}
            </div>
            {escalation && (
              <div className="notice notice--info">
                <span>{escalation.reason ?? `命中 ${escalation.count}，新建 ${escalation.created}`}</span>
              </div>
            )}
          </Card>
        </div>
      )}

      {tab === 'quality' && (
        <div className="settings-grid">
          <Card title="生成后试运行" subtitle="有步骤 + 有依据断言的待确认用例先跑一次；通过自动启用，失败进入复核。">
            {validation ? (
              <div className="stack">
                <div className="run-detail-kpis">
                  <div><span>扫描 pending</span><strong>{validation.pending_scanned}</strong></div>
                  <div><span>可试运行</span><strong>{validation.eligible}</strong></div>
                  <div><span>通过后自动启用</span><strong>{validation.auto_enable_after_validation ? '开' : '关'}</strong></div>
                </div>
                {validation.reasons && Object.keys(validation.reasons).length > 0 && (
                  <span className="muted small">
                    挡住：{Object.entries(validation.reasons)
                      .map(([key, count]) => `${VALIDATION_REASON[key] ?? key}:${count}`)
                      .join(' / ')}
                  </span>
                )}
                <div className="row-actions">
                  <Button size="sm" variant="primary" loading={busy === 'validation'} onClick={runValidation}>
                    试运行一批（50 条）
                  </Button>
                  {validation.active_run_id && <Pill tone="info">排队中 run #{validation.active_run_id}</Pill>}
                </div>
              </div>
            ) : <span className="muted small">暂无待确认用例。</span>}
          </Card>
          <Card title="断言质量" subtitle="强度评分 / 弱断言 / 无断言用例；探针用例默认不写断言。">
            {quality ? (
              <div className="stack">
                <div className="run-detail-kpis">
                  <div><span>用例</span><strong>{quality.cases}</strong></div>
                  <div><span>断言</span><strong>{quality.assertions}</strong></div>
                  <div><span>平均分</span><strong>{quality.average_score}</strong></div>
                  <div><span>弱断言</span><strong className="text-warning">{quality.bands.weak ?? 0}</strong></div>
                  <div><span>无断言</span><strong className="text-danger">{quality.bands.no_assertions ?? 0}</strong></div>
                  {(quality.probe_cases ?? 0) > 0 && (
                    <div><span>其中探针</span><strong>{quality.probe_cases}</strong></div>
                  )}
                </div>
                {(quality.weakest_cases ?? []).slice(0, 8).map((item: Record<string, any>) => (
                  <div key={String(item.case_id)} className="row-actions">
                    <Pill tone="warning">{item.average_score}</Pill>
                    <span>#{item.case_id} {item.title}</span>
                  </div>
                ))}
              </div>
            ) : <span className="muted small">暂无数据。</span>}
          </Card>
          <Card title="Oracle mutation" subtitle="用最近成功响应构造错误响应，统计断言 kill rate。">
            {mutation ? (
              <pre className="code code--scroll">{JSON.stringify(mutation, null, 2)}</pre>
            ) : <span className="muted small">暂无数据。</span>}
          </Card>
          <Card title="Oracle 趋势" subtitle="按最近 run 统计断言来源与 pending 数量。">
            {!trend.length && <span className="muted small">样本不足。</span>}
            <div className="stack">
              {trend.slice(-8).map((point) => (
                <div key={point.run_id} className="row-actions">
                  <Pill tone="muted">run #{point.run_id}</Pill>
                  <span className="small">{point.assertions} 断言 · pending {point.pending}</span>
                  <span className="muted small">contract {point.oracle_mix.contract ?? 0} / policy {point.oracle_mix.policy ?? 0}</span>
                </div>
              ))}
            </div>
          </Card>
          <Card title="用例退役" subtitle="过期 / 重复 / 无步骤用例预览；默认先 dry-run。">
            <div className="row-actions">
              <Button size="sm" loading={busy === 'retire'} onClick={previewRetirement}>预览</Button>
              <Button size="sm" variant="danger" loading={busy === 'retire'} onClick={applyRetirement}>执行退役</Button>
            </div>
            {retirement && (
              <div className="stack">
                <span className="small">命中 {retirement.count ?? 0} 条{retirement.dry_run === false ? '（已执行）' : '（预览）'}</span>
                {(retirement.cases ?? []).slice(0, 10).map((item: Record<string, any>) => (
                  <div key={String(item.id)} className="row-actions">
                    <Pill tone="warning">{String(item.reason ?? 'retire')}</Pill>
                    <span>#{item.id} {item.title}</span>
                  </div>
                ))}
              </div>
            )}
          </Card>
        </div>
      )}

      {tab === 'attack' && (
        <div className="stack">
          <Card
            title="攻击用例包"
            subtitle="按可扩展的攻击模式生成对抗用例：越权、重定向、改密会话、CSRF、路径穿越、依赖漏洞。"
          >
            {!attackPack ? (
              <Skeleton />
            ) : (
              <div className="stack">
                <div className="row-actions">
                  <Pill tone="info">{attackPack.summary?.patterns ?? 0} 个模式</Pill>
                  <Pill tone="success">{attackPack.summary?.applicable ?? 0} 适用</Pill>
                  <Pill tone="warning">{attackPack.summary?.new_cases ?? 0} 条待生成</Pill>
                  <Pill tone="danger">{attackPack.summary?.new_findings ?? 0} 条新发现</Pill>
                  <div className="spacer" />
                  <Button size="sm" icon="refresh" loading={busy === 'attack'} onClick={() => scanAttackPack()}>
                    重新扫描
                  </Button>
                  <Button size="sm" icon="play" loading={busy === 'attack-lab'} onClick={runSecurityLab}>
                    隔离实验室
                  </Button>
                  <Button
                    size="sm"
                    icon="sparkles"
                    loading={busy === 'attack-generate'}
                    onClick={() => generateAttackPack(false)}
                  >
                    生成用例
                  </Button>
                  <Button
                    size="sm"
                    variant="primary"
                    icon="play"
                    loading={busy === 'attack-validate'}
                    onClick={() => generateAttackPack(true)}
                  >
                    生成并验证
                  </Button>
                </div>
                <span className="muted small">
                  生成是幂等的：同一条攻击用例只创建一次；已存在的用例会保留人工修改。供应链类问题直接落缺陷，不生成无法执行的用例。
                </span>
                {attackPack.dependencies && (
                  <span className="muted small">
                    依赖清单：{attackPack.dependencies.manifests?.length ?? 0} 个
                    {" · "}直接依赖 {attackPack.dependencies.count ?? 0} 个
                    {attackPack.dependencies.toolchains?.length
                      ? ` · 工具链 ${attackPack.dependencies.toolchains.map((item: any) => `${item.name} ${item.version}`).join('、')}`
                      : ''}
                    {attackPack.dependencies.errors?.length ? ` · ${attackPack.dependencies.errors.join('；')}` : ''}
                  </span>
                )}
              </div>
            )}
          </Card>

          {(attackPack?.findings?.length ?? 0) > 0 && (
            <Card title="供应链 / 依赖发现" subtitle="来自 OSV 与工具链生命周期检查，已直接确认。">
              <div className="stack">
                {(attackPack?.findings ?? []).map((item: any) => (
                  <div key={item.key} className="stack">
                    <div className="row-actions">
                      <Pill tone={meta(SEVERITY, item.severity).tone}>{meta(SEVERITY, item.severity).label}</Pill>
                      <strong>{item.title}</strong>
                      {item.existing_finding_id && (
                        <Link className="small" to={`/projects/${projectId}/findings?finding=${item.existing_finding_id}`}>
                          缺陷 #{item.existing_finding_id}
                        </Link>
                      )}
                    </div>
                    <span className="small muted" style={{ whiteSpace: 'pre-wrap' }}>{item.description}</span>
                  </div>
                ))}
              </div>
            </Card>
          )}

          <Card title="攻击模式" subtitle="每个模式声明适用条件、生成的用例与策略依据；新增模式只需要注册一个类。">
            {!attackPack ? (
              <Skeleton />
            ) : (
              <div className="stack">
                {attackPack.patterns?.map((item: any) => (
                  <div key={item.id} className="stack">
                    <div className="row-actions">
                      <Pill tone={item.applicable ? 'success' : 'muted'}>{item.applicable ? '适用' : '不适用'}</Pill>
                      <strong>{item.title}</strong>
                      <Pill tone={meta(SEVERITY, item.severity).tone}>{meta(SEVERITY, item.severity).label}</Pill>
                      <span className="muted small">{ATTACK_CATEGORY[item.category] ?? item.category}</span>
                      <span className="muted small">
                        {item.cases} 条用例 · {item.findings} 条发现
                      </span>
                    </div>
                    <span className="small muted">{item.description}</span>
                    {!item.applicable && item.reason && <span className="small">不适用：{item.reason}</span>}
                  </div>
                ))}
              </div>
            )}
          </Card>

          {(attackPack?.cases?.length ?? 0) > 0 && (
            <Card title="攻击用例" subtitle="点击用例号进入详情；已存在表示幂等命中，不会重复创建。">
              <div className="stack">
                {(attackPack?.cases ?? []).map((item: any) => (
                  <div key={item.attack_key} className="row-actions">
                    <Pill tone="muted">{item.pattern_id}</Pill>
                    {item.existing_case_id ? (
                      <Link className="small" to={`/projects/${projectId}/cases/${item.existing_case_id}`}>
                        用例 #{item.existing_case_id}
                      </Link>
                    ) : (
                      <span className="muted small">待生成</span>
                    )}
                    <span className="small">{item.title}</span>
                    <span className="muted small">{item.policy_ref}</span>
                  </div>
                ))}
              </div>
            </Card>
          )}
        </div>
      )}

      {tab === 'performance' && (
        <div className="settings-grid">
          <Card title="性能采样" subtitle="baseline / load / stress / soak；stress 会输出阶梯与 breakpoint。">
            <div className="settings-fields">
              <div className="form-row">
                <Field label="模式">
                  <select className="input" value={perfMode} onChange={(event) => setPerfMode(event.target.value)}>
                    <option value="baseline">baseline</option>
                    <option value="load">load</option>
                    <option value="stress">stress</option>
                    <option value="soak">soak</option>
                  </select>
                </Field>
                <Field label="Path"><input className="input mono" value={perfPath} onChange={(event) => setPerfPath(event.target.value)} /></Field>
              </div>
              <div className="form-row">
                <Field label="并发"><input className="input mono" type="number" min={1} value={perfConcurrency} onChange={(event) => setPerfConcurrency(event.target.value)} /></Field>
                <Field label="迭代"><input className="input mono" type="number" min={1} value={perfIterations} onChange={(event) => setPerfIterations(event.target.value)} /></Field>
                <Field label="持续秒数（可选）"><input className="input mono" type="number" min={0} value={perfDuration} onChange={(event) => setPerfDuration(event.target.value)} /></Field>
              </div>
              <Button icon="gauge" loading={busy === 'performance'} onClick={runPerformance}>开始采样</Button>
            </div>
          </Card>
          <Card title="历史与退化" subtitle="最近采样、SLO 通过情况与基线退化。">
            {performance?.degradation && (
              <div className="notice notice--warning">
                <span>{performance.degradation.degraded ? '检测到退化' : '未检测到退化'} · {summaryValue(performance.degradation.reason ?? performance.degradation.reasons)}</span>
              </div>
            )}
            {!performance?.items.length && <span className="muted small">还没有性能结果。</span>}
            <div className="stack">
              {(performance?.items ?? []).slice(-10).reverse().map((item) => (
                <div key={item.id} className="row-actions">
                  <Pill tone={item.slo_passed ? 'success' : 'danger'}>{item.mode}</Pill>
                  <span className="mono small">{item.method} {item.path}</span>
                  <span className="muted small">p95 {item.p95_ms}ms · {item.requests_per_second} rps · {percent(item.success_rate)}</span>
                  <span className="muted small">{item.created_at ? formatFullTime(item.created_at) : ''}</span>
                </div>
              ))}
            </div>
          </Card>
        </div>
      )}

      {tab === 'visual' && (
        <div className="settings-grid">
          <Card title="视觉基线" subtitle="用最近一次浏览器用例截图的 hash 建立基线。">
            <div className="settings-fields">
              <div className="form-row">
                <Field label="用例 ID"><input className="input mono" value={baselineCase} onChange={(event) => setBaselineCase(event.target.value)} /></Field>
                <Field label="基线名称"><input className="input" value={baselineName} onChange={(event) => setBaselineName(event.target.value)} /></Field>
              </div>
              <Button icon="eye" loading={busy === 'baseline'} onClick={setBaseline}>保存为基线</Button>
            </div>
            <div className="stack">
              {!baselines.length && <span className="muted small">还没有视觉基线。</span>}
              {baselines.map((item) => (
                <div key={item.id} className="row-actions">
                  <Pill tone="muted">case #{item.case_id}</Pill>
                  <span>{item.name}</span>
                  <code className="small mono">{item.sha256.slice(0, 12)}…</code>
                  <span className="muted small">{formatFullTime(item.created_at)}</span>
                </div>
              ))}
            </div>
          </Card>
          <Card title="a11y 审计" subtitle="读取用例最近一次浏览器 a11y 指标并按 WCAG 规则判定。">
            <div className="form-row">
              <Field label="用例 ID"><input className="input mono" value={a11yCase} onChange={(event) => setA11yCase(event.target.value)} /></Field>
              <Field label="操作">
                <Button size="sm" loading={busy === 'a11y'} onClick={loadA11y}>读取</Button>
              </Field>
              <Field label=" ">
                <Button size="sm" icon="shield" loading={busy === 'a11y'} onClick={auditA11y}>重新审计</Button>
              </Field>
            </div>
            {a11y && <pre className="code code--scroll">{JSON.stringify(a11y, null, 2)}</pre>}
          </Card>
        </div>
      )}

      {tab === 'settings' && (
        <div className="settings-grid">
          <Card title="通知与摘要" subtitle="webhook 与邮件互相独立；只配邮件也能收到 digest。">
            <div className="settings-fields">
              <Field label="Webhook URL"><input className="input mono" value={webhookUrl} onChange={(event) => setWebhookUrl(event.target.value)} placeholder="https://example.com/hooks/shakedown" /></Field>
              <Field label="通知邮箱"><input className="input mono" value={notificationEmail} onChange={(event) => setNotificationEmail(event.target.value)} placeholder="qa@example.com" /></Field>
              <div className="row-actions">
                <label className="row-actions"><input type="checkbox" checked={!!settings.digest_enabled} onChange={() => toggleSetting('digest_enabled')} /> 启用定时 digest</label>
                <Field label="间隔（秒）">
                  <input className="input mono" type="number" min={3600} value={settings.digest_interval_seconds ?? 86400} onChange={(event) => setSettings((prev) => ({ ...prev, digest_interval_seconds: Number(event.target.value) }))} />
                </Field>
              </div>
            </div>
          </Card>
          <Card title="SLA / 变更 / Flaky" subtitle="这些开关同时影响 worker 调度和手工 API。">
            <div className="stack">
              <label className="row-actions"><input type="checkbox" checked={!!settings.sla_escalation_enabled} onChange={() => toggleSetting('sla_escalation_enabled')} /> SLA 自动升级</label>
              <label className="row-actions"><input type="checkbox" checked={!!settings.auto_enrich_impacted} onChange={() => toggleSetting('auto_enrich_impacted')} /> 变更影响自动 enrich</label>
              <label className="row-actions"><input type="checkbox" checked={!!settings.auto_rerun_impacted} onChange={() => toggleSetting('auto_rerun_impacted')} /> 变更影响自动回归</label>
              <label className="row-actions"><input type="checkbox" checked={!!settings.flaky_quarantine} onChange={() => toggleSetting('flaky_quarantine')} /> Flaky 自动隔离</label>
              <label className="row-actions"><input type="checkbox" checked={settings.auto_validate_generated !== false} onChange={() => toggleDefaultOn('auto_validate_generated')} /> 生成后自动试运行</label>
              <label className="row-actions"><input type="checkbox" checked={settings.auto_enable_after_validation !== false} onChange={() => toggleDefaultOn('auto_enable_after_validation')} /> 试运行通过后自动启用</label>
              <label className="row-actions"><input type="checkbox" checked={settings.auto_generate_on_bootstrap !== false} onChange={() => toggleDefaultOn('auto_generate_on_bootstrap')} /> 抽取功能点后自动生成用例（仅空项目）</label>
              <div className="form-row">
                <Field label="SLA 最低严重度">
                  <select className="input" value={settings.sla_escalation_min_severity ?? 'medium'} onChange={(event) => setSettings((prev) => ({ ...prev, sla_escalation_min_severity: event.target.value }))}>
                    <option value="low">low</option>
                    <option value="medium">medium</option>
                    <option value="high">high</option>
                    <option value="critical">critical</option>
                  </select>
                </Field>
                <Field label="命令执行">
                  <select className="input" value={settings.command_execution ?? 'sandbox'} onChange={(event) => setSettings((prev) => ({ ...prev, command_execution: event.target.value }))}>
                    <option value="sandbox">sandbox · 默认</option>
                    <option value="host">host · 需显式允许</option>
                  </select>
                </Field>
              </div>
              <label className="row-actions"><input type="checkbox" checked={!!settings.allow_host_commands} onChange={() => toggleSetting('allow_host_commands')} /> 允许宿主命令（高风险）</label>
              <label className="row-actions"><input type="checkbox" checked={!!settings.allow_local_sandbox_commands} onChange={() => toggleSetting('allow_local_sandbox_commands')} /> 允许 local 沙箱命令（无隔离）</label>
            </div>
          </Card>
          <Card title="保存" subtitle="保存后 worker 会在下一轮调度读取新配置。">
            <Button variant="primary" icon="check" loading={busy === 'settings'} onClick={saveSettings}>保存自动化设置</Button>
          </Card>
          <Card title="相关页面" subtitle="质量 / 缺口 / 运行详情。">
            <div className="row-actions">
              <Link className="btn btn--secondary btn--sm" to={`/projects/${project.id}/knowledge/gaps`}><Icon name="alert" size={14} /> 契约缺口</Link>
              <Link className="btn btn--secondary btn--sm" to={`/projects/${project.id}/runs`}><Icon name="play" size={14} /> 执行记录</Link>
              <Link className="btn btn--secondary btn--sm" to={`/projects/${project.id}/settings`}><Icon name="settings" size={14} /> 项目设置</Link>
            </div>
          </Card>
        </div>
      )}
    </div>
  )
}
