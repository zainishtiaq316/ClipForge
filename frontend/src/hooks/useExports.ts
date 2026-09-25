import { useCallback, useEffect, useRef, useState } from 'react'
import { api, type ExportJob } from '../lib/api'

export interface TrackedExport {
  key: string // clip id for single exports, "batch" for multi-clip exports
  label: string
  job: ExportJob | null
  error?: string
}

const POLL_MS = 800

function triggerDownload(jobId: string) {
  const a = document.createElement('a')
  a.href = api.downloadUrl(jobId)
  a.download = ''
  document.body.appendChild(a)
  a.click()
  a.remove()
}

/**
 * Starts export jobs, polls them, and downloads the result when ready.
 * `beforeExport` lets the editor flush pending clip edits first, so the server
 * never renders stale start/end times.
 */
export function useExports(projectId: string, beforeExport: () => Promise<void>) {
  const [exports, setExports] = useState<Record<string, TrackedExport>>({})
  const timers = useRef<number[]>([])

  useEffect(() => () => timers.current.forEach(clearTimeout), [])

  const update = (key: string, patch: Partial<TrackedExport>) =>
    setExports((all) => ({ ...all, [key]: { ...all[key], ...patch } }))

  const poll = useCallback((key: string, jobId: string) => {
    const tick = async () => {
      try {
        const job = await api.exportStatus(jobId)
        update(key, { job })
        if (job.status === 'done') triggerDownload(job.id)
        else if (job.status !== 'failed') timers.current.push(window.setTimeout(tick, POLL_MS))
      } catch (err) {
        update(key, { error: err instanceof Error ? err.message : 'Lost track of the export' })
      }
    }
    tick()
  }, [])

  const start = useCallback(
    async (key: string, label: string, clipIds: string[]) => {
      setExports((all) => ({ ...all, [key]: { key, label, job: null } }))
      try {
        await beforeExport()
        const job = await api.startExport(projectId, clipIds)
        update(key, { job })
        poll(key, job.id)
      } catch (err) {
        update(key, { error: err instanceof Error ? err.message : 'Export failed' })
      }
    },
    [beforeExport, poll, projectId],
  )

  const dismiss = useCallback((key: string) => {
    setExports((all) => {
      const next = { ...all }
      delete next[key]
      return next
    })
  }, [])

  const isBusy = (key: string) => {
    const entry = exports[key]
    return !!entry && !entry.error && entry.job?.status !== 'done' && entry.job?.status !== 'failed'
  }

  return { exports, start, dismiss, isBusy, redownload: triggerDownload }
}
