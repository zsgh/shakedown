import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../lib/api'
import { useAsync } from '../lib/hooks'
import { fromNow, percent } from '../lib/format'
import { RUN_STATUS, meta } from '../lib/labels'
import type { ProjectSummary } from '../lib/types'
import { Icon } from '../ui/icons'
import { Button, Card, EmptyState, Field, PageHead, Pill, Progress, Status } from '../ui/kit'
import { ConfirmDialog, Modal, useToast } from '../ui/overlays'

export default function Projects() {
  const navigate = useNavigate()
  const toast = useToast()
  const { data, error, loading, reload } = useAsync<ProjectSummary[]>(() => api.get('/projects'), [], {
    refreshMs: 30_000,
  })
  const [wizardOpen, setWizardOpen] = useState(false)
  const [deleting, setDeleting] = useState<ProjectSummary | null>(null)
  const [busy, setBusy] = useState(false)

  async function remove() {
    if (!deleting) return
    setBusy(true)
    try {
      await api.del(`/projects/${deleting.id}`)
      toast.success('项目已删除', deleting.name)
      setDeleting(null)
      reload()
    } catch (error) {
      toast.error('删除失败', String(error))
    } finally {
      setBusy(false)
    }
  }

  const projects = data ?? []
  const reviewTotal = projects.reduce((sum, item) => sum + item.pending_review, 0)
  const findingTotal = projects.reduce((sum, item) => sum + item.findings_pending, 0)

  return (
    <div className="page">
      <PageHead
        eyebrow="工作区"
        title="项目"
        subtitle="每个项目是一套完全隔离的被测环境、知识源与用例。"
        actions={
          <Button variant="primary" icon="plus" onClick={() => setWizardOpen(true)}>
            新建项目
          </Button>
        }
      />

      <div className="summary-strip">
        <div className="summary-strip__item">
          <span>项目</span>
          <strong>{projects.length}</strong>
        </div>
        <div className="summary-strip__item">
          <span>待审核用例</span>
          <strong className={reviewTotal ? 'text-warning' : undefined}>{reviewTotal}</strong>
        </div>
        <div className="summary-strip__item">
          <span>待确认缺陷</span>
          <strong className={findingTotal ? 'text-danger' : undefined}>{findingTotal}</strong>
        </div>
        <div className="summary-strip__spacer" />
        <span className="muted small">数据每 30 秒自动刷新</span>
      </div>

      {error && <div className="notice notice--danger">{error}</div>}
      {loading && !data && (
        <div className="project-grid">
          {[0, 1, 2].map((index) => (
            <div key={index} className="project-card project-card--skeleton" />
          ))}
        </div>
      )}

      {!loading && projects.length === 0 && (
        <Card>
          <EmptyState
            icon="target"
            title="连接第一个被测项目"
            description="给一个仓库路径或被测地址，Agent 会读文档、抽接口、生成测试点，再在沙箱里真实执行。"
            action={
              <Button variant="primary" icon="plus" onClick={() => setWizardOpen(true)}>
                新建项目
              </Button>
            }
          />
        </Card>
      )}

      <div className="project-grid">
        {projects.map((project) => {
          const runMeta = meta(RUN_STATUS, project.last_run_status ?? undefined)
          const hasAttention = project.pending_review > 0 || project.findings_pending > 0 || project.last_run_status === 'failed'
          return (
            <article
              key={project.id}
              className="project-card"
              onClick={() => navigate(`/projects/${project.id}`)}
              tabIndex={0}
              onKeyDown={(event) => {
                if (event.key === 'Enter') navigate(`/projects/${project.id}`)
              }}
            >
              <header className="project-card__head">
                <span className="project-card__avatar">{project.name.slice(0, 1).toUpperCase()}</span>
                <div className="project-card__title">
                  <h3>{project.name}</h3>
                  <p>{project.description || '暂无描述'}</p>
                </div>
                <button
                  className="icon-btn project-card__delete"
                  title="删除项目"
                  onClick={(event) => {
                    event.stopPropagation()
                    setDeleting(project)
                  }}
                >
                  <Icon name="trash" size={15} />
                </button>
              </header>

              <div className="project-card__url mono">{project.base_url || '未配置被测地址'}</div>

              <div className="project-card__stats">
                <div>
                  <span>覆盖率</span>
                  <strong>{percent(project.coverage_rate)}</strong>
                </div>
                <div>
                  <span>用例</span>
                  <strong>
                    {project.enabled_case_count}
                    <small> / {project.case_count}</small>
                  </strong>
                </div>
                <div>
                  <span>接口</span>
                  <strong>{project.endpoint_count}</strong>
                </div>
                <div>
                  <span>知识源</span>
                  <strong>{project.source_count}</strong>
                </div>
              </div>

              <div className="project-card__coverage">
                <Progress value={project.coverage_rate} tone={project.coverage_rate >= 0.8 ? 'success' : 'warning'} />
              </div>

              <footer className="project-card__foot">
                <div className="project-card__run">
                  {project.last_run_status ? (
                    <Status value={project.last_run_status} map={RUN_STATUS} />
                  ) : (
                    <Pill tone="muted">未执行</Pill>
                  )}
                  <span className="muted small">
                    {project.last_run_at ? fromNow(project.last_run_at) : '还没有执行记录'}
                    {project.last_run_pass_rate !== null && project.last_run_pass_rate !== undefined
                      ? ` · 通过 ${percent(project.last_run_pass_rate)}`
                      : ''}
                  </span>
                </div>
                <div className="project-card__badges">
                  {project.pending_review > 0 && (
                    <Pill tone="warning" dot>
                      {project.pending_review} 待审核
                    </Pill>
                  )}
                  {project.findings_pending > 0 && (
                    <Pill tone="danger" dot>
                      {project.findings_pending} 缺陷
                    </Pill>
                  )}
                  {!hasAttention && project.last_run_status && (
                    <Pill tone={runMeta.tone} dot>
                      {runMeta.label}
                    </Pill>
                  )}
                </div>
              </footer>
            </article>
          )
        })}
      </div>

      <ProjectWizard
        open={wizardOpen}
        onClose={() => setWizardOpen(false)}
        onCreated={(project) => {
          setWizardOpen(false)
          reload()
          navigate(`/projects/${project.id}`)
        }}
      />

      <ConfirmDialog
        open={!!deleting}
        title={`删除项目「${deleting?.name ?? ''}」？`}
        description="项目下的知识源、用例、执行记录和缺陷都会被删除，且不可恢复。"
        confirmLabel="删除项目"
        danger
        busy={busy}
        onCancel={() => setDeleting(null)}
        onConfirm={remove}
      />
    </div>
  )
}

