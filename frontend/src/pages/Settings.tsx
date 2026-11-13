import { useEffect, useMemo, useState } from 'react'
import { api } from '../lib/api'
import { useAsync } from '../lib/hooks'
import { Icon } from '../ui/icons'
import { Button, Card, CopyButton, Field, PageHead, Pill, Skeleton, cn } from '../ui/kit'
import { ConfirmDialog, useToast } from '../ui/overlays'

interface SettingsPayload {
  effective: Record<string, any>
  overrides: Record<string, any>
  schema: Array<{ key: string; label: string; description: string }>
}

interface FieldMeta {
  label: string
  type?: string
  options?: string[]
  hint?: string
}

interface McpClient {
  id: number
  name: string
  token_prefix: string
  scopes: string[]
  tenant?: string
  actor?: string
  enabled: boolean
  last_used_at: string | null
  created_at: string
}

interface McpClientCreated extends McpClient {
  token: string
}

const FIELDS: Record<string, FieldMeta> = {
  llm_provider: { label: '模型提供方', options: ['mock', 'openai', 'skipper'], hint: 'mock = 离线规则版，不需要外部服务' },
  llm_base_url: { label: '接口地址', hint: '填到 /v1 即可；也兼容完整 /chat/completions 地址。OpenCode Go 自动带 session 头' },
  llm_api_key: { label: 'API Key', type: 'password' },
  llm_model: { label: '模型名' },
  llm_temperature: { label: '采样温度', type: 'number' },
  llm_max_tokens: { label: '最大 token', type: 'number' },
  llm_reasoning_effort: { label: '推理强度', options: ['auto', 'low', 'medium', 'high'] },
  embedding_provider: { label: '向量提供方', options: ['hash', 'openai'] },
  embedding_base_url: { label: '接口地址' },
  embedding_api_key: { label: 'API Key', type: 'password' },
  embedding_model: { label: '模型名' },
  embedding_dim: { label: '向量维度', type: 'number' },
  sandbox_mode: { label: '沙箱模式', options: ['local', 'berth'], hint: 'berth = 隔离执行，local = 开发降级' },
  berth_base_url: { label: 'berth 地址' },
  berth_token: { label: 'berth Token', type: 'password' },
  berth_image: { label: '基础镜像' },
  berth_runner_image: { label: '执行器镜像' },
  sandbox_timeout: { label: '执行超时（秒）', type: 'number' },
  sandbox_memory_mb: { label: '内存上限（MB）', type: 'number' },
  sandbox_cpu_percent: { label: 'CPU 上限（%）', type: 'number' },
  worker_enabled: { label: '启用后台 worker', options: ['true', 'false'] },
  worker_concurrency: { label: '并发数', type: 'number' },
  worker_poll_interval: { label: '轮询间隔（秒）', type: 'number' },
  cors_origins: { label: '允许的前端来源', hint: '逗号分隔' },
  encryption_key: { label: '加密密钥', type: 'password', hint: '改动后需要重新录入测试账号' },
  github_token: { label: 'GitHub Token', type: 'password', hint: '需要 repo / issues 权限；导出后可用于创建 Issue' },
  github_repo: { label: 'GitHub 仓库', hint: '格式 owner/repo，例如 zsgh/mymac' },
  github_api_url: { label: 'GitHub API 地址', hint: '默认 https://api.github.com；企业版可改' },
  github_labels: { label: 'GitHub 标签', hint: '逗号分隔；留空则不附加' },
  jira_base_url: { label: 'Jira 地址', hint: '例如 https://your-team.atlassian.net' },
  jira_email: { label: 'Jira 邮箱' },
  jira_api_token: { label: 'Jira API Token', type: 'password' },
  jira_project_key: { label: 'Jira 项目 Key', hint: '例如 SEC' },
  jira_issue_type: { label: 'Jira 工单类型', hint: '默认 Bug；需要与项目工作流匹配' },
  mcp_require_auth: { label: 'MCP 要求 Token', options: ['true', 'false'], hint: '建议保持 true；false 仅用于本机调试' },
  mcp_allowed_hosts: { label: 'MCP 允许的 Host', hint: '逗号分隔，例如 127.0.0.1,localhost,mcp.example.com' },
}

