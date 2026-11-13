import { useEffect, useMemo, useRef, useState } from 'react'
import { Link, NavLink, Outlet, useLocation, useMatch, useNavigate } from 'react-router-dom'
import { api } from '../lib/api'
import { useAsync, useDocumentTitle } from '../lib/hooks'
import { CASE_STATUS, meta } from '../lib/labels'
import type { Overview, Project, ProjectHealth, ProjectSummary, Task } from '../lib/types'
import { Icon, type IconName } from '../ui/icons'
import { cn } from '../ui/kit'
import { useToast } from '../ui/overlays'
import { CommandPalette } from './CommandPalette'
import { ProjectContext } from './project'

const NAV: Array<{ path: string; label: string; icon: IconName; badge?: 'review' | 'findings' }> = [
  { path: '', label: '总览', icon: 'compass' },
  { path: 'cases', label: '测试设计', icon: 'beaker', badge: 'review' },
  { path: 'runs', label: '执行', icon: 'play' },
  { path: 'findings', label: '缺陷', icon: 'bug', badge: 'findings' },
  { path: 'knowledge', label: '理解', icon: 'book' },
  { path: 'test-data', label: '测试数据', icon: 'file' },
  { path: 'activity', label: 'Agent 活动', icon: 'activity' },
  { path: 'automation', label: '无人值守', icon: 'zap' },
]

const TITLES: Record<string, string> = {
  '': '总览',
  cases: '测试设计',
  runs: '执行',
  findings: '缺陷',
  knowledge: '理解',
  'test-data': '测试数据',
  activity: 'Agent 活动',
  automation: '无人值守',
  settings: '项目设置',
}

