import { useMemo, useState } from 'react'
import { useParams } from 'react-router-dom'
import { api, apiUrl } from '../../lib/api'
import { fromNow } from '../../lib/format'
import { useAsync } from '../../lib/hooks'
import { ENDPOINT_STATE, meta } from '../../lib/labels'
import type { ConsistencyItem, ConsistencySummary } from '../../lib/types'
import { Icon } from '../../ui/icons'
import { Button, Card, EmptyState, ErrorBox, Pill, Skeleton, Status, cn } from '../../ui/kit'
import { useToast } from '../../ui/overlays'

const KIND_LABEL: Record<string, string> = {
  doc_only: '文档独有',
  code_only: '代码独有',
  spec_only: 'Spec 独有',
  param_mismatch: '参数不一致',
  method_only: '方法未声明',
  doc_missing: '文档缺口',
}

export default function Consistency() {
  const { projectId } = useParams()
  const toast = useToast()
  const [includeWeak, setIncludeWeak] = useState(true)
  const [scope, setScope] = useState<'strong' | 'weak'>('strong')
  const [busy, setBusy] = useState(false)

  const summary = useAsync<ConsistencySummary>(() => api.get(`/projects/${projectId}/consistency/summary`), [projectId])
  const items = useAsync<ConsistencyItem[]>(
    () => api.get(`/projects/${projectId}/consistency${includeWeak ? '?include_weak=true' : ''}`),
    [projectId, includeWeak],
  )

  const all = items.data ?? []
  const strong = useMemo(() => all.filter((item) => item.state !== 'partial'), [all])
  const weak = useMemo(() => all.filter((item) => item.state === 'partial'), [all])
  const visible = scope === 'strong' ? strong : weak

  async function recheck() {
    setBusy(true)
    try {
      await api.post(`/projects/${projectId}/consistency/check`)
      toast.success('一致性对照已重做')
      items.reload()
      summary.reload()
    } catch (error) {
      toast.error('对照失败', String(error))
    } finally {
      setBusy(false)
    }
  }

  const data = summary.data

  return (
    <div className="stack">
      <div className="section-bar">
        <div className="section-bar__stats">
          <span><strong>{data?.api_endpoints ?? 0}</strong> 个接口</span>
          <span className="text-danger"><strong>{data?.strong_signals ?? 0}</strong> 强信号</span>
          <span className="text-warning"><strong>{data?.weak_signals ?? 0}</strong> 弱信号</span>
          <span><strong>{data?.doc_coverage === null || data?.doc_coverage === undefined ? '—' : `${Math.round(data.doc_coverage * 1000) / 10}%`}</strong> 文档覆盖</span>
        </div>
        <div className="section-bar__actions">
          <label className="checkbox-label">
            <input type="checkbox" checked={includeWeak} onChange={(event) => setIncludeWeak(event.target.checked)} />
            包含弱信号
          </label>
          <a
            className="btn btn--secondary btn--sm"
            href={apiUrl(`/projects/${projectId}/consistency/export.md?include_weak=${includeWeak}`)}
          >
            <Icon name="download" size={14} /> 导出修复清单
          </a>
          <Button size="sm" icon="refresh" loading={busy} onClick={recheck}>重新对照</Button>
        </div>
      </div>

      <div className="notice notice--info">
        <Icon name="info" size={16} />
        <span>
          不一致项本身就是潜在缺陷线索。<strong>强信号</strong>是文档提到但没实现、与 spec 冲突、方法契约不一致；
          <strong>弱信号</strong>是文档缺口，属于改进项。只有 role=spec 的 API 文档计入契约，README / 架构笔记不会制造“文档缺口”。
          导出的 Markdown 带证据定位与修复指引，可直接交给编码 AI。
        </span>
      </div>

      {items.error && <ErrorBox>{items.error}</ErrorBox>}
      {items.loading && !items.data && <Card><Skeleton lines={6} /></Card>}

      {!items.loading && all.length === 0 && (
        <Card>
          <EmptyState
            icon="check"
            title="没有发现三方不一致"
            description="OpenAPI、代码路由和文档描述目前对得上。"
          />
        </Card>
      )}

      {all.length > 0 && (
        <>
          <div className="segmented">
            <button className={cn('segmented__item', scope === 'strong' && 'is-active')} onClick={() => setScope('strong')}>
              强信号 <span className="segmented__count">{strong.length}</span>
            </button>
            <button className={cn('segmented__item', scope === 'weak' && 'is-active')} onClick={() => setScope('weak')}>
              弱信号 <span className="segmented__count">{weak.length}</span>
            </button>
          </div>

          <div className="consistency-list">
            {visible.map((item) => {
              const stateMeta = meta(ENDPOINT_STATE, item.state ?? 'inconsistent')
              return (
                <article key={item.endpoint_id} className={cn('consistency-card', `is-${stateMeta.tone}`)}>
                  <header className="consistency-card__head">
                    <Pill tone={item.method === 'GET' ? 'blue' : item.method === 'DELETE' ? 'danger' : 'success'}>
                      {item.method}
                    </Pill>
                    <code>{item.path}</code>
                    <Status value={item.state ?? 'inconsistent'} map={ENDPOINT_STATE} />
                    <span className="muted small">{item.summary}</span>
                    <div className="spacer" />
                    <span className="muted small">{(item.sources ?? []).join(' / ')}</span>
                  </header>
                  <div className="consistency-card__body">
                    {(item.mismatches ?? []).map((mismatch, index) => (
                      <div key={index} className="mismatch">
                        <Pill tone={item.state === 'partial' ? 'warning' : 'danger'}>
                          {KIND_LABEL[mismatch.kind] ?? mismatch.kind}
                        </Pill>
                        <span>{mismatch.message}</span>
                      </div>
                    ))}
                  </div>
                  <footer className="consistency-card__foot">
                    <span>检查于 {fromNow(item.checked_at)}</span>
                    <div className="spacer" />
                    <span>{item.sources?.length ?? 0} 个来源参与对照</span>
                  </footer>
                </article>
              )
            })}
          </div>
        </>
      )}
    </div>
  )
}
