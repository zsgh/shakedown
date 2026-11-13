import { useState } from 'react'
import { useParams } from 'react-router-dom'
import { api } from '../../lib/api'
import { fromNow, shortHash, shortPath } from '../../lib/format'
import { useAsync } from '../../lib/hooks'
import { SOURCE_KIND, SOURCE_ROLE, meta } from '../../lib/labels'
import type { KnowledgeSource } from '../../lib/types'
import { Icon } from '../../ui/icons'
import {
  Button,
  Card,
  EmptyState,
  ErrorBox,
  Field,
  IconButton,
  Pill,
  Skeleton,
  Status,
  cn,
} from '../../ui/kit'
import { ConfirmDialog, Modal, useToast } from '../../ui/overlays'

const KIND_ICON = { code: 'code', markdown: 'file', openapi: 'api' } as const

const STATUS_MAP: Record<string, { label: string; tone: string }> = {
  ready: { label: '已就绪', tone: 'success' },
  parsing: { label: '解析中', tone: 'info' },
  pending: { label: '等待解析', tone: 'warning' },
  failed: { label: '解析失败', tone: 'danger' },
}

function sourceStats(source: KnowledgeSource): string[] {
  const meta2 = (source.meta ?? {}) as Record<string, any>
  const stats: string[] = []
  if (meta2.chunks) stats.push(`${meta2.chunks} 块`)
  const endpoints = meta2.endpoints ?? meta2.routes ?? meta2.doc_endpoints
  if (Array.isArray(endpoints)) stats.push(`${endpoints.length} 接口`)
  else if (typeof endpoints === 'number' && endpoints > 0) stats.push(`${endpoints} 接口`)
  if (meta2.files) stats.push(`${meta2.files} 文件`)
  return stats
}