const GROUPS: Array<{ id: string; title: string; description: string; icon: 'sparkles' | 'layers' | 'shield' | 'activity' | 'link' | 'terminal'; keys: string[] }> = [
  {
    id: 'llm',
    title: '模型',
    description: 'Agent 规划、生成、归因使用的模型。mock 模式不调用外部服务。',
    icon: 'sparkles',
    keys: ['llm_provider', 'llm_base_url', 'llm_api_key', 'llm_model', 'llm_temperature', 'llm_max_tokens', 'llm_reasoning_effort'],
  },
  {
    id: 'embedding',
    title: 'Embedding',
    description: '知识检索用的向量模型。hash 模式离线可用。',
    icon: 'layers',
    keys: ['embedding_provider', 'embedding_base_url', 'embedding_api_key', 'embedding_model', 'embedding_dim'],
  },
  {
    id: 'sandbox',
    title: '沙箱',
    description: '用例在隔离环境中执行；berth 不可用时退化为本地临时目录。',
    icon: 'shield',
    keys: ['sandbox_mode', 'berth_base_url', 'berth_token', 'berth_image', 'berth_runner_image', 'sandbox_timeout', 'sandbox_memory_mb', 'sandbox_cpu_percent'],
  },
  {
    id: 'worker',
    title: 'Worker',
    description: '数据库驱动的任务队列；执行、生成、归因都在后台 worker 里跑。',
    icon: 'activity',
    keys: ['worker_enabled', 'worker_concurrency', 'worker_poll_interval'],
  },
  {
    id: 'integrations',
    title: '缺陷工单凭据',
    description: '这里只放账号级凭据和服务地址；GitHub 仓库、Jira 项目和工单类型在每个项目的「项目设置 → 工单集成」里配置。',
    icon: 'link',
    keys: ['github_token', 'github_api_url', 'jira_base_url', 'jira_email', 'jira_api_token'],
  },
  {
    id: 'mcp',
    title: 'MCP HTTP',
    description: '把缺陷数据通过 Streamable HTTP MCP 暴露给其他 Agent；客户端 Token 在下方管理。',
    icon: 'terminal',
    keys: ['mcp_require_auth', 'mcp_allowed_hosts'],
  },
  {
    id: 'security',
    title: '安全',
    description: '跨域与敏感配置加密。',
    icon: 'shield',
    keys: ['cors_origins', 'encryption_key'],
  },
]

