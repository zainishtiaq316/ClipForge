import { AlertCircle, CheckCircle2, Loader2, X } from 'lucide-react'
import type { TrackedExport } from '../../hooks/useExports'
import { ProgressBar } from '../ui/ProgressBar'

interface Props {
  exports: TrackedExport[]
  onDismiss: (key: string) => void
  onDownload: (jobId: string) => void
}

function describe({ job, error }: TrackedExport): string {
  if (error) return error
  if (!job || job.status === 'queued') return 'Waiting to start…'
  if (job.status === 'failed') return job.error ?? 'Export failed'
  if (job.status === 'done') return 'Downloaded · click to save again'
  if (job.status === 'packaging') return 'Creating ZIP…'
  const total = job.clip_ids.length
  const pct = Math.round(job.progress * 100)
  return total > 1 ? `Rendering clip ${job.current} of ${total} · ${pct}%` : `Rendering · ${pct}%`
}

export function ExportTray({ exports, onDismiss, onDownload }: Props) {
  if (!exports.length) return null
  return (
    <div className="fixed right-3 bottom-3 left-3 z-40 flex flex-col gap-2 sm:left-auto sm:w-96" aria-live="polite">
      {exports.map((entry) => {
        const { job, error } = entry
        const failed = !!error || job?.status === 'failed'
        const done = job?.status === 'done'
        return (
          <div key={entry.key} className="card flex items-start gap-3 bg-surface-2 p-3.5">
            <div className="pt-0.5">
              {failed ? (
                <AlertCircle className="size-5 text-danger" />
              ) : done ? (
                <CheckCircle2 className="size-5 text-success" />
              ) : (
                <Loader2 className="size-5 animate-spin text-accent" />
              )}
            </div>
            <div className="min-w-0 flex-1">
              <p className="truncate text-sm font-medium">Export: {entry.label}</p>
              {done && job ? (
                <button className="text-left text-xs text-accent hover:underline" onClick={() => onDownload(job.id)}>
                  {job.file_name} · save again
                </button>
              ) : (
                <p className={`text-xs ${failed ? 'text-danger' : 'text-muted'}`}>{describe(entry)}</p>
              )}
              {!done && !failed && <ProgressBar value={job?.progress ?? 0} className="mt-2" />}
            </div>
            {(done || failed) && (
              <button className="text-subtle hover:text-fg" onClick={() => onDismiss(entry.key)} aria-label="Dismiss">
                <X className="size-4" />
              </button>
            )}
          </div>
        )
      })}
    </div>
  )
}
