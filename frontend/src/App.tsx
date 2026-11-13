import { Navigate, Route, Routes, useParams } from 'react-router-dom'
import { AppShell } from './app/AppShell'
import Automation from './pages/Automation'
import Activity from './pages/Activity'
import Design from './pages/Design'
import Findings from './pages/Findings'
import Knowledge from './pages/Knowledge'
import Overview from './pages/Overview'
import ProjectSettings from './pages/ProjectSettings'
import Projects from './pages/Projects'
import RunDetail from './pages/RunDetail'
import Runs from './pages/Runs'
import RunCompare from './pages/RunCompare'
import Settings from './pages/Settings'
import { EmptyState, Button } from './ui/kit'
import { Link } from 'react-router-dom'

export default function App() {
  return (
    <Routes>
      <Route element={<AppShell />}>
        <Route index element={<Projects />} />
        <Route path="settings" element={<Settings />} />
        <Route path="projects/:projectId">
          <Route index element={<Overview />} />
          <Route path="cases" element={<Design />} />
          <Route path="cases/:caseId" element={<Design />} />
          <Route path="runs" element={<Runs />} />
          <Route path="runs/compare" element={<RunCompare />} />
          <Route path="runs/:runId" element={<RunDetail />} />
          <Route path="automation" element={<Automation />} />
          <Route path="findings" element={<Findings />} />
          <Route path="knowledge" element={<Navigate to="sources" replace />} />
          <Route path="knowledge/:tab" element={<KnowledgeRoute />} />
          <Route path="activity" element={<Activity />} />
          <Route path="settings" element={<ProjectSettings />} />
          <Route path="sources" element={<LegacyRedirect to="knowledge/sources" />} />
          <Route path="endpoints" element={<LegacyRedirect to="knowledge/endpoints" />} />
          <Route path="consistency" element={<LegacyRedirect to="knowledge/consistency" />} />
          <Route path="traces" element={<LegacyRedirect to="activity" />} />
        </Route>
        <Route path="*" element={<NotFound />} />
      </Route>
    </Routes>
  )
}

function KnowledgeRoute() {
  const { tab } = useParams()
  const value = tab === 'endpoints' || tab === 'consistency' || tab === 'gaps' || tab === 'specs' ? tab : 'sources'
  return <Knowledge tab={value} />
}

function LegacyRedirect({ to }: { to: string }) {
  const { projectId } = useParams()
  return <Navigate to={`/projects/${projectId}/${to}`} replace />
}

function NotFound() {
  return (
    <div className="page">
      <div className="card">
        <EmptyState
          icon="compass"
          title="页面不存在"
          description="链接可能过期了，回到项目列表重新进入。"
          action={
            <Link to="/">
              <Button variant="primary">回到项目</Button>
            </Link>
          }
        />
      </div>
    </div>
  )
}
