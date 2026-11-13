import { useState } from 'react'
import { useParams } from 'react-router-dom'
import { api, apiUrl } from '../lib/api'
import { formatFullTime } from '../lib/format'
import { useAsync } from '../lib/hooks'
import type { TestDataHealth, TestDataset, TestFile } from '../lib/types'
import { Icon } from '../ui/icons'
import { Button, Card, EmptyState, ErrorBox, Field, PageHead, Pill, Skeleton } from '../ui/kit'
import { useToast } from '../ui/overlays'

const DEFAULT_FIELDS = `[
  {"name": "email", "type": "string", "format": "email", "unique": true},
  {"name": "name", "type": "string", "minLength": 3},
  {"name": "amount", "type": "number", "minimum": 1, "maximum": 1000}
]`

export default function TestData() {
  const { projectId } = useParams()
  const toast = useToast()
  const [name, setName] = useState('')
  const [count, setCount] = useState('10')
  const [seed, setSeed] = useState('42')
  const [fields, setFields] = useState(DEFAULT_FIELDS)
  const [negative, setNegative] = useState(false)
  const [busy, setBusy] = useState('')
  const [caseId, setCaseId] = useState('')
  const [file, setFile] = useState<File | null>(null)
  const [fileName, setFileName] = useState('')

  const datasets = useAsync<{ items: TestDataset[]; count: number }>(
    () => api.get(`/projects/${projectId}/test-datasets`),
    [projectId],
  )
  const health = useAsync<TestDataHealth>(
    () => api.get(`/projects/${projectId}/test-data/health`),
    [projectId],
    { refreshMs: 15_000 },
  )
  const files = useAsync<{ items: TestFile[]; count: number }>(
    () => api.get(`/projects/${projectId}/test-files`),
    [projectId],
  )

  async function createDataset() {
    setBusy('create')
    try {
      let parsed: unknown
      try {
        parsed = JSON.parse(fields)
      } catch {
        toast.error('字段 JSON 不合法')
        return
      }
      const created = await api.post<TestDataset>(`/projects/${projectId}/test-datasets`, {
        name: name.trim(),
        count: Number(count) || 10,
        seed: Number(seed) || 42,
        fields: parsed,
        negative,
      })
      setName('')
      toast.success('数据集已创建', `${created.name} · ${created.row_count} 行`)
      datasets.reload()
      health.reload()
    } catch (error) {
      toast.error('创建数据集失败', String(error))
    } finally {
      setBusy('')
    }
  }

  async function regenerate(dataset: TestDataset) {
    setBusy(`regen-${dataset.id}`)
    try {
      await api.patch(`/projects/${projectId}/test-datasets/${dataset.id}`, {
        regenerate: true,
        count: Math.max(1, dataset.row_count),
        negative: Boolean(dataset.schema?.negative),
      })
      toast.success('已重新生成', dataset.name)
      datasets.reload()
      health.reload()
    } catch (error) {
      toast.error('重新生成失败', String(error))
    } finally {
      setBusy('')
    }
  }

  async function remove(dataset: TestDataset) {
    setBusy(`del-${dataset.id}`)
    try {
      await api.del(`/projects/${projectId}/test-datasets/${dataset.id}`)
      toast.success('数据集已删除', dataset.name)
      datasets.reload()
      health.reload()
    } catch (error) {
      toast.error('删除失败', String(error))
    } finally {
      setBusy('')
    }
  }

  async function bind(dataset: TestDataset) {
    const id = Number(caseId)
    if (!id) {
      toast.error('请填写用例 ID')
      return
    }
    setBusy(`bind-${dataset.id}`)
    try {
      await api.post(`/test-cases/${id}/data-bindings`, {
        dataset_id: dataset.id,
        mode: 'iterate',
      })
      toast.success('已绑定到用例', `用例 #${id} 将按数据集行迭代`)
      health.reload()
    } catch (error) {
      toast.error('绑定失败', String(error))
    } finally {
      setBusy('')
    }
  }

  async function upload() {
    if (!file) {
      toast.error('请选择文件')
      return
    }
    setBusy('upload')
    try {
      const body = new FormData()
      body.append('file', file)
      body.append('name', fileName.trim() || file.name)
      const response = await fetch(apiUrl(`/projects/${projectId}/test-files`), { method: 'POST', body })
      if (!response.ok) throw new Error(await response.text())
      toast.success('文件已上传')
      setFile(null)
      setFileName('')
      files.reload()
      health.reload()
    } catch (error) {
      toast.error('上传失败', String(error))
    } finally {
      setBusy('')
    }
  }

  async function cleanupData() {
    setBusy('cleanup')
    try {
      const result = await api.post<Record<string, any>>(`/projects/${projectId}/test-data/cleanup`, {})
      toast.success('清理完成', `删除行 ${result.rows_deleted} · 失效绑定 ${result.stale_bindings_deleted} · 丢失文件 ${result.orphan_files_deleted}`)
      datasets.reload()
      files.reload()
      health.reload()
    } catch (error) {
      toast.error('清理失败', String(error))
    } finally {
      setBusy('')
    }
  }

  async function removeFile(testFile: TestFile) {
    setBusy(`file-${testFile.id}`)
    try {
      await api.del(`/test-files/${testFile.id}`)
      toast.success('文件已删除', testFile.name)
      files.reload()
      health.reload()
    } catch (error) {
      toast.error('删除文件失败', String(error))
    } finally {
      setBusy('')
    }
  }

  return (
    <div className="page">
      <PageHead
        title="测试数据"
        subtitle="数据集、行迭代、fixture 文件与账号/数据库准备状态。执行时通过 {{data.字段}} 引用。"
        actions={
          <Button icon="refresh" onClick={() => { datasets.reload(); health.reload(); files.reload() }}>
            刷新
          </Button>
        }
      />

      <div className="activity-summary">
        <div><span>数据集</span><strong>{health.data?.dataset_count ?? 0}</strong></div>
        <div><span>数据行</span><strong>{health.data?.row_count ?? 0}</strong></div>
        <div><span>绑定用例</span><strong>{health.data?.binding_count ?? 0}</strong></div>
        <div><span>文件</span><strong>{health.data?.file_count ?? 0}</strong></div>
        <div><span>PII 数据集</span><strong className="text-warning">{health.data?.pii_dataset_count ?? 0}</strong></div>
      </div>

      <Card title="新建数据集" subtitle="按字段定义生成确定性数据；seed 相同即可复现。">
        <div className="form-row">
          <Field label="名称">
            <input className="input" value={name} onChange={(event) => setName(event.target.value)} placeholder="users-normal" />
          </Field>
          <Field label="行数">
            <input className="input" type="number" value={count} onChange={(event) => setCount(event.target.value)} />
          </Field>
          <Field label="Seed">
            <input className="input" type="number" value={seed} onChange={(event) => setSeed(event.target.value)} />
          </Field>
        </div>
        <Field label="字段 JSON" hint="支持 type / format / enum / minimum / maximum / pattern / items / properties / unique；schema 里可加 relations 做跨字段约束。">
          <textarea className="input code-input" rows={8} value={fields} onChange={(event) => setFields(event.target.value)} />
        </Field>
        <label className="row-actions">
          <input type="checkbox" checked={negative} onChange={(event) => setNegative(event.target.checked)} />
          生成负向数据（负数 / 超长 / 类型错误）
        </label>
        <div className="row-actions">
          <Button variant="primary" icon="plus" loading={busy === 'create'} onClick={createDataset}>创建并生成</Button>
        </div>
      </Card>

      <Card title="数据集" subtitle="绑定用例后可选择 single（第一行）或 iterate（逐行执行）。">
        {datasets.error && <ErrorBox>{datasets.error}</ErrorBox>}
        {datasets.loading && !datasets.data && <Skeleton lines={6} />}
        {!datasets.loading && (datasets.data?.items.length ?? 0) === 0 && (
          <EmptyState icon="file" title="还没有数据集" description="先创建一个字段模板，或让 Agent 从 interface / schema 生成。" />
        )}
        {(datasets.data?.items ?? []).map((dataset) => (
          <div key={dataset.id} className="card" style={{ marginBottom: 12 }}>
            <div className="row-actions" style={{ justifyContent: 'space-between' }}>
              <div>
                <strong>{dataset.name}</strong>
                <span className="muted small"> · v{dataset.version} · {dataset.row_count} 行 · seed {dataset.seed}</span>
                {dataset.pii_fields.length > 0 && <Pill tone="warning">PII: {dataset.pii_fields.join(', ')}</Pill>}
              </div>
              <div className="row-actions">
                <input className="input input--sm" style={{ width: 110 }} placeholder="用例 ID" value={caseId} onChange={(event) => setCaseId(event.target.value)} />
                <Button size="sm" icon="link" loading={busy === `bind-${dataset.id}`} onClick={() => bind(dataset)}>绑定</Button>
                <Button size="sm" icon="refresh" loading={busy === `regen-${dataset.id}`} onClick={() => regenerate(dataset)}>重生成</Button>
                <Button size="sm" variant="danger" icon="trash" loading={busy === `del-${dataset.id}`} onClick={() => remove(dataset)}>删除</Button>
              </div>
            </div>
            {dataset.rows.length > 0 && (
              <div className="table-wrap" style={{ marginTop: 10 }}>
                <table className="table">
                  <thead>
                    <tr>
                      <th>#</th>
                      {Object.keys(dataset.rows[0].data).map((key) => <th key={key}>{key}</th>)}
                    </tr>
                  </thead>
                  <tbody>
                    {dataset.rows.slice(0, 5).map((row) => (
                      <tr key={row.id}>
                        <td>{row.row_index}</td>
                        {Object.keys(dataset.rows[0].data).map((key) => (
                          <td key={key} className="mono">{String(row.data[key] ?? '')}</td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        ))}
      </Card>

      <Card title="Fixture 文件" subtitle="multipart 上传和浏览器 set_input_files 使用；执行时用 {file_id} 引用。">
        {files.error && <ErrorBox>{files.error}</ErrorBox>}
        <div className="form-row">
          <Field label="文件">
            <input className="input" type="file" onChange={(event) => setFile(event.target.files?.[0] ?? null)} />
          </Field>
          <Field label="引用名" hint="用例里写 files: {upload: {file_id: N}}。">
            <input className="input" value={fileName} onChange={(event) => setFileName(event.target.value)} placeholder="avatar.png" />
          </Field>
        </div>
        <Button icon="file" loading={busy === 'upload'} onClick={upload}>上传文件</Button>
        <div className="table-wrap" style={{ marginTop: 12 }}>
          <table className="table">
            <thead><tr><th>ID</th><th>名称</th><th>文件</th><th>类型</th><th>大小</th><th>SHA-256</th><th /></tr></thead>
            <tbody>
              {(files.data?.items ?? []).map((item) => (
                <tr key={item.id}>
                  <td>{item.id}</td>
                  <td>{item.name}</td>
                  <td className="mono">{item.filename}</td>
                  <td>{item.content_type}</td>
                  <td>{item.size}</td>
                  <td className="mono">{item.sha256.slice(0, 12)}…</td>
                  <td><Button size="sm" variant="ghost" icon="trash" loading={busy === `file-${item.id}`} onClick={() => removeFile(item)}>删除</Button></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>

      <Card title="健康度" subtitle="查看数据准备缺口；用例没有绑定时仍可使用内联数据。">
        {health.error && <ErrorBox>{health.error}</ErrorBox>}
        <div className="muted small">
          <div>无行数据集：{health.data?.datasets_without_rows ?? 0}</div>
          <div>启用但未绑定数据的用例：{health.data?.cases_without_data ?? 0}</div>
          <div>孤儿行：{health.data?.orphan_rows ?? 0}</div>
          {health.data && <div>统计时间：{formatFullTime(new Date().toISOString())}</div>}
          <div>孤儿文件：{health.data?.orphan_files ?? 0} · 失效绑定：{health.data?.stale_bindings ?? 0}</div>
        </div>
        <div className="row-actions" style={{ marginTop: 10 }}>
          <Button icon="trash" loading={busy === 'cleanup'} onClick={cleanupData}>清理归档数据 / 失效绑定 / 丢失文件</Button>
        </div>
      </Card>
    </div>
  )
}