function ProjectWizard({
  open,
  onClose,
  onCreated,
}: {
  open: boolean
  onClose: () => void
  onCreated: (project: { id: number }) => void
}) {
  const toast = useToast()
  const [step, setStep] = useState(0)
  const [name, setName] = useState('')
  const [baseUrl, setBaseUrl] = useState('')
  const [description, setDescription] = useState('')
  const [repoPath, setRepoPath] = useState('')
  const [busy, setBusy] = useState(false)

  function reset() {
    setStep(0)
    setName('')
    setBaseUrl('')
    setDescription('')
    setRepoPath('')
  }

  async function create() {
    setBusy(true)
    try {
      const project = await api.post<{ id: number }>('/projects', {
        name: name.trim(),
        base_url: baseUrl.trim(),
        description: description.trim(),
      })
      if (repoPath.trim()) {
        try {
          await api.post(`/projects/${project.id}/sources/auto-import`, { path: repoPath.trim() })
          toast.success('项目已创建', '知识源正在后台解析')
        } catch (error) {
          toast.error('项目已创建，但知识源导入失败', String(error))
        }
      } else {
        toast.success('项目已创建')
      }
      reset()
      onCreated(project)
    } catch (error) {
      toast.error('创建失败', String(error))
    } finally {
      setBusy(false)
    }
  }

  const canNext = step === 0 ? name.trim().length > 0 : true

  return (
    <Modal
      open={open}
      onClose={() => {
        reset()
        onClose()
      }}
      title="新建项目"
      subtitle="三步建好工作区。知识源可以稍后再补。"
      size="lg"
      footer={
        <>
          <span className="wizard__steps">第 {step + 1} / 3 步</span>
          <div className="spacer" />
          {step > 0 && <Button onClick={() => setStep((value) => value - 1)}>上一步</Button>}
          {step < 2 ? (
            <Button variant="primary" disabled={!canNext} onClick={() => setStep((value) => value + 1)}>
              下一步
            </Button>
          ) : (
            <Button variant="primary" icon="sparkles" loading={busy} onClick={create}>
              创建并开始
            </Button>
          )}
        </>
      }
    >
      <div className="wizard__progress">
        {['基本信息', '连接知识', '确认'].map((label, index) => (
          <div key={label} className={`wizard__step ${index <= step ? 'is-active' : ''}`}>
            <span>{index + 1}</span>
            {label}
          </div>
        ))}
      </div>

      {step === 0 && (
        <div className="stack">
          <Field label="项目名称" hint="比如「用户中心」「订单服务」。">
            <input
              className="input"
              value={name}
              autoFocus
              onChange={(event) => setName(event.target.value)}
              placeholder="用户中心"
            />
          </Field>
          <Field label="被测环境地址" hint="被测系统已经跑起来的地址；沙箱执行时只访问它。">
            <input
              className="input mono"
              value={baseUrl}
              onChange={(event) => setBaseUrl(event.target.value)}
              placeholder="http://127.0.0.1:8888"
            />
          </Field>
          <Field label="描述（可选）">
            <input
              className="input"
              value={description}
              onChange={(event) => setDescription(event.target.value)}
              placeholder="一句话说明这个项目"
            />
          </Field>
        </div>
      )}

      {step === 1 && (
        <div className="stack">
          <Field
            label="仓库目录（可选）"
            hint="填本地已 clone 的路径，会自动发现代码、Markdown 文档和 OpenAPI，并把文档里的接口清单一并解析。"
          >
            <input
              className="input mono"
              value={repoPath}
              autoFocus
              onChange={(event) => setRepoPath(event.target.value)}
              placeholder="/Users/you/github/my-service"
            />
          </Field>
          <div className="notice notice--info">
            <Icon name="info" size={16} />
            <span>也可以创建后在「理解 → 知识源」里逐个添加文档、接口 spec 或代码目录。</span>
          </div>
        </div>
      )}

      {step === 2 && (
        <div className="review-list">
          <div>
            <span>项目名称</span>
            <strong>{name}</strong>
          </div>
          <div>
            <span>被测地址</span>
            <strong className="mono">{baseUrl || '未配置'}</strong>
          </div>
          <div>
            <span>描述</span>
            <strong>{description || '—'}</strong>
          </div>
          <div>
            <span>知识源</span>
            <strong>{repoPath ? '创建后自动导入仓库' : '稍后添加'}</strong>
          </div>
        </div>
      )}
    </Modal>
  )
}