export default function Sources() {
  const { projectId } = useParams()
  const toast = useToast()
  const [autoOpen, setAutoOpen] = useState(false)
  const [addOpen, setAddOpen] = useState(false)
  const [deleting, setDeleting] = useState<KnowledgeSource | null>(null)
  const [busy, setBusy] = useState(false)

  const sources = useAsync<KnowledgeSource[]>(() => api.get(`/projects/${projectId}/sources`), [projectId], {
    refreshMs: 5000,
  })
  const list = sources.data ?? []
  const ready = list.filter((item) => item.status === 'ready').length
  const failed = list.filter((item) => item.status === 'failed').length
  const parsing = list.filter((item) => item.status === 'pending' || item.status === 'parsing').length

  async function reparse(source: KnowledgeSource) {
    setBusy(true)
    try {
      await api.post(`/sources/${source.id}/reparse`)
      toast.success('已排队重新解析', source.name)
      sources.reload()
    } catch (error) {
      toast.error('重新解析失败', String(error))
    } finally {
      setBusy(false)
    }
  }

  async function setRole(source: KnowledgeSource, role: string) {
    try {
      await api.patch(`/sources/${source.id}`, { role })
      sources.reload()
    } catch (error) {
      toast.error('修改角色失败', String(error))
    }
  }

  async function remove() {
    if (!deleting) return
    setBusy(true)
    try {
      await api.del(`/sources/${deleting.id}`)
      toast.success('知识源已删除')
      setDeleting(null)
      sources.reload()
    } catch (error) {
      toast.error('删除失败', String(error))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="stack">
      <div className="section-bar">
        <div className="section-bar__stats">
          <span><strong>{list.length}</strong> 个知识源</span>
          <span className="text-success"><strong>{ready}</strong> 就绪</span>
          {parsing > 0 && <span className="text-info"><strong>{parsing}</strong> 解析中</span>}
          {failed > 0 && <span className="text-danger"><strong>{failed}</strong> 失败</span>}
        </div>
        <div className="section-bar__actions">
          <Button size="sm" icon="plus" onClick={() => setAddOpen(true)}>添加知识源</Button>
          <Button size="sm" variant="primary" icon="download" onClick={() => setAutoOpen(true)}>从仓库自动导入</Button>
        </div>
      </div>

      {sources.error && <ErrorBox>{sources.error}</ErrorBox>}

      {!sources.loading && list.length === 0 && (
        <Card>
          <EmptyState
            icon="book"
            title="还没有知识源"
            description="导入代码库、Markdown 文档或 OpenAPI。每条用例的预期都要能追溯到这里的内容。"
            action={<Button variant="primary" icon="download" onClick={() => setAutoOpen(true)}>从仓库自动导入</Button>}
          />
        </Card>
      )}

      {sources.loading && !sources.data && <Card><Skeleton lines={5} /></Card>}

      <div className="source-list">
        {list.map((source) => {
          const kind = meta(SOURCE_KIND, source.kind)
          return (
            <article key={source.id} className={cn('source-row', source.status === 'failed' && 'is-failed')}>
              <span className={cn('source-row__icon', `tone-${kind.tone}`)}>
                <Icon name={KIND_ICON[source.kind] ?? 'file'} size={18} />
              </span>
              <div className="source-row__main">
                <div className="source-row__title">
                  <strong>{source.name}</strong>
                  <Pill tone={kind.tone}>{kind.label}</Pill>
                  <Status value={source.status} map={STATUS_MAP} />
                  {source.kind === 'markdown' && (
                    <select
                      className="input input--xs"
                      value={source.role ?? 'notes'}
                      onChange={(event) => setRole(source, event.target.value)}
                      title="spec=接口规范（可作为断言依据）；notes=设计笔记"
                    >
                      {Object.entries(SOURCE_ROLE).map(([value, label]) => (
                        <option key={value} value={value}>{label}</option>
                      ))}
                    </select>
                  )}
                </div>
                <div className="source-row__location mono" title={source.location}>
                  {shortPath(source.location, 96)}
                </div>
                <div className="source-row__meta">
                  {sourceStats(source).map((item) => <span key={item}>{item}</span>)}
                  {source.version && <span className="mono">版本 {shortHash(source.version)}</span>}
                  <span>{source.parsed_at ? `解析于 ${fromNow(source.parsed_at)}` : '已解析'}</span>
                  {source.error && <span className="text-danger">{source.error}</span>}
                </div>
              </div>
              <div className="source-row__actions">
                <IconButton icon="refresh" label="重新解析" disabled={busy} onClick={() => reparse(source)} />
                <IconButton icon="trash" label="删除" onClick={() => setDeleting(source)} />
              </div>
            </article>
          )
        })}
      </div>

      <AutoImportModal
        open={autoOpen}
        projectId={projectId!}
        onClose={() => setAutoOpen(false)}
        onDone={() => {
          sources.reload()
          setAutoOpen(false)
        }}
      />
      <AddSourceModal
        open={addOpen}
        projectId={projectId!}
        onClose={() => setAddOpen(false)}
        onDone={() => {
          sources.reload()
          setAddOpen(false)
        }}
      />
      <ConfirmDialog
        open={!!deleting}
        title={`删除知识源「${deleting?.name ?? ''}」？`}
        description="相关的代码块和由它抽取的接口会一并清理，已生成的用例不受影响。"
        confirmLabel="删除"
        danger
        busy={busy}
        onCancel={() => setDeleting(null)}
        onConfirm={remove}
      />
    </div>
  )
}

function AutoImportModal({
  open,
  projectId,
  onClose,
  onDone,
}: {
  open: boolean
  projectId: string
  onClose: () => void
  onDone: () => void
}) {
  const toast = useToast()
  const [path, setPath] = useState('')
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState<any>(null)

  async function run() {
    setBusy(true)
    setResult(null)
    try {
      const payload = await api.post<any>(`/projects/${projectId}/sources/auto-import`, { path: path.trim() })
      setResult(payload)
      const failed = payload.summary?.failed ?? 0
      if (failed) toast.error('导入完成，但有失败项', `${failed} 个知识源解析失败`)
      else toast.success('导入完成', `新建 / 更新 ${payload.summary?.sources ?? 0} 个知识源`)
    } catch (error) {
      toast.error('自动导入失败', String(error))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal
      open={open}
      onClose={onClose}
      title="从仓库自动导入"
      subtitle="给一个本地已 clone 的目录，自动发现代码、Markdown、OpenAPI 和文档里的接口清单。"
      size="lg"
      footer={
        <>
          <div className="spacer" />
          <Button onClick={onClose}>关闭</Button>
          {result ? (
            <Button variant="primary" onClick={onDone}>完成</Button>
          ) : (
            <Button variant="primary" icon="download" loading={busy} disabled={!path.trim()} onClick={run}>
              开始导入
            </Button>
          )}
        </>
      }
    >
      <div className="stack">
        <Field label="仓库目录" hint="本地路径；不支持远程 URL 自动克隆。">
          <input
            className="input mono"
            autoFocus
            value={path}
            onChange={(event) => setPath(event.target.value)}
            placeholder="/Users/you/github/my-service"
          />
        </Field>
        {busy && (
          <div className="notice notice--info">
            <span className="spinner" />
            <span>正在扫描并解析，仓库大的话需要一会儿…</span>
          </div>
        )}
        {result && (
          <>
            <div className="import-summary">
              <div><span>代码文件</span><strong>{result.discovery?.code?.count ?? 0}</strong></div>
              <div><span>Markdown</span><strong>{result.discovery?.markdown?.count ?? 0}</strong></div>
              <div><span>OpenAPI</span><strong>{result.discovery?.openapi?.count ?? 0}</strong></div>
              <div><span>知识源</span><strong>{result.summary?.sources ?? 0}</strong></div>
            </div>
            <div className="import-list">
              {(result.sources ?? []).map((item: any) => (
                <div key={item.id} className="import-row">
                  <Status value={item.status === 'ready' ? 'ready' : item.status} map={STATUS_MAP} />
                  <strong>{item.name}</strong>
                  <span className="muted">{item.kind}</span>
                  <span className={item.error ? 'text-danger' : 'muted'}>
                    {item.error || `${item.meta?.chunks ?? 0} 块 · ${item.meta?.endpoints ?? item.meta?.routes ?? item.meta?.doc_endpoints ?? 0} 接口`}
                  </span>
                </div>
              ))}
            </div>
          </>
        )}
      </div>
    </Modal>
  )
}

function AddSourceModal({
  open,
  projectId,
  onClose,
  onDone,
}: {
  open: boolean
  projectId: string
  onClose: () => void
  onDone: () => void
}) {
  const toast = useToast()
  const [kind, setKind] = useState('markdown')
  const [location, setLocation] = useState('')
  const [name, setName] = useState('')
  const [busy, setBusy] = useState(false)

  async function add() {
    setBusy(true)
    try {
      await api.post(`/projects/${projectId}/sources?sync=true`, {
        kind,
        location: location.trim(),
        name: name.trim(),
      })
      toast.success('知识源已导入')
      setLocation('')
      setName('')
      onDone()
    } catch (error) {
      toast.error('导入失败', String(error))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal
      open={open}
      onClose={onClose}
      title="添加知识源"
      subtitle="代码目录、Markdown 文件/目录、OpenAPI JSON/YAML 文件或 URL。"
      footer={
        <>
          <div className="spacer" />
          <Button onClick={onClose}>取消</Button>
          <Button variant="primary" loading={busy} disabled={!location.trim()} onClick={add}>导入</Button>
        </>
      }
    >
      <div className="stack">
        <Field label="类型">
          <select className="input" value={kind} onChange={(event) => setKind(event.target.value)}>
            {Object.entries(SOURCE_KIND).map(([value, item]) => (
              <option key={value} value={value}>{item.label}</option>
            ))}
          </select>
        </Field>
        <Field label="位置" hint="本地路径或 URL；Swagger UI 页面会自动探测背后的 spec。">
          <input
            className="input mono"
            autoFocus
            value={location}
            onChange={(event) => setLocation(event.target.value)}
            placeholder="/path/to/docs 或 https://example.com/openapi.json"
          />
        </Field>
        <Field label="名称（可选）">
          <input className="input" value={name} onChange={(event) => setName(event.target.value)} placeholder="接口文档" />
        </Field>
      </div>
    </Modal>
  )
}