export function AppShell() {
  const location = useLocation()
  const navigate = useNavigate()
  const toast = useToast()
  const wildcard = useMatch('/projects/:projectId/*')
  const exact = useMatch('/projects/:projectId')
  const projectId = wildcard?.params.projectId ?? exact?.params.projectId
  const [paletteOpen, setPaletteOpen] = useState(false)
  const [collapsed, setCollapsed] = useState(false)
  const [projectMenu, setProjectMenu] = useState(false)
  const [theme, setTheme] = useState<'light' | 'dark'>(() => {
    const stored = localStorage.getItem('shakedown-theme')
    if (stored === 'light' || stored === 'dark') return stored
    return window.matchMedia?.('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
  })
  const seenTasks = useRef<Map<number, string>>(new Map())
  const tasksInitialized = useRef(false)

  const projects = useAsync<ProjectSummary[]>(() => api.get('/projects'), [], { refreshMs: 30_000 })
  const project = useAsync<Project | null>(
    () => (projectId ? api.get<Project>(`/projects/${projectId}`) : Promise.resolve(null)),
    [projectId],
  )
  const overview = useAsync<Overview | null>(
    () => (projectId ? api.get<Overview>(`/projects/${projectId}/overview`) : Promise.resolve(null)),
    [projectId],
    { refreshMs: 20_000 },
  )
  const health = useAsync<ProjectHealth | null>(
    () => (projectId ? api.get<ProjectHealth>(`/projects/${projectId}/health`) : Promise.resolve(null)),
    [projectId],
    { refreshMs: 60_000 },
  )
  const tasks = useAsync<Task[]>(
    () => api.get(projectId ? `/tasks?project_id=${projectId}&limit=8` : '/tasks?limit=8'),
    [projectId],
    { refreshMs: 4_000 },
  )

  useEffect(() => {
    document.documentElement.dataset.theme = theme
    localStorage.setItem('shakedown-theme', theme)
  }, [theme])

  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') {
        event.preventDefault()
        setPaletteOpen((value) => !value)
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  useEffect(() => {
    if (!tasks.data) return
    const previous = seenTasks.current
    const next = new Map<number, string>()
    tasks.data.forEach((task) => {
      next.set(task.id, task.status)
      const before = previous.get(task.id)
      if (
        tasksInitialized.current &&
        (before === 'queued' || before === 'running') &&
        (task.status === 'succeeded' || task.status === 'failed')
      ) {
        if (task.status === 'succeeded') {
          toast.success('任务完成', `#${task.id} · ${task.kind}`)
        } else {
          toast.error('任务失败', task.error?.slice(0, 120) ?? `#${task.id} · ${task.kind}`)
        }
      }
    })
    seenTasks.current = next
    tasksInitialized.current = true
  }, [tasks.data, toast])

  // 任务完成 / 执行完成时刷新侧栏计数
  const activeTaskCount = useMemo(
    () => (tasks.data ?? []).filter((task) => task.status === 'queued' || task.status === 'running').length,
    [tasks.data],
  )

  const section = useMemo(() => {
    if (!projectId) return ''
    const rest = location.pathname.replace(`/projects/${projectId}`, '').replace(/^\//, '')
    return rest.split('/')[0] ?? ''
  }, [location.pathname, projectId])

  const pageTitle = TITLES[section] ?? 'Shakedown'

  const projectItems = projects.data ?? []
  const currentSummary = projectItems.find((item) => String(item.id) === projectId)
  const currentName = project.data?.name ?? currentSummary?.name ?? (projectId ? '加载中…' : '全部项目')
  const currentUrl = project.data?.base_url ?? currentSummary?.base_url ?? '加载中…'
  useDocumentTitle(
    projectId ? `${pageTitle} · ${currentName}` : location.pathname === '/settings' ? '系统设置' : '项目',
  )
  const projectValue = useMemo(
    () => ({
      project: project.data ?? null,
      projects: projectItems,
      reload: () => {
        project.reload()
        projects.reload()
        overview.reload()
      },
    }),
    [project.data, projectItems, project, projects, overview],
  )

  const healthOk = health.data?.target.ok
  const healthTone = healthOk === true ? 'success' : healthOk === false ? 'danger' : 'muted'

  return (
    <ProjectContext.Provider value={projectValue}>
      <div className={cn('shell', collapsed && 'shell--collapsed')}>
        <aside className="sidebar">
          <div className="sidebar__brand">
            <Link to="/" className="brand">
              <span className="brand__mark">
                <Icon name="wave" size={18} />
              </span>
              <span className="brand__text">
                <strong>Shakedown</strong>
                <small>测试 Agent</small>
              </span>
            </Link>
            <button className="sidebar__collapse" onClick={() => setCollapsed((value) => !value)} aria-label="折叠侧栏">
              <Icon name={collapsed ? 'chevronRight' : 'chevronLeft'} size={15} />
            </button>
          </div>

          <div className="project-switcher">
            <button className="project-switcher__button" onClick={() => setProjectMenu((value) => !value)}>
              <span className="project-switcher__avatar">
                {projectId ? currentName.slice(0, 1).toUpperCase() : '全'}
              </span>
              <span className="project-switcher__text">
                <strong>{currentName}</strong>
                <small>{projectId ? currentUrl : projects.loading ? '加载中…' : `${projectItems.length} 个项目`}</small>
              </span>
              <Icon name="chevronDown" size={14} />
            </button>
            {projectMenu && (
              <>
                <div className="project-switcher__backdrop" onClick={() => setProjectMenu(false)} />
                <div className="project-switcher__menu">
                  <div className="project-switcher__menu-label">切换项目</div>
                  {projectItems.map((item) => (
                    <button
                      key={item.id}
                      className={cn('project-switcher__item', String(item.id) === projectId && 'is-active')}
                      onClick={() => {
                        setProjectMenu(false)
                        navigate(`/projects/${item.id}`)
                      }}
                    >
                      <span className="project-switcher__avatar project-switcher__avatar--sm">
                        {item.name.slice(0, 1).toUpperCase()}
                      </span>
                      <span>
                        <strong>{item.name}</strong>
                        <small>{meta(CASE_STATUS, item.last_run_status ?? undefined).label}</small>
                      </span>
                    </button>
                  ))}
                  <Link to="/" className="project-switcher__all" onClick={() => setProjectMenu(false)}>
                    <Icon name="grid" size={14} /> 全部项目
                  </Link>
                </div>
              </>
            )}
          </div>

          <nav className="nav">
            {projectId ? (
              <>
                <div className="nav__label">工作区</div>
                {NAV.map((item) => {
                  const to = item.path ? `/projects/${projectId}/${item.path}` : `/projects/${projectId}`
                  const badge =
                    item.badge === 'review'
                      ? overview.data?.pending_review
                      : item.badge === 'findings'
                        ? overview.data?.findings.by_status.pending
                        : undefined
                  return (
                    <NavLink
                      key={item.path}
                      to={to}
                      end={!item.path}
                      className={({ isActive }) =>
                        cn(
                          'nav__item',
                          (isActive || (item.path === 'knowledge' && section === 'knowledge')) && 'is-active',
                        )
                      }
                      onClick={() => setProjectMenu(false)}
                    >
                      <Icon name={item.icon} size={17} />
                      <span>{item.label}</span>
                      {!!badge && <span className="nav__badge">{badge > 99 ? '99+' : badge}</span>}
                    </NavLink>
                  )
                })}
                <div className="nav__divider" />
                <NavLink
                  to={`/projects/${projectId}/settings`}
                  className={({ isActive }) => cn('nav__item', isActive && 'is-active')}
                >
                  <Icon name="settings" size={17} />
                  <span>项目设置</span>
                </NavLink>
              </>
            ) : (
              <>
                <div className="nav__label">工作区</div>
                <NavLink to="/" end className={({ isActive }) => cn('nav__item', isActive && 'is-active')}>
                  <Icon name="grid" size={17} />
                  <span>项目</span>
                </NavLink>
                <NavLink to="/settings" className={({ isActive }) => cn('nav__item', isActive && 'is-active')}>
                  <Icon name="settings" size={17} />
                  <span>系统设置</span>
                </NavLink>
              </>
            )}
          </nav>

          <div className="sidebar__foot">
            {projectId && (
              <div className="health-chip">
                <span className={cn('health-chip__dot', `tone-${healthTone}`)} />
                <span>
                  {health.data
                    ? healthOk === true
                      ? `环境可达 · ${health.data.target.latency_ms ?? 0}ms`
                      : healthOk === false
                        ? '环境不可达'
                        : '未配置被测地址'
                    : '检查环境…'}
                </span>
              </div>
            )}
            <div className="sidebar__footer-row">
              <div className="sidebar__version">v0.1 · 本地部署</div>
              <button
                className="sidebar__theme"
                onClick={() => setTheme((value) => (value === 'dark' ? 'light' : 'dark'))}
                aria-label={theme === 'dark' ? '切换到浅色' : '切换到深色'}
                title={theme === 'dark' ? '切换到浅色' : '切换到深色'}
              >
                <Icon name={theme === 'dark' ? 'sun' : 'moon'} size={14} />
                <span>{theme === 'dark' ? '浅色' : '深色'}</span>
              </button>
            </div>
          </div>
        </aside>

        <div className="main">
          <header className="topbar">
            <div className="topbar__crumb">
              <button className="icon-btn topbar__menu" onClick={() => setCollapsed((value) => !value)} aria-label="菜单">
                <Icon name="list" size={17} />
              </button>
              <Link to="/" className="topbar__root">项目</Link>
              {projectId && project.data && (
                <>
                  <Icon name="chevronRight" size={13} />
                  <span>{project.data.name}</span>
                  <Icon name="chevronRight" size={13} />
                  <strong>{pageTitle}</strong>
                </>
              )}
              {!projectId && <strong>{location.pathname === '/settings' ? '系统设置' : '全部项目'}</strong>}
            </div>

            <div className="topbar__actions">
              <button className="search-trigger" onClick={() => setPaletteOpen(true)}>
                <Icon name="search" size={15} />
                <span>搜索</span>
                <kbd>⌘K</kbd>
              </button>
              {activeTaskCount > 0 && projectId && (
                <Link to={`/projects/${projectId}/activity`} className="task-chip">
                  <span className="spinner" />
                  {activeTaskCount} 个任务运行中
                </Link>
              )}

            </div>
          </header>

          <main className="content">
            <Outlet />
          </main>
        </div>
      </div>

      <CommandPalette
        open={paletteOpen}
        onClose={() => setPaletteOpen(false)}
        projectId={projectId}
        projects={projectItems}
      />
    </ProjectContext.Provider>
  )
}