export default function Settings() {
  const toast = useToast()
  const { data, error, reload } = useAsync<SettingsPayload>(() => api.get('/settings'), [])
  const mcpClients = useAsync<McpClient[]>(() => api.get('/mcp/clients'), [])
  const [values, setValues] = useState<Record<string, any>>({})
  const [busy, setBusy] = useState(false)
  const [probe, setProbe] = useState<Record<string, any> | null>(null)
  const [probing, setProbing] = useState(false)
  const [resetOpen, setResetOpen] = useState(false)
  const [mcpName, setMcpName] = useState('')
  const [mcpScopes, setMcpScopes] = useState<string[]>(['read'])
  const [mcpTenant, setMcpTenant] = useState('')
  const [mcpActor, setMcpActor] = useState('')
  const [mcpBusy, setMcpBusy] = useState(false)
  const [createdToken, setCreatedToken] = useState<string | null>(null)
  const [mcpDeleteClient, setMcpDeleteClient] = useState<McpClient | null>(null)

  useEffect(() => {
    if (data) setValues({ ...data.effective })
  }, [data])

  const overrides = useMemo(() => new Set(Object.keys(data?.overrides ?? {})), [data])

  async function save() {
    setBusy(true)
    try {
      const managed = new Set(data?.schema.map((item) => item.key) ?? [])
      const normalize = (value: any) => (Array.isArray(value) ? value.join(',') : String(value ?? ''))
      const payload: Record<string, any> = {}
      Object.entries(values).forEach(([key, value]) => {
        if (!managed.has(key)) return
        if (normalize(value) !== normalize(data?.effective?.[key])) payload[key] = value
      })
      if (Object.keys(payload).length === 0) {
        toast.info('没有改动')
        return
      }
      await api.put('/settings', { values: payload })
      toast.success('设置已保存', '即时生效，不用重启')
      reload()
    } catch (error) {
      toast.error('保存失败', String(error))
    } finally {
      setBusy(false)
    }
  }

  async function runProbe() {
    setProbing(true)
    try {
      const result = await api.get<Record<string, any>>('/settings/probe')
      setProbe(result)
      toast.success('连通性检查完成')
    } catch (error) {
      toast.error('检查失败', String(error))
    } finally {
      setProbing(false)
    }
  }

  async function reset() {
    setBusy(true)
    try {
      await api.post('/settings/reset')
      toast.success('已恢复环境变量 / 默认值')
      reload()
    } catch (error) {
      toast.error('恢复失败', String(error))
    } finally {
      setBusy(false)
    }
  }

  async function createMcpClient() {
    if (!mcpName.trim()) {
      toast.info('先填写客户端名称')
      return
    }
    setMcpBusy(true)
    try {
      const created = await api.post<McpClientCreated>('/mcp/clients', {
        name: mcpName.trim(),
        scopes: mcpScopes,
        tenant: mcpTenant.trim() || undefined,
        actor: mcpActor.trim() || undefined,
      })
      setCreatedToken(created.token)
      setMcpName('')
      setMcpTenant('')
      setMcpActor('')
      mcpClients.reload()
      toast.success('MCP Token 已创建', '明文只显示这一次，请立即复制保存')
    } catch (error) {
      toast.error('创建失败', String(error))
    } finally {
      setMcpBusy(false)
    }
  }

  async function deleteMcpClient() {
    if (!mcpDeleteClient) return
    setMcpBusy(true)
    try {
      await api.del(`/mcp/clients/${mcpDeleteClient.id}`)
      mcpClients.reload()
      toast.success('MCP 客户端已删除', mcpDeleteClient.name)
      setMcpDeleteClient(null)
    } catch (error) {
      toast.error('删除失败', String(error))
    } finally {
      setMcpBusy(false)
    }
  }

  function toggleMcpScope(scope: string) {
    setMcpScopes((prev) => (prev.includes(scope) ? prev.filter((item) => item !== scope) : [...prev, scope]))
  }

  if (!data) {
    return (
      <div className="page">
        <PageHead title="系统设置" />
        {error ? <div className="notice notice--danger">{error}</div> : <Skeleton lines={10} />}
      </div>
    )
  }

  return (
    <div className="page">
      <PageHead
        title="系统设置"
        subtitle="改完即时生效，不用重启。留空的项使用环境变量或默认值。"
        actions={
          <>
            <Button icon="activity" loading={probing} onClick={runProbe}>测试连通性</Button>
            <Button icon="refresh" onClick={() => setResetOpen(true)} disabled={busy}>恢复默认</Button>
            <Button variant="primary" icon="check" loading={busy} onClick={save}>保存设置</Button>
          </>
        }
      />

      {probe && (
        <Card title="连通性" subtitle="刚刚的探测结果">
          <div className="probe-grid">
            {Object.entries(probe).map(([key, value]) => {
              const ok = typeof value === 'object' && value !== null ? (value as any).ok ?? (value as any).available : null
              return (
                <div key={key} className="probe-item">
                  <div className="probe-item__head">
                    <strong>{key === 'llm' ? '模型服务' : key === 'sandbox' ? '沙箱' : key}</strong>
                    <Pill tone={ok === true ? 'success' : ok === false ? 'danger' : 'muted'} dot>
                      {ok === true ? '可用' : ok === false ? '不可用' : '未知'}
                    </Pill>
                  </div>
                  <pre className="code code--compact">{JSON.stringify(value, null, 2)}</pre>
                </div>
              )
            })}
          </div>
        </Card>
      )}

      <div className="settings-grid">
        {GROUPS.map((group) => {
          const keys = group.keys.filter((key) => data.schema.some((item) => item.key === key))
          if (!keys.length) return null
          return (
            <Card key={group.id} title={group.title} subtitle={group.description}>
              <div className="settings-fields">
                {keys.map((key) => {
                  const meta = FIELDS[key] ?? { label: key }
                  const value = values[key]
                  return (
                    <Field
                      key={key}
                      label={meta.label}
                      hint={
                        <>
                          {meta.hint ?? data.schema.find((item) => item.key === key)?.description}
                          {overrides.has(key) && <Pill tone="blue" className="ml-1">已覆盖</Pill>}
                        </>
                      }
                    >
                      {meta.options ? (
                        <select
                          className="input"
                          value={String(value ?? '')}
                          onChange={(event) => setValues((prev) => ({ ...prev, [key]: event.target.value }))}
                        >
                          {meta.options.map((option) => (
                            <option key={option} value={option}>{option}</option>
                          ))}
                        </select>
                      ) : (
                        <input
                          className={cn('input', meta.type === 'password' && 'mono')}
                          type={meta.type ?? 'text'}
                          value={Array.isArray(value) ? value.join(',') : (value ?? '')}
                          onChange={(event) => setValues((prev) => ({ ...prev, [key]: event.target.value }))}
                          placeholder={meta.type === 'password' ? '已保存则显示为 ********' : ''}
                        />
                      )}
                    </Field>
                  )
                })}
              </div>
            </Card>
          )
        })}
      </div>

      <Card
        title="MCP HTTP 客户端"
        subtitle="其他 Agent 通过 Streamable HTTP 连接 /mcp/；每个客户端单独 Token，read 可读取缺陷 / 用例 / 运行结果，write 才能触发运行、提交修复和创建工单。"
      >
        <div className="settings-fields">
          <div className="notice notice--info">
            <Icon name="terminal" size={16} />
            <span>
              MCP endpoint：<code>{`${window.location.protocol}//${window.location.hostname}:8000/mcp/`}</code>
              <br />
              客户端请求头：<code>Authorization: Bearer mcp_...</code>
            </span>
          </div>

          {createdToken && (
            <div className="notice notice--success">
              <Icon name="check" size={16} />
              <div>
                <strong>Token 只显示这一次</strong>
                <div className="crumb-inline" style={{ marginTop: 8 }}>
                  <code className="mono">{createdToken}</code>
                  <CopyButton text={createdToken} label="复制 Token" />
                </div>
              </div>
            </div>
          )}

          <div className="form-row">
            <Field label="客户端名称" hint="例如 opencode / claude-code / ci-fixer">
              <input
                className="input"
                value={mcpName}
                onChange={(event) => setMcpName(event.target.value)}
                placeholder="opencode"
              />
            </Field>
            <Field label="权限">
              <div className="crumb-inline">
                <label className="checkbox-label">
                  <input
                    type="checkbox"
                    checked={mcpScopes.includes('read')}
                    onChange={() => toggleMcpScope('read')}
                  />
                  read
                </label>
                <label className="checkbox-label">
                  <input
                    type="checkbox"
                    checked={mcpScopes.includes('write')}
                    onChange={() => toggleMcpScope('write')}
                  />
                  write
                </label>
              </div>
            </Field>
          </div>

          <div className="form-row">
            <Field label="租户（可选）" hint="非空时只能访问同 tenant 的项目；留空不限制。">
              <input
                className="input mono"
                value={mcpTenant}
                onChange={(event) => setMcpTenant(event.target.value)}
                placeholder="team-a"
              />
            </Field>
            <Field label="绑定 Actor（可选）" hint="匹配项目成员 actor，用于 owner / maintainer / viewer 角色。">
              <input
                className="input mono"
                value={mcpActor}
                onChange={(event) => setMcpActor(event.target.value)}
                placeholder="qa-lead"
              />
            </Field>
          </div>

          <Button icon="plus" loading={mcpBusy} onClick={createMcpClient}>
            创建 MCP Token
          </Button>

          {mcpClients.error && <div className="notice notice--danger">{mcpClients.error}</div>}
          <div className="account-list">
            {(mcpClients.data ?? []).map((client) => (
              <div key={client.id} className="account-row">
                <div className="account-row__name">
                  <Icon name="link" size={15} />
                  <strong>{client.name}</strong>
                </div>
                <code className="mono">{client.token_prefix}…</code>
                <div className="crumb-inline">
                  {client.scopes.map((scope) => (
                    <Pill key={scope} tone={scope === 'write' ? 'warning' : 'blue'}>
                      {scope}
                    </Pill>
                  ))}
                  {client.tenant && <Pill tone="muted">tenant {client.tenant}</Pill>}
                  {client.actor && <Pill tone="muted">actor {client.actor}</Pill>}
                </div>
                <Pill tone={client.enabled ? 'success' : 'muted'} dot>
                  {client.enabled ? '启用' : '已停用'}
                </Pill>
                <Button
                  size="sm"
                  variant="danger"
                  disabled={mcpBusy}
                  onClick={() => setMcpDeleteClient(client)}
                >
                  删除
                </Button>
              </div>
            ))}
          </div>
        </div>
      </Card>

      <div className="settings-foot">
        <Icon name="info" size={15} />
        <span>密钥类字段在接口里打码返回；原样保存会保留已存的值。</span>
      </div>

      <ConfirmDialog
        open={resetOpen}
        title="恢复默认设置？"
        description="会清空数据库里的所有配置覆盖，回到环境变量 / 默认值。已保存的模型密钥也会被清除。"
        confirmLabel="恢复默认"
        danger
        busy={busy}
        onCancel={() => setResetOpen(false)}
        onConfirm={async () => {
          await reset()
          setResetOpen(false)
        }}
      />

      <ConfirmDialog
        open={!!mcpDeleteClient}
        title={`删除 MCP 客户端「${mcpDeleteClient?.name ?? ''}」？`}
        description="删除后该 Token 立即失效，任何 Agent 都不能再用它访问 MCP。此操作不可恢复。"
        confirmLabel="确认删除"
        danger
        busy={mcpBusy}
        onCancel={() => setMcpDeleteClient(null)}
        onConfirm={deleteMcpClient}
      />
    </div>
  )
}
