import { useEffect, useMemo, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { api } from '../../lib/api'
import { formatFullTime, fromNow, shortHash } from '../../lib/format'
import { useAsync } from '../../lib/hooks'
import { CASE_STATUS } from '../../lib/labels'
import type { SpecCompare, SpecSnapshot } from '../../lib/types'
import { Icon } from '../../ui/icons'
import { Button, Card, CopyButton, EmptyState, ErrorBox, Pill, Skeleton, Status, cn } from '../../ui/kit'

export default function Specs() {
  const { projectId } = useParams()
  const [sourceId, setSourceId] = useState<number | null>(null)
  const [baseId, setBaseId] = useState<number | null>(null)
  const [targetId, setTargetId] = useState<number | null>(null)
  const [showHistory, setShowHistory] = useState(false)

  const snapshots = useAsync<SpecSnapshot[]>(() => api.get(`/projects/${projectId}/spec-snapshots`), [projectId])
  const list = snapshots.data ?? []

  const sources = useMemo(() => {
    const map = new Map<number, { id: number; name: string; count: number }>()
    list.forEach((item) => {
      const current = map.get(item.source_id) ?? { id: item.source_id, name: item.source_name, count: 0 }
      current.count += 1
      map.set(item.source_id, current)
    })
    return [...map.values()].sort((a, b) => b.count - a.count)
  }, [list])

  useEffect(() => {
    if (sourceId === null && sources.length) {
      const preferred = sources[0]
      setSourceId(preferred.id)
      const items = list.filter((item) => item.source_id === preferred.id)
      if (items.length >= 2) {
        setTargetId(items[0].id)
        setBaseId(items[1].id)
      } else if (items.length === 1) {
        setTargetId(items[0].id)
        setBaseId(items[0].id)
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sources.length, list.length])

  const sourceSnapshots = useMemo(
    () => (sourceId ? list.filter((item) => item.source_id === sourceId) : []),
    [list, sourceId],
  )

  const compare = useAsync<SpecCompare | null>(
    () =>
      baseId && targetId
        ? api.get<SpecCompare>(`/projects/${projectId}/spec-snapshots/compare?base_id=${baseId}&target_id=${targetId}`)
        : Promise.resolve(null),
    [projectId, baseId, targetId],
  )

  const changedVersions = list.filter(
    (item) => item.diff_counts.added + item.diff_counts.removed + item.diff_counts.changed > 0,
  ).length

  function selectSource(id: number) {
    setSourceId(id)
    const items = list.filter((item) => item.source_id === id)
    if (items.length >= 2) {
      setTargetId(items[0].id)
      setBaseId(items[1].id)
    } else if (items.length === 1) {
      setTargetId(items[0].id)
      setBaseId(items[0].id)
    } else {
      setBaseId(null)
      setTargetId(null)
    }
  }

  if (snapshots.loading && !snapshots.data) {
    return <Card><Skeleton lines={7} /></Card>
  }
  if (snapshots.error) return <ErrorBox>{snapshots.error}</ErrorBox>

  if (!list.length) {
    return (
      <Card>
        <EmptyState
          icon="file"
          title="还没有 Spec 快照"
          description="导入 OpenAPI / Swagger 后，每次重新解析都会留一个快照。修改 spec 再导入，就能在这里对比新增、删除和变更的接口。"
        />
      </Card>
    )
  }

  const diff = compare.data?.diff
  const hasDiff = !!diff && (diff.added.length > 0 || diff.removed.length > 0 || diff.changed.length > 0)

  return (
    <div className="stack">
      <div className="section-bar">
        <div className="section-bar__stats">
          <span><strong>{list.length}</strong> 个快照</span>
          <span><strong>{sources.length}</strong> 个 spec 来源</span>
          <span className={changedVersions ? 'text-warning' : 'muted'}><strong>{changedVersions}</strong> 个版本有变更</span>
        </div>
        <div className="section-bar__actions">
          <Button size="sm" icon={showHistory ? 'chevronDown' : 'clock'} onClick={() => setShowHistory((value) => !value)}>
            {showHistory ? '收起历史' : '版本历史'}
          </Button>
        </div>
      </div>

      <Card className="spec-picker">
        <div className="spec-picker__row">
          <label>
            <span>Spec 来源</span>
            <select
              className="input"
              value={sourceId ?? ''}
              onChange={(event) => selectSource(Number(event.target.value))}
            >
              {sources.map((source) => (
                <option key={source.id} value={source.id}>{source.name}（{source.count} 个版本）</option>
              ))}
            </select>
          </label>
          <label>
            <span>基线版本</span>
            <select
              className="input"
              value={baseId ?? ''}
              onChange={(event) => setBaseId(Number(event.target.value))}
            >
              {sourceSnapshots.map((item) => (
                <option key={item.id} value={item.id}>
                  {shortHash(item.version)} · {formatFullTime(item.created_at)}
                </option>
              ))}
            </select>
          </label>
          <span className="spec-picker__arrow"><Icon name="arrowRight" size={17} /></span>
          <label>
            <span>目标版本</span>
            <select
              className="input"
              value={targetId ?? ''}
              onChange={(event) => setTargetId(Number(event.target.value))}
            >
              {sourceSnapshots.map((item) => (
                <option key={item.id} value={item.id}>
                  {shortHash(item.version)} · {formatFullTime(item.created_at)}
                </option>
              ))}
            </select>
          </label>
        </div>
      </Card>

      {sourceSnapshots.length < 2 && (
        <div className="notice notice--info">
          <Icon name="info" size={16} />
          <span>这个来源只有一个快照。修改 spec 后重新导入，就会生成新版本并自动对比。</span>
        </div>
      )}

      {compare.error && <ErrorBox>{compare.error}</ErrorBox>}
      {!compare.data && compare.loading && <Card><Skeleton lines={5} /></Card>}

      {compare.data && (
        <>
          <div className="spec-summary">
            <div className="spec-stat is-added">
              <span>新增接口</span>
              <strong>{diff?.added.length ?? 0}</strong>
            </div>
            <div className="spec-stat is-removed">
              <span>删除接口</span>
              <strong>{diff?.removed.length ?? 0}</strong>
            </div>
            <div className="spec-stat is-changed">
              <span>变更接口</span>
              <strong>{diff?.changed.length ?? 0}</strong>
            </div>
            <div className="spec-stat">
              <span>受影响用例</span>
              <strong>{compare.data.affected_total}</strong>
            </div>
          </div>

          {!hasDiff && (
            <Card>
              <EmptyState
                icon="check"
                title="两个版本没有差异"
                description={
                  compare.data.base.id === compare.data.target.id
                    ? '选中的是同一个版本，换一个目标版本试试。'
                    : '接口清单、参数、请求体和响应结构都没有变化。'
                }
              />
            </Card>
          )}

          {!!diff?.added.length && (
            <Card title={`新增接口 ${diff.added.length}`} subtitle="目标版本里新出现、基线里没有的接口。">
              <div className="spec-endpoint-list">
                {diff.added.map((item) => (
                  <div key={`${item.method}-${item.path}`} className="spec-endpoint is-added">
                    <Pill tone={item.method === 'GET' ? 'blue' : item.method === 'DELETE' ? 'danger' : 'success'}>{item.method}</Pill>
                    <code>{item.path}</code>
                    <span className="muted small">{item.summary}</span>
                  </div>
                ))}
              </div>
            </Card>
          )}

          {!!diff?.removed.length && (
            <Card title={`删除接口 ${diff.removed.length}`} subtitle="基线里有、目标版本里消失的接口。">
              <div className="spec-endpoint-list">
                {diff.removed.map((item) => (
                  <div key={`${item.method}-${item.path}`} className="spec-endpoint is-removed">
                    <Pill tone={item.method === 'GET' ? 'blue' : item.method === 'DELETE' ? 'danger' : 'success'}>{item.method}</Pill>
                    <code>{item.path}</code>
                    <span className="muted small">{item.summary}</span>
                  </div>
                ))}
              </div>
            </Card>
          )}

          {!!diff?.changed.length && (
            <Card title={`变更接口 ${diff.changed.length}`} subtitle="方法/路径不变，但参数、请求体或响应结构变了。">
              <div className="spec-endpoint-list">
                {diff.changed.map((item) => (
                  <div key={`${item.method}-${item.path}`} className="spec-endpoint is-changed">
                    <Pill tone="warning">{item.method}</Pill>
                    <code>{item.path}</code>
                    <div className="spec-endpoint__details">
                      {(item.details ?? []).map((detail, index) => (
                        <span key={index}>{detail}</span>
                      ))}
                    </div>
                  </div>
                ))}
              </div>
            </Card>
          )}

          {!!compare.data.affected_cases.length && (
            <Card
              title={`受影响用例 ${compare.data.affected_total}`}
              subtitle="这些用例的步骤命中了变更/删除的接口，重新导入时会被标记为待复核。"
            >
              <div className="affected-list">
                {compare.data.affected_cases.map((item) => (
                  <Link key={item.id} className="affected-case" to={`/projects/${projectId}/cases/${item.id}`}>
                    <Status value={item.status} map={CASE_STATUS} />
                    <span>{item.title}</span>
                    <span className="muted small">#{item.id}</span>
                    <Icon name="chevronRight" size={13} />
                  </Link>
                ))}
              </div>
            </Card>
          )}
        </>
      )}

      {showHistory && (
        <Card title="版本历史" subtitle="每次重新导入 OpenAPI / Swagger 都会留一份快照。">
          <div className="snapshot-list">
            {list.map((item) => {
              const total = item.diff_counts.added + item.diff_counts.removed + item.diff_counts.changed
              return (
                <div key={item.id} className={cn('snapshot-row', item.id === targetId && 'is-selected')}>
                  <span className="snapshot-row__dot" />
                  <div className="snapshot-row__main">
                    <strong>{item.source_name}</strong>
                    <span className="mono">{shortHash(item.version)}</span>
                    <span>{item.endpoints} 个接口</span>
                    <span className="muted">{fromNow(item.created_at)}</span>
                  </div>
                  <div className="snapshot-row__diff">
                    {total === 0 ? (
                      <span className="muted small">无变更</span>
                    ) : (
                      <>
                        {!!item.diff_counts.added && <Pill tone="success">+{item.diff_counts.added}</Pill>}
                        {!!item.diff_counts.removed && <Pill tone="danger">-{item.diff_counts.removed}</Pill>}
                        {!!item.diff_counts.changed && <Pill tone="warning">~{item.diff_counts.changed}</Pill>}
                      </>
                    )}
                  </div>
                  <CopyButton text={item.version} label="复制 hash" />
                  <button
                    className="link-btn"
                    onClick={() => {
                      const sameSource = list.filter((snapshot) => snapshot.source_id === item.source_id)
                      setSourceId(item.source_id)
                      setTargetId(item.id)
                      setBaseId(sameSource.find((snapshot) => snapshot.id !== item.id)?.id ?? item.id)
                    }}
                  >
                    作为目标版本
                  </button>
                </div>
              )
            })}
          </div>
        </Card>
      )}
    </div>
  )
}
