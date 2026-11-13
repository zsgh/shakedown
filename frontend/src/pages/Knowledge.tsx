import { Link, NavLink, useParams } from 'react-router-dom'
import { useProject } from '../app/project'
import { Icon } from '../ui/icons'
import { PageHead, cn } from '../ui/kit'
import Consistency from './knowledge/Consistency'
import Endpoints from './knowledge/Endpoints'
import Gaps from './knowledge/Gaps'
import Sources from './knowledge/Sources'
import Specs from './knowledge/Specs'

const TABS = [
  { key: 'sources', label: '知识源', icon: 'book' },
  { key: 'endpoints', label: '接口清单', icon: 'api' },
  { key: 'consistency', label: '三方一致性', icon: 'layers' },
  { key: 'gaps', label: '契约缺口', icon: 'alert' },
  { key: 'specs', label: 'Spec 版本', icon: 'file' },
] as const

export default function Knowledge({ tab }: { tab: 'sources' | 'endpoints' | 'consistency' | 'gaps' | 'specs' }) {
  const { projectId } = useParams()
  const { project } = useProject()

  return (
    <div className="page">
      <PageHead
        title="理解"
        subtitle={
          project?.isolation?.mode
            ? `Agent 的依据来源。当前隔离模式：${project.isolation.mode}。`
            : 'Agent 的依据来源：代码、文档、OpenAPI，以及它们之间的差异。'
        }
        actions={
          tab === 'gaps' ? (
            <Link className="btn btn--secondary btn--sm" to={`/projects/${projectId}/automation`}>
              <Icon name="gauge" size={14} /> 断言质量 / 用例退役
            </Link>
          ) : undefined
        }
      />
      <nav className="page-tabs">
        {TABS.map((item) => (
          <NavLink
            key={item.key}
            to={`/projects/${projectId}/knowledge/${item.key}`}
            className={({ isActive }) => cn('page-tabs__item', isActive && 'is-active')}
          >
            <Icon name={item.icon} size={15} />
            {item.label}
          </NavLink>
        ))}
      </nav>
      {tab === 'sources' && <Sources />}
      {tab === 'endpoints' && <Endpoints />}
      {tab === 'consistency' && <Consistency />}
      {tab === 'gaps' && <Gaps />}
      {tab === 'specs' && <Specs />}
    </div>
  )
}
