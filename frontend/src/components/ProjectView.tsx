import { useEffect, useState } from 'react'
import { Loader2 } from 'lucide-react'
import { api, ApiError, type Project } from '../lib/api'
import { navigate } from '../hooks/useHashRoute'
import { EditorView } from './editor/EditorView'
import { ProcessingView } from './ProcessingView'
import { Button } from './ui/Button'

const POLL_MS = 1000

export function ProjectView({ projectId }: { projectId: string }) {
  const [project, setProject] = useState<Project | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    let timer: number | undefined
    const load = async () => {
      try {
        const data = await api.getProject(projectId)
        if (cancelled) return
        setProject(data)
        if (data.status !== 'ready' && data.status !== 'failed') timer = window.setTimeout(load, POLL_MS)
      } catch (err) {
        if (cancelled) return
        if (err instanceof ApiError && err.status === 404) setError('This project does not exist or was deleted.')
        else timer = window.setTimeout(load, POLL_MS * 3) // transient network error: keep trying
      }
    }
    load()
    return () => {
      cancelled = true
      window.clearTimeout(timer)
    }
  }, [projectId])

  if (error) {
    return (
      <div className="mx-auto max-w-md px-4 py-24 text-center">
        <p className="text-muted">{error}</p>
        <Button className="mt-6" variant="primary" onClick={navigate.home}>
          Start a new project
        </Button>
      </div>
    )
  }

  if (!project) {
    return (
      <div className="grid place-items-center py-32 text-muted">
        <Loader2 className="size-6 animate-spin" />
      </div>
    )
  }

  if (project.status !== 'ready') return <ProcessingView project={project} />
  return <EditorView initial={project} />
}
