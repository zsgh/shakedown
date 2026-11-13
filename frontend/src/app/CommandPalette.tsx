import { useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../lib/api'
import { useDebounced } from '../lib/hooks'
import { CASE_STATUS, FINDING_STATUS, meta } from '../lib/labels'
import type { ProjectSummary, SearchResults } from '../lib/types'
import { Icon, type IconName } from '../ui/icons'
import { cn } from '../ui/kit'

interface Command {
  id: string
  label: string
  hint?: string
  icon: IconName
  group: string
  run: () => void
}

export function CommandPalette({
  open,
  onClose,
  projectId,
  projects,
}: {
  open: boolean
  onClose: () => void
  projectId?: string
  projects: ProjectSummary[]
}) {
  const navigate = useNavigate()
  const inputRef = useRef<HTMLInputElement>(null)
  const [query, setQuery] = useState('')
  const [active, setActive] = useState(0)
  const [results, setResults] = useState<{ query: string; data: SearchResults } | null>(null)
  const debounced = useDebounced(query, 220)
  const trimmed = query.trim()

  useEffect(() => {
    if (!open) return
    setQuery('')
    setActive(0)
    setResults(null)
    window.setTimeout(() => inputRef.current?.focus(), 20)
  }, [open])

  useEffect(() => {
    if (!open) return
    function onKey(event: KeyboardEvent) {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, onClose])

  useEffect(() => {
    if (!open || !trimmed || !projectId) {
      setResults(null)
      return
    }
    // 输入还在防抖窗口内，等稳定后再发请求
    if (debounced.trim() !== trimmed) return
    let alive = true
    api
      .get<SearchResults>(`/projects/${projectId}/search-all?q=${encodeURIComponent(trimmed)}`)
      .then((data) => {
        if (alive) setResults({ query: trimmed, data })
      })
      .catch(() => {
        if (alive) setResults({ query: trimmed, data: { cases: [], findings: [], endpoints: [] } })
      })
    return () => {
      alive = false
    }
  }, [open, projectId, trimmed, debounced])

  const commands = useMemo<Command[]>(() => {
    const list: Command[] = []
    if (projectId) {
      const base = `/projects/${projectId}`
      list.push(
        { id: 'nav-overview', label: '总览', icon: 'compass', group: '前往', run: () => navigate(base) },
        { id: 'nav-cases', label: '测试设计', icon: 'beaker', group: '前往', run: () => navigate(`${base}/cases`) },
        { id: 'nav-runs', label: '执行', icon: 'play', group: '前往', run: () => navigate(`${base}/runs`) },
        { id: 'nav-findings', label: '缺陷', icon: 'bug', group: '前往', run: () => navigate(`${base}/findings`) },
        { id: 'nav-knowledge', label: '理解 · 知识源', icon: 'book', group: '前往', run: () => navigate(`${base}/knowledge/sources`) },
        { id: 'nav-endpoints', label: '理解 · 接口清单', icon: 'api', group: '前往', run: () => navigate(`${base}/knowledge/endpoints`) },
        { id: 'nav-gaps', label: '理解 · 契约缺口', icon: 'alert', group: '前往', run: () => navigate(`${base}/knowledge/gaps`) },
        { id: 'nav-activity', label: 'Agent 活动', icon: 'activity', group: '前往', run: () => navigate(`${base}/activity`) },
        { id: 'nav-settings', label: '项目设置', icon: 'settings', group: '前往', run: () => navigate(`${base}/settings`) },
      )
    }
    list.push(
      { id: 'nav-home', label: '项目列表', icon: 'grid', group: '前往', run: () => navigate('/') },
      { id: 'nav-global-settings', label: '系统设置', icon: 'settings', group: '前往', run: () => navigate('/settings') },
    )
    if (!projectId) {
      projects.forEach((project) => {
        list.push({
          id: `project-${project.id}`,
          label: project.name,
          hint: project.base_url,
          icon: 'target',
          group: '项目',
          run: () => navigate(`/projects/${project.id}`),
        })
      })
    }
    return list
  }, [navigate, projectId, projects])

  const searchCommands = useMemo<Command[]>(() => {
    if (!projectId || !results || results.query !== trimmed) return []
    const base = `/projects/${projectId}`
    return [
      ...results.data.cases.map((item) => ({
        id: `case-${item.id}`,
        label: item.title,
        hint: meta(CASE_STATUS, item.status).label,
        icon: 'beaker' as IconName,
        group: '用例',
        run: () => navigate(`${base}/cases/${item.id}`),
      })),
      ...results.data.findings.map((item) => ({
        id: `finding-${item.id}`,
        label: item.title,
        hint: meta(FINDING_STATUS, item.status).label,
        icon: 'bug' as IconName,
        group: '缺陷',
        run: () => navigate(`${base}/findings?finding=${item.id}`),
      })),
      ...results.data.endpoints.map((item) => ({
        id: `endpoint-${item.id}`,
        label: `${item.method} ${item.path}`,
        hint: item.summary,
        icon: 'api' as IconName,
        group: '接口',
        run: () => navigate(`${base}/knowledge/endpoints?endpoint=${item.id}`),
      })),
    ]
  }, [navigate, projectId, results, trimmed])

  // 本地命令（导航、项目）也要能被搜索，而不是一输入就被远端结果整组替换
  const matchingCommands = useMemo<Command[]>(() => {
    const needle = trimmed.toLowerCase()
    if (!needle) {
      // 未输入时项目只展示最近几个，避免把面板撑满；搜索时匹配全部项目
      let projectCount = 0
      return commands.filter((command) => {
        if (command.group !== '项目') return true
        projectCount += 1
        return projectCount <= 6
      })
    }
    return commands.filter((command) =>
      `${command.label} ${command.hint ?? ''} ${command.group}`.toLowerCase().includes(needle),
    )
  }, [commands, trimmed])

  const visible = useMemo(
    () => [...matchingCommands, ...searchCommands],
    [matchingCommands, searchCommands],
  )
  const groups = useMemo(() => {
    const map = new Map<string, Command[]>()
    visible.forEach((command) => {
      map.set(command.group, [...(map.get(command.group) ?? []), command])
    })
    return [...map.entries()]
  }, [visible])

  const flat = useMemo(() => groups.flatMap(([, items]) => items), [groups])

  useEffect(() => {
    setActive(0)
  }, [query, results])

  if (!open) return null

  function onKeyDown(event: React.KeyboardEvent) {
    if (event.key === 'ArrowDown') {
      event.preventDefault()
      setActive((value) => Math.min(flat.length - 1, value + 1))
    } else if (event.key === 'ArrowUp') {
      event.preventDefault()
      setActive((value) => Math.max(0, value - 1))
    } else if (event.key === 'Enter') {
      event.preventDefault()
      const command = flat[active]
      if (command) {
        command.run()
        onClose()
      }
    }
  }

  let cursor = -1
  return (
    <div className="overlay overlay--top" onMouseDown={onClose}>
      <div className="palette" onMouseDown={(event) => event.stopPropagation()}>
        <div className="palette__search">
          <Icon name="search" size={17} />
          <input
            ref={inputRef}
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            onKeyDown={onKeyDown}
            placeholder={projectId ? '搜索用例、缺陷、接口，或跳转…' : '搜索项目，或跳转…'}
          />
          <kbd>ESC</kbd>
        </div>
        <div className="palette__body">
          {trimmed && !visible.length && (
            <div className="palette__empty">
              {results?.query === trimmed ? '没有找到匹配的内容' : '搜索中…'}
            </div>
          )}
          {groups.map(([group, items]) => (
            <div key={group} className="palette__group">
              <div className="palette__group-title">{group}</div>
              {items.map((command) => {
                cursor += 1
                const index = cursor
                return (
                  <button
                    key={command.id}
                    className={cn('palette__item', index === active && 'is-active')}
                    onMouseEnter={() => setActive(index)}
                    onClick={() => {
                      command.run()
                      onClose()
                    }}
                  >
                    <span className="palette__item-icon">
                      <Icon name={command.icon} size={15} />
                    </span>
                    <span className="palette__item-label">{command.label}</span>
                    {command.hint && <span className="palette__item-hint">{command.hint}</span>}
                    <Icon name="arrowRight" size={13} className="palette__item-arrow" />
                  </button>
                )
              })}
            </div>
          ))}
        </div>
        <div className="palette__foot">
          <span><kbd>↑</kbd><kbd>↓</kbd> 选择</span>
          <span><kbd>↵</kbd> 打开</span>
          <span><kbd>⌘K</kbd> 关闭</span>
        </div>
      </div>
    </div>
  )
}
