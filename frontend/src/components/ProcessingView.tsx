import { AlertTriangle, Check, Loader2 } from 'lucide-react'
import type { Project } from '../lib/api'
import { navigate } from '../hooks/useHashRoute'
import { Button } from './ui/Button'
import { ProgressBar } from './ui/ProgressBar'

interface Step {
  label: string
  detail: string
}

function stepsFor(project: Project): Step[] {
  return [
    project.source_type === 'youtube'
      ? { label: 'Download', detail: 'Fetching the video from YouTube' }
      : { label: 'Upload', detail: 'Video received' },
    { label: 'Analyze', detail: 'Detecting scenes, pauses in speech and faces' },
    { label: 'Plan clips', detail: 'Splitting into clips and planning the 9:16 framing' },
  ]
}

function activeIndex(project: Project): number {
  if (project.status === 'queued' || project.status === 'downloading') return 0
  if (project.stage.startsWith('Planning')) return 2
  return 1
}

export function ProcessingView({ project }: { project: Project }) {
  const failed = project.status === 'failed'
  const steps = stepsFor(project)
  const active = activeIndex(project)
  const showBar = project.stage.startsWith('Finding') || project.status === 'downloading'

  return (
    <div className="mx-auto max-w-xl px-4 py-16 sm:py-24">
      <div className="card p-6 sm:p-8">
        <p className="text-xs font-medium tracking-wide text-subtle uppercase">
          {failed ? 'Processing failed' : 'Processing'}
        </p>
        <h1 className="mt-1 truncate text-xl font-semibold" title={project.name}>
          {project.name}
        </h1>

        {failed ? (
          <div className="mt-6 flex gap-3 rounded-xl border border-danger/30 bg-danger/10 p-4 text-sm">
            <AlertTriangle className="size-5 shrink-0 text-danger" />
            <p className="leading-relaxed">{project.error}</p>
          </div>
        ) : (
          <>
            <ol className="mt-7 space-y-5">
              {steps.map((step, i) => {
                const done = i < active
                const current = i === active
                return (
                  <li key={step.label} className="flex gap-3.5">
                    <span
                      className={`grid size-7 shrink-0 place-items-center rounded-full border text-xs font-semibold transition-colors ${
                        done
                          ? 'border-success bg-success text-white'
                          : current
                            ? 'border-accent bg-accent-soft text-accent'
                            : 'border-line text-subtle'
                      }`}
                    >
                      {done ? <Check className="size-4" /> : current ? <Loader2 className="size-3.5 animate-spin" /> : i + 1}
                    </span>
                    <div className="min-w-0 flex-1 pt-0.5">
                      <p className={`text-sm font-medium ${current || done ? 'text-fg' : 'text-subtle'}`}>{step.label}</p>
                      <p className="text-xs text-muted">{current ? project.stage : step.detail}</p>
                      {current && (
                        <div className="mt-2.5 flex items-center gap-2">
                          <ProgressBar value={project.progress} indeterminate={!showBar} className="flex-1" />
                          {showBar && (
                            <span className="tabular w-9 text-right text-xs text-muted">
                              {Math.round(project.progress * 100)}%
                            </span>
                          )}
                        </div>
                      )}
                    </div>
                  </li>
                )
              })}
            </ol>
            <p className="mt-7 text-xs text-subtle">
              A 10-minute video usually takes about a minute. You can leave this page open; it updates automatically.
            </p>
          </>
        )}

        <div className="mt-7 flex gap-2">
          <Button variant={failed ? 'primary' : 'ghost'} onClick={navigate.home}>
            {failed ? 'Try another video' : 'Back to home'}
          </Button>
        </div>
      </div>
    </div>
  )
}
