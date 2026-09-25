import { useEffect, useState } from 'react'
import { ChevronRight, Clapperboard, Link2, Trash2 } from 'lucide-react'
import { api, type Project } from '../../lib/api'
import { formatDuration } from '../../lib/time'
import { navigate } from '../../hooks/useHashRoute'
import { useToast } from '../ui/toast-context'

const statusStyle: Record<Project['status'], string> = {
  ready: 'bg-success/15 text-success',
  failed: 'bg-danger/15 text-danger',
  queued: 'bg-warning/15 text-warning',
  downloading: 'bg-warning/15 text-warning',
  analyzing: 'bg-warning/15 text-warning',
}

function timeAgo(epochSeconds: number) {
  const minutes = Math.round((Date.now() / 1000 - epochSeconds) / 60)
  if (minutes < 1) return 'just now'
  if (minutes < 60) return `${minutes} min ago`
  const hours = Math.round(minutes / 60)
  return hours < 24 ? `${hours} h ago` : `${Math.round(hours / 24)} d ago`
}

export function RecentProjects() {
  const toast = useToast()
  const [projects, setProjects] = useState<Project[] | null>(null)

  useEffect(() => {
    api.listProjects().then(setProjects).catch(() => setProjects([]))
  }, [])

  if (!projects?.length) return null

  const remove = async (project: Project) => {
    if (!window.confirm(`Delete "${project.name}" and all of its clips?`)) return
    try {
      await api.deleteProject(project.id)
      setProjects((all) => all?.filter((p) => p.id !== project.id) ?? null)
    } catch (err) {
      toast(err instanceof Error ? err.message : 'Could not delete', 'error')
    }
  }

  return (
    <section className="mx-auto mt-14 max-w-3xl">
      <h2 className="mb-3 text-sm font-semibold text-muted">Recent projects</h2>
      <ul className="card divide-y divide-line overflow-hidden">
        {projects.slice(0, 8).map((p) => (
          <li key={p.id} className="group flex items-center gap-3 px-4 py-3 transition-colors hover:bg-surface-2">
            <div className="grid size-9 shrink-0 place-items-center rounded-lg bg-surface-3 text-muted">
              {p.source_type === 'youtube' ? <Link2 className="size-4" /> : <Clapperboard className="size-4" />}
            </div>
            <button className="min-w-0 flex-1 text-left" onClick={() => navigate.project(p.id)}>
              <p className="truncate text-sm font-medium">{p.name}</p>
              <p className="text-xs text-subtle">
                {timeAgo(p.created_at)}
                {p.media && ` · ${formatDuration(p.media.duration)}`}
                {p.status === 'ready' && ` · ${p.clips.length} clips`}
              </p>
            </button>
            <span className={`hidden rounded-full px-2 py-0.5 text-[11px] font-medium capitalize sm:inline ${statusStyle[p.status]}`}>
              {p.status === 'analyzing' || p.status === 'downloading' ? 'processing' : p.status}
            </span>
            <button
              onClick={() => remove(p)}
              className="rounded-md p-1.5 text-subtle opacity-100 transition hover:bg-danger/10 hover:text-danger sm:opacity-0 sm:group-hover:opacity-100"
              aria-label={`Delete ${p.name}`}
            >
              <Trash2 className="size-4" />
            </button>
            <ChevronRight className="size-4 text-subtle" />
          </li>
        ))}
      </ul>
    </section>
  )
}
