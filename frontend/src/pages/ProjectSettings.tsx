import { useEffect, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { useProject } from '../app/project'
import { api } from '../lib/api'
import { formatFullTime } from '../lib/format'
import { Icon } from '../ui/icons'
import { Button, Card, Field, PageHead, Pill, Skeleton } from '../ui/kit'
import type { WebhookDeliveriesResponse } from '../lib/types'
import { ConfirmDialog, useToast } from '../ui/overlays'

interface Account {
  username?: string
  password?: string
  token?: string
  role?: string
  [key: string]: unknown
}

const MODE_LABEL: Record<string, string> = {
  prefix: 'prefix · run_id 前缀 + 事后清理',
  reset: 'reset · 每次整库重置',
}

export default function ProjectSettings() {
  const { project, reload } = useProject()
  const navigate = useNavigate()
  const toast = useToast()

  const [name, setName] = useState('')
  const [baseUrl, setBaseUrl] = useState('')
  const [description, setDescription] = useState('')
  const [tenant, setTenant] = useState('default')
  const [accounts, setAccounts] = useState<Record<string, Account>>({})
  const [dsn, setDsn] = useState('')
  const [readDsn, setReadDsn] = useState('')
  const [mode, setMode] = useState('prefix')
  const [dbService, setDbService] = useState('')
  const [dbName, setDbName] = useState('')
  const [dbTemplate, setDbTemplate] = useState('postgres')
  const [perRun, setPerRun] = useState(false)
  const [initScript, setInitScript] = useState('')
  const [startCommand, setStartCommand] = useState('')
  const [stopCommand, setStopCommand] = useState('')
  const [migrateCommand, setMigrateCommand] = useState('')
  const [seedCommand, setSeedCommand] = useState('')
  const [accountCommand, setAccountCommand] = useState('')
  const [cleanupCommand, setCleanupCommand] = useState('')
  const [allowExecute, setAllowExecute] = useState(false)
  const [waitSeconds, setWaitSeconds] = useState('30')
  const [accountCheck, setAccountCheck] = useState<Record<string, any> | null>(null)
  const [allowedHosts, setAllowedHosts] = useState('')
  const [riskWhitelist, setRiskWhitelist] = useState('')
  const [healthUrl, setHealthUrl] = useState('')
  const [performanceSlaMs, setPerformanceSlaMs] = useState('')
  const [performanceSamples, setPerformanceSamples] = useState('20')
  const [targetType, setTargetType] = useState('external')
  const [githubRepo, setGithubRepo] = useState('')
  const [githubLabels, setGithubLabels] = useState('')
  const [jiraProjectKey, setJiraProjectKey] = useState('')
  const [jiraIssueType, setJiraIssueType] = useState('Bug')
  const [webhookUrl, setWebhookUrl] = useState('')
  const [notificationEmail, setNotificationEmail] = useState('')
  const [digestEnabled, setDigestEnabled] = useState(false)
  const [digestInterval, setDigestInterval] = useState('86400')
  const [deliveries, setDeliveries] = useState<WebhookDeliveriesResponse | null>(null)
  const [deliveriesLoading, setDeliveriesLoading] = useState(false)
  const [pushBusy, setPushBusy] = useState(false)
  const [busy, setBusy] = useState(false)
  const [deleteOpen, setDeleteOpen] = useState(false)

  useEffect(() => {
    if (!project) return
    setName(project.name ?? '')
    setBaseUrl(project.base_url ?? '')
    setDescription(project.description ?? '')
    setTenant(project.tenant ?? 'default')
    setAccounts((project.test_accounts as Record<string, Account>) ?? {})
    setMode(project.isolation?.mode ?? 'prefix')
    setDbService(project.isolation?.db_service ?? '')
    setDbName(project.isolation?.db_name ?? '')
    setInitScript(project.isolation?.init_script ?? '')
    setAllowedHosts((project.isolation?.allowed_hosts ?? []).join('\n'))
    setRiskWhitelist(((project.settings?.risk_whitelist ?? []) as string[]).join('\n'))
    setHealthUrl((project.target_start?.health_url as string) ?? '')
    setPerformanceSlaMs(String(project.settings?.performance_sla_ms ?? ''))
    setPerformanceSamples(String(project.settings?.performance_samples ?? 20))
    setTargetType((project.target_start?.type as string) ?? 'external')
    const integrations = (project.settings?.integrations ?? {}) as Record<string, any>
    setGithubRepo(String(integrations.github?.repo ?? ''))
    setGithubLabels(String(integrations.github?.labels ?? ''))
    setJiraProjectKey(String(integrations.jira?.project_key ?? ''))
    setJiraIssueType(String(integrations.jira?.issue_type ?? 'Bug'))
    setWebhookUrl(String(project.settings?.webhook_url ?? ''))
    setNotificationEmail(String(project.settings?.notification_email ?? ''))
    const automation = (project.settings?.automation ?? {}) as Record<string, any>
    setDigestEnabled(Boolean(automation.digest_enabled))
    setDigestInterval(String(automation.digest_interval_seconds ?? 86400))
  }, [project])

  useEffect(() => {
    if (!project) return
    loadDeliveries(project.id)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [project?.id])

  if (!project) {
    return (
      <div className="page">
        <PageHead title="项目设置" />
        <Skeleton lines={8} />
      </div>
    )
  }

  async function validateAccounts() {
    setBusy(true)
    try {
      const result = await api.post<Record<string, any>>(`/projects/${project!.id}/accounts/validate`, {})
      setAccountCheck(result)
      if (result.failed) {
        toast.error('账号预检有失败项', `失败 ${result.failed} 个，查看下方明细`)
      } else {
        toast.success('账号预检完成', `检查 ${result.total} 个，跳过 ${result.skipped} 个`)
      }
    } catch (error) {
      toast.error('账号预检失败', String(error))
    } finally {
      setBusy(false)
    }
  }

  async function save() {
    setBusy(true)
    try {
      await api.patch(`/projects/${project!.id}`, {
        name: name.trim(),
        base_url: baseUrl.trim(),
        description,
        tenant: tenant.trim() || 'default',
        test_accounts: accounts,
        target_dsn: dsn || undefined,
        target_read_dsn: readDsn || undefined,
        isolation: {
          ...(project!.isolation ?? {}),
          mode,
          db_service: dbService,
          db_name: dbName,
          db_template: dbTemplate || 'postgres',
          per_run: perRun,
          init_script: initScript,
          allowed_hosts: allowedHosts.split('\n').map((item) => item.trim()).filter(Boolean),
        },
        target_start: {
          ...(project!.target_start ?? {}),
          type: targetType,
          health_url: healthUrl,
          allow_execute: allowExecute,
          start_command: startCommand,
          stop_command: stopCommand,
          migrate_command: migrateCommand,
          seed_command: seedCommand,
          account_command: accountCommand,
          cleanup_command: cleanupCommand,
          wait_seconds: Number(waitSeconds) || 30,
        },
        settings: {
          ...(project!.settings ?? {}),
          performance_sla_ms: performanceSlaMs.trim() ? Number(performanceSlaMs) : null,
          performance_samples: performanceSamples.trim() ? Number(performanceSamples) : 20,
          webhook_url: webhookUrl.trim() || null,
          notification_email: notificationEmail.trim() || null,
          automation: {
            ...(project!.settings?.automation ?? {}),
            digest_enabled: digestEnabled,
            digest_interval_seconds: Math.max(3600, Number(digestInterval) || 86400),
          },
          risk_whitelist: riskWhitelist.split('\n').map((item) => item.trim()).filter(Boolean),
          integrations: {
            ...(project!.settings?.integrations ?? {}),
            github: {
              ...(project!.settings?.integrations?.github ?? {}),
              repo: githubRepo.trim(),
              labels: githubLabels.trim(),
            },
            jira: {
              ...(project!.settings?.integrations?.jira ?? {}),
              project_key: jiraProjectKey.trim(),
              issue_type: jiraIssueType.trim() || 'Bug',
            },
          },
        },
      })
      toast.success('项目设置已保存')
      reload()
    } catch (error) {
      toast.error('保存失败', String(error))
    } finally {
      setBusy(false)
    }
  }

  async function loadDeliveries(projectId?: number) {
    if (!projectId) return
    setDeliveriesLoading(true)
    try {
      setDeliveries(await api.get<WebhookDeliveriesResponse>(`/projects/${projectId}/webhook-deliveries?limit=20`))
    } catch {
      setDeliveries(null)
    } finally {
      setDeliveriesLoading(false)
    }
  }

  async function sendDigest() {
    if (!webhookUrl.trim() && !notificationEmail.trim()) {
      toast.error('请先配置 Webhook 或通知邮箱', '未配置通知目标时无法主动推送摘要')
      return
    }
    setPushBusy(true)
    try {
      await api.post(`/projects/${project!.id}/push/digest`, {})
      toast.success('摘要已排队', '会包含 actionable 缺陷与契约缺口摘要；推送失败会自动重试，不阻塞执行主流程')
      await loadDeliveries(project!.id)
    } catch (error) {
      toast.error('摘要推送失败', String(error))
    } finally {
      setPushBusy(false)
    }
  }

  async function retryDelivery(deliveryId: number) {
    setPushBusy(true)
    try {
      await api.post(`/projects/${project!.id}/webhook-deliveries/${deliveryId}/retry`, {})
      toast.success('已重新排队', `投递 #${deliveryId}`)
      await loadDeliveries(project!.id)
    } catch (error) {
      toast.error('重试失败', String(error))
    } finally {
      setPushBusy(false)
    }
  }

  async function remove() {
    setBusy(true)
    try {
      await api.del(`/projects/${project!.id}`)
      toast.success('项目已删除')
      navigate('/')
    } catch (error) {
      toast.error('删除失败', String(error))
      setBusy(false)
    }
  }

  function setAccountField(accountName: string, key: string, value: string) {
    setAccounts((prev) => ({ ...prev, [accountName]: { ...(prev[accountName] ?? {}), [key]: value } }))
  }

  return (
    <div className="page">
      <PageHead
        title="项目设置"
        subtitle={
          <span className="crumb-inline">
            <Pill tone="muted">#{project.id}</Pill>
            创建于 {formatFullTime(project.created_at)}
          </span>
        }
        actions={<Button variant="primary" icon="check" loading={busy} onClick={save}>保存设置</Button>}
      />

      <div className="settings-grid settings-grid--project">
        <Card title="基本信息" subtitle="项目名称、被测地址和描述。">
          <div className="settings-fields">
            <Field label="项目名称">
              <input className="input" value={name} onChange={(event) => setName(event.target.value)} />
            </Field>
            <Field label="被测环境地址" hint="执行时只访问这个地址；沙箱网络白名单以它为准。">
              <input className="input mono" value={baseUrl} onChange={(event) => setBaseUrl(event.target.value)} placeholder="http://127.0.0.1:8888" />
            </Field>
            <Field label="描述">
              <input className="input" value={description} onChange={(event) => setDescription(event.target.value)} />
            </Field>
            <Field label="租户" hint="MCP client 的 tenant 只有匹配时才能访问；默认 default。">
              <input className="input mono" value={tenant} onChange={(event) => setTenant(event.target.value)} placeholder="default" />
            </Field>
          </div>
        </Card>

        <Card title="性能基线" subtitle="生成性能维度用例时作为 timing 阈值；留空则标记缺少 SLA，不硬编阈值。">
          <div className="settings-fields">
            <div className="form-row">
              <Field label="接口响应时间 P95 SLA (ms)" hint="当前基线 100ms，硬上限 200ms；项目配置优先于文档中的 SLA。">
                <input
                  className="input mono"
                  type="number"
                  min={1}
                  value={performanceSlaMs}
                  onChange={(event) => setPerformanceSlaMs(event.target.value)}
                  placeholder="100"
                />
              </Field>
              <Field label="性能采样次数" hint="同一请求重复采样次数；默认 20，用于计算 P95。">
                <input
                  className="input mono"
                  type="number"
                  min={1}
                  max={1000}
                  value={performanceSamples}
                  onChange={(event) => setPerformanceSamples(event.target.value)}
                  placeholder="20"
                />
              </Field>
            </div>
          </div>
        </Card>

        <Card
          title="主动推送"
          subtitle="事件只包含 Shakedown 自身数据（缺陷 / 缺口 / 摘要），不写回被测系统；推送失败自动重试，不影响执行主流程。"
        >
          <div className="settings-fields">
            <div className="form-row">
              <Field label="项目 Webhook URL" hint="支持 http / https；新缺陷、缺口状态变更和执行完成摘要会 POST 到这里。">
                <input
                  className="input mono"
                  value={webhookUrl}
                  onChange={(event) => setWebhookUrl(event.target.value)}
                  placeholder="https://example.com/hooks/shakedown"
                />
              </Field>
              <Field label="通知邮箱" hint="与 webhook 独立；只配邮箱也能收到 digest（SMTP 在系统设置里配置）。">
                <input
                  className="input mono"
                  value={notificationEmail}
                  onChange={(event) => setNotificationEmail(event.target.value)}
                  placeholder="qa@example.com"
                />
              </Field>
            </div>
            <div className="form-row">
              <Field label="定时 digest" hint="启用后由 worker 按间隔投递日报 / 周报；间隔 >= 7 天为周报。">
                <label className="row-actions">
                  <input type="checkbox" checked={digestEnabled} onChange={(event) => setDigestEnabled(event.target.checked)} />
                  <span>{digestEnabled ? '已启用' : '未启用'}</span>
                </label>
              </Field>
              <Field label="digest 间隔（秒）" hint="最小 3600 秒；例如 86400 = 每日，604800 = 每周。">
                <input
                  className="input mono"
                  type="number"
                  min={3600}
                  value={digestInterval}
                  onChange={(event) => setDigestInterval(event.target.value)}
                />
              </Field>
              <Field label="手动触发" hint="立即排队一条 digest，内容为 actionable 缺陷 + 契约缺口摘要。">
                <Button icon="send" loading={pushBusy} onClick={sendDigest}>
                  推送摘要 digest
                </Button>
              </Field>
            </div>
            <div className="stack">
              <div className="row-actions">
                <strong>最近投递</strong>
                <span className="muted small">
                  {deliveries?.notification_configured
                    ? `已配置${deliveries?.webhook_configured ? ' webhook' : ''}${deliveries?.email_configured ? ' 邮件' : ''}`
                    : '尚未配置通知目标'}
                </span>
                <div className="spacer" />
                <Button size="sm" loading={deliveriesLoading} onClick={() => loadDeliveries(project.id)}>
                  刷新
                </Button>
              </div>
              {!deliveriesLoading && !(deliveries?.items.length ?? 0) && (
                <span className="muted small">还没有投递记录。保存 webhook 后，新事件或手动 digest 会出现在这里。</span>
              )}
              {(deliveries?.items ?? []).map((delivery) => (
                <div className="row-actions" key={delivery.id}>
                  <Pill tone={delivery.status === 'delivered' || delivery.status === 'sent' ? 'success' : delivery.status === 'failed' ? 'danger' : 'warning'}>
                    {delivery.status}
                  </Pill>
                  <code className="small">{delivery.event}</code>
                  <span className="muted small">尝试 {delivery.attempts} 次</span>
                  {delivery.created_at && <span className="muted small">{formatFullTime(delivery.created_at)}</span>}
                  {delivery.last_error && <span className="muted small">错误：{delivery.last_error}</span>}
                  {delivery.status !== 'delivered' && (
                    <Button size="sm" loading={pushBusy} onClick={() => retryDelivery(delivery.id)}>
                      重试
                    </Button>
                  )}
                </div>
              ))}
            </div>
          </div>
        </Card>

        <Card title="测试账号" subtitle="加密存储，只在执行时注入沙箱；密码显示为 ******** 表示未改动。">
          <div className="account-list">
            {Object.entries(accounts).map(([accountName, account]) => (
              <div key={accountName} className="account-row">
                <div className="account-row__name">
                  <Icon name="user" size={15} />
                  <input
                    className="input input--sm"
                    value={accountName}
                    readOnly
                    aria-label="账号名"
                  />
                </div>
                <input
                  className="input input--sm"
                  placeholder="用户名"
                  value={account.username ?? ''}
                  onChange={(event) => setAccountField(accountName, 'username', event.target.value)}
                />
                <input
                  className="input input--sm"
                  placeholder="密码"
                  type="password"
                  value={account.password ?? ''}
                  onChange={(event) => setAccountField(accountName, 'password', event.target.value)}
                />
                <input
                  className="input input--sm"
                  placeholder="Token（可选）"
                  value={account.token ?? ''}
                  onChange={(event) => setAccountField(accountName, 'token', event.target.value)}
                />
                <input
                  className="input input--sm input--xs"
                  placeholder="角色"
                  value={account.role ?? ''}
                  onChange={(event) => setAccountField(accountName, 'role', event.target.value)}
                />
                <button
                  className="icon-btn"
                  aria-label="删除账号"
                  onClick={() => {
                    setAccounts((prev) => {
                      const next = { ...prev }
                      delete next[accountName]
                      return next
                    })
                  }}
                >
                  <Icon name="trash" size={15} />
                </button>
              </div>
            ))}
          </div>
          <div className="row-actions">
            <Button
              size="sm"
              icon="plus"
              onClick={() => setAccounts((prev) => ({ ...prev, [nextAccountName(prev)]: { username: '', password: '' } }))}
            >
              添加账号
            </Button>
            <Button size="sm" icon="shield" loading={busy} onClick={validateAccounts}>预检账号</Button>
          </div>
          {accountCheck && (
            <div className="muted small" style={{ marginTop: 8 }}>
              共 {accountCheck.total} 个 · 通过 {accountCheck.ok} · 失败 {accountCheck.failed} · 跳过 {accountCheck.skipped}
              {Array.isArray(accountCheck.items) && accountCheck.items.filter((item: any) => item.ok === false).map((item: any) => (
                <div key={item.name} className="text-danger">· {item.name}: {item.detail}</div>
              ))}
            </div>
          )}
        </Card>

        <Card title="被测数据库" subtitle="用于「数据库状态断言」和按项目隔离的数据准备。">
          <div className="settings-fields">
            <Field label="DSN" hint={project.target_dsn_configured ? '已配置；留空表示不修改。' : '留空表示不启用数据库断言。'}>
              <input
                className="input mono"
                type="password"
                value={dsn}
                onChange={(event) => setDsn(event.target.value)}
                placeholder={project.target_dsn_configured ? '已保存（留空不修改）' : 'postgresql://user@host:5432/db'}
              />
            </Field>
            <Field label="只读 DSN" hint="数据库断言优先使用；未配置时回退到上面的 DSN。建议用只读账号。">
              <input
                className="input mono"
                type="password"
                value={readDsn}
                onChange={(event) => setReadDsn(event.target.value)}
                placeholder={project.target_read_dsn_configured ? '已保存（留空不修改）' : 'postgresql://readonly@host:5432/db'}
              />
            </Field>
            <Field label="隔离模式">
              <select className="input" value={mode} onChange={(event) => setMode(event.target.value)}>
                {Object.entries(MODE_LABEL).map(([value, label]) => (
                  <option key={value} value={value}>{label}</option>
                ))}
              </select>
            </Field>
            <div className="form-row">
              <Field label="berth 服务名">
                <input className="input mono" value={dbService} onChange={(event) => setDbService(event.target.value)} placeholder="my-service-db" />
              </Field>
              <Field label="库名">
                <input className="input mono" value={dbName} onChange={(event) => setDbName(event.target.value)} placeholder="app" />
              </Field>
            </div>
            <div className="form-row">
              <Field label="数据库模板">
                <input className="input mono" value={dbTemplate} onChange={(event) => setDbTemplate(event.target.value)} placeholder="postgres" />
              </Field>
              <Field label="每次 run 独立服务" hint="reset 模式下用 {服务名}-{run_id} 创建独立库；外部 SUT 需要自行指向该 DSN。">
                <label className="row-actions">
                  <input type="checkbox" checked={perRun} onChange={(event) => setPerRun(event.target.checked)} />
                  启用 per-run 数据库
                </label>
              </Field>
            </div>
            <Field label="初始化脚本">
              <input className="input mono" value={initScript} onChange={(event) => setInitScript(event.target.value)} placeholder="/path/to/init.sql" />
            </Field>
            <Field label="网络白名单" hint="每行一个主机名；实际拦截依赖 berth 网络策略。">
              <textarea className="input" rows={3} value={allowedHosts} onChange={(event) => setAllowedHosts(event.target.value)} />
            </Field>
          </div>
        </Card>

        <Card title="被测系统与安全" subtitle="启动方式、健康检查与高风险操作白名单。">
          <div className="settings-fields">
            <Field label="启动方式">
              <select className="input" value={targetType} onChange={(event) => setTargetType(event.target.value)}>
                <option value="external">external · 外部已启动</option>
                <option value="docker">docker</option>
                <option value="command">command</option>
              </select>
            </Field>
            <Field label="健康检查地址">
              <input className="input mono" value={healthUrl} onChange={(event) => setHealthUrl(event.target.value)} placeholder="/api/health" />
            </Field>
            <Field label="允许执行准备命令" hint="还需要在无人值守设置里开启 allow_target_commands；命令默认进沙箱，宿主执行需额外显式开启。">
              <label className="row-actions">
                <input type="checkbox" checked={allowExecute} onChange={(event) => setAllowExecute(event.target.checked)} />
                allow_execute
              </label>
            </Field>
            <div className="form-row">
              <Field label="启动命令"><input className="input mono" value={startCommand} onChange={(event) => setStartCommand(event.target.value)} placeholder="./scripts/dev-up.sh" /></Field>
              <Field label="停止命令"><input className="input mono" value={stopCommand} onChange={(event) => setStopCommand(event.target.value)} placeholder="./scripts/dev-down.sh" /></Field>
            </div>
            <div className="form-row">
              <Field label="迁移命令"><input className="input mono" value={migrateCommand} onChange={(event) => setMigrateCommand(event.target.value)} placeholder="alembic upgrade head" /></Field>
              <Field label="Seed 命令"><input className="input mono" value={seedCommand} onChange={(event) => setSeedCommand(event.target.value)} placeholder="python scripts/seed.py" /></Field>
            </div>
            <div className="form-row">
              <Field label="账号准备命令"><input className="input mono" value={accountCommand} onChange={(event) => setAccountCommand(event.target.value)} placeholder="python scripts/create_test_users.py" /></Field>
              <Field label="清理命令"><input className="input mono" value={cleanupCommand} onChange={(event) => setCleanupCommand(event.target.value)} placeholder="python scripts/cleanup_data.py" /></Field>
            </div>
            <Field label="就绪等待（秒）">
              <input className="input mono" value={waitSeconds} onChange={(event) => setWaitSeconds(event.target.value)} />
            </Field>
            <Field label="高风险操作白名单" hint="每行一条，如 DELETE /api/test-data/*；白名单内启用时不再二次确认。">
              <textarea className="input code-input" rows={4} value={riskWhitelist} onChange={(event) => setRiskWhitelist(event.target.value)} />
            </Field>
          </div>
        </Card>

        <Card title="工单集成" subtitle="这个项目对应的 GitHub 仓库和 Jira 项目；Token / 服务地址在系统设置里统一配置。">
          <div className="settings-fields">
            <Field label="GitHub 仓库" hint="格式 owner/repo；留空则使用系统设置里的全局默认值。">
              <input className="input mono" value={githubRepo} onChange={(event) => setGithubRepo(event.target.value)} placeholder="owner/repo" />
            </Field>
            <Field label="GitHub 标签" hint="逗号分隔；留空表示不附加标签。">
              <input className="input" value={githubLabels} onChange={(event) => setGithubLabels(event.target.value)} placeholder="shakedown, bug" />
            </Field>
            <div className="form-row">
              <Field label="Jira 项目 Key" hint="例如 SEC；留空则使用全局默认值。">
                <input className="input mono" value={jiraProjectKey} onChange={(event) => setJiraProjectKey(event.target.value)} placeholder="SEC" />
              </Field>
              <Field label="Jira 工单类型">
                <input className="input" value={jiraIssueType} onChange={(event) => setJiraIssueType(event.target.value)} placeholder="Bug" />
              </Field>
            </div>
          </div>
        </Card>
      </div>

      <Card title="无人值守控制台" subtitle="调度、成员角色、例外 / SLA、断言质量、性能历史、视觉基线和 a11y 审计。">
        <div className="row-actions">
          <Link className="btn btn--secondary btn--sm" to={`/projects/${project.id}/automation`}>
            <Icon name="zap" size={14} /> 打开自动化控制台
          </Link>
          <span className="muted small">digest / SLA / flaky / 命令执行等自动化开关也在那里。</span>
        </div>
      </Card>

      <Card title="危险操作" className="danger-card">
        <div className="danger-row">
          <div>
            <strong>删除项目</strong>
            <p>知识源、用例、执行记录、缺陷会全部删除，且不可恢复。</p>
          </div>
          <Button variant="danger" icon="trash" onClick={() => setDeleteOpen(true)}>删除项目</Button>
        </div>
      </Card>

      <ConfirmDialog
        open={deleteOpen}
        title={`删除项目「${project.name}」？`}
        description="这个操作不可恢复。项目下的所有知识源、用例、执行和缺陷都会被删除。"
        confirmLabel="确认删除"
        danger
        busy={busy}
        onCancel={() => setDeleteOpen(false)}
        onConfirm={remove}
      />
    </div>
  )
}

function nextAccountName(accounts: Record<string, Account>): string {
  let index = 1
  while (accounts[`account${index}`]) index += 1
  return `account${index}`
}
