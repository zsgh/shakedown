import { createContext, useContext } from 'react'
import type { Project, ProjectSummary } from '../lib/types'

export interface ProjectContextValue {
  project: Project | null
  projects: ProjectSummary[]
  reload: () => void
}

export const ProjectContext = createContext<ProjectContextValue>({
  project: null,
  projects: [],
  reload: () => undefined,
})

export function useProject(): ProjectContextValue {
  return useContext(ProjectContext)
}
