import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { ArrowLeft, Check, CloudOff, Download, Loader2 } from 'lucide-react'
import { api, type CameraPathData, type Clip, type Project } from '../../lib/api'
import { clamp, formatDuration, round3 } from '../../lib/time'
import { navigate } from '../../hooks/useHashRoute'
import { useExports } from '../../hooks/useExports'
import { Button } from '../ui/Button'
import { useToast } from '../ui/toast-context'
import { ClipInspector } from './ClipInspector'
import { ClipList } from './ClipList'
import { ExportTray } from './ExportTray'
import { SourcePlayer } from './SourcePlayer'
import { Timeline } from './Timeline'
import { VerticalPreview } from './VerticalPreview'

export const MIN_CLIP = 1
type SaveState = 'saved' | 'dirty' | 'saving' | 'error'

const sortClips = (clips: Clip[]) => [...clips].sort((a, b) => a.start - b.start || a.end - b.end)

function newId() {
  return Array.from(crypto.getRandomValues(new Uint8Array(4)), (b) => b.toString(16).padStart(2, '0')).join('')
}

export function EditorView({ initial }: { initial: Project }) {
  const toast = useToast()
  const duration = initial.media?.duration ?? 0
  const videoRef = useRef<HTMLVideoElement>(null)

  const [project, setProject] = useState(initial)
  const [clips, setClips] = useState<Clip[]>(() => sortClips(initial.clips))
  const [selectedId, setSelectedId] = useState<string | null>(initial.clips[0]?.id ?? null)
  const [checked, setChecked] = useState<Set<string>>(new Set())
  const [cameraPath, setCameraPath] = useState<CameraPathData | null>(null)
  const [saveState, setSaveState] = useState<SaveState>('saved')
  const [playRange, setPlayRange] = useState<{ start: number; end: number } | null>(null)

  const selected = clips.find((c) => c.id === selectedId) ?? null

  useEffect(() => {
    api.cameraPath(initial.id).then(setCameraPath).catch(() => toast('Could not load the tracking data', 'error'))
  }, [initial.id, toast])

  // --- autosave (debounced) ---------------------------------------------------
  const clipsRef = useRef(clips)
  const saveTimer = useRef<number | undefined>(undefined)
  const pendingSave = useRef<Promise<void> | null>(null)
  const firstRender = useRef(true)

  const saveNow = useCallback(async () => {
    window.clearTimeout(saveTimer.current)
    if (pendingSave.current) await pendingSave.current
    const snapshot = clipsRef.current
    setSaveState('saving')
    pendingSave.current = api
      .saveClips(initial.id, snapshot)
      .then(() => setSaveState(clipsRef.current === snapshot ? 'saved' : 'dirty'))
      .catch((err) => {
        setSaveState('error')
        toast(err instanceof Error ? err.message : 'Could not save your changes', 'error')
        throw err
      })
      .finally(() => {
        pendingSave.current = null
      })
    return pendingSave.current
  }, [initial.id, toast])

  useEffect(() => {
    clipsRef.current = clips
    if (firstRender.current) {
      firstRender.current = false
      return
    }
    setSaveState('dirty')
    window.clearTimeout(saveTimer.current)
    saveTimer.current = window.setTimeout(() => saveNow().catch(() => {}), 700)
  }, [clips, saveNow])

  useEffect(() => {
    const warn = (e: BeforeUnloadEvent) => {
      if (saveState === 'dirty' || saveState === 'saving') e.preventDefault()
    }
    window.addEventListener('beforeunload', warn)
    return () => window.removeEventListener('beforeunload', warn)
  }, [saveState])

  const flushBeforeExport = useCallback(async () => {
    if (saveState !== 'saved' || pendingSave.current) await saveNow()
  }, [saveNow, saveState])

  const { exports, start: startExport, dismiss, isBusy, redownload } = useExports(initial.id, flushBeforeExport)

  // --- playback helpers ---------------------------------------------------------
  const currentTime = () => videoRef.current?.currentTime ?? 0

  const seek = useCallback((t: number) => {
    const video = videoRef.current
    if (video) video.currentTime = clamp(t, 0, duration)
  }, [duration])

  const playClip = useCallback(
    (clip: Clip) => {
      setSelectedId(clip.id)
      setPlayRange({ start: clip.start, end: clip.end })
      seek(clip.start)
      videoRef.current?.play().catch(() => {})
    },
    [seek],
  )

  // Stop at the end of the clip being previewed.
  useEffect(() => {
    if (!playRange) return
    let raf = 0
    const tick = () => {
      const video = videoRef.current
      if (video && video.currentTime >= playRange.end) {
        video.pause()
        video.currentTime = playRange.end
        setPlayRange(null)
        return
      }
      raf = requestAnimationFrame(tick)
    }
    raf = requestAnimationFrame(tick)
    const video = videoRef.current
    const onPause = () => setPlayRange(null)
    video?.addEventListener('pause', onPause)
    return () => {
      cancelAnimationFrame(raf)
      video?.removeEventListener('pause', onPause)
    }
  }, [playRange])

  // --- clip editing ------------------------------------------------------------
  const updateClip = useCallback(
    (id: string, patch: Partial<Clip>) => {
      setClips((all) =>
        sortClips(
          all.map((c) => {
            if (c.id !== id) return c
            const next = { ...c, ...patch }
            next.start = round3(clamp(next.start, 0, duration - MIN_CLIP))
            next.end = round3(clamp(next.end, next.start + MIN_CLIP, duration))
            return next
          }),
        ),
      )
    },
    [duration],
  )

  const addClip = useCallback(() => {
    const t = currentTime()
    const length = Math.min(project.target_length, duration)
    let start = t
    let end = Math.min(duration, t + length)
    if (end - start < MIN_CLIP) start = Math.max(0, end - length)
    const clip: Clip = { id: newId(), start: round3(start), end: round3(end), framing: 'auto', zoom: 0, title: `Clip ${clips.length + 1}` }
    setClips((all) => sortClips([...all, clip]))
    setSelectedId(clip.id)
    toast(`Added a ${formatDuration(end - start)} clip at the playhead`, 'success')
  }, [clips.length, duration, project.target_length, toast])

  const splitClip = useCallback(() => {
    const t = round3(currentTime())
    const target = clips.find((c) => c.id === selectedId && t > c.start && t < c.end) ?? clips.find((c) => t > c.start && t < c.end)
    if (!target) return toast('Move the playhead inside a clip to split it', 'info')
    if (t - target.start < MIN_CLIP || target.end - t < MIN_CLIP) return toast('Both parts need to be at least 1 second long', 'info')
    const second: Clip = { ...target, id: newId(), start: t, title: `${target.title || 'Clip'} (2)` }
    setClips((all) => sortClips([...all.filter((c) => c.id !== target.id), { ...target, end: t }, second]))
    setSelectedId(second.id)
  }, [clips, selectedId, toast])

  const deleteClip = useCallback(
    (id: string) => {
      const index = clips.findIndex((c) => c.id === id)
      const remaining = clips.filter((c) => c.id !== id)
      setClips(remaining)
      setChecked((set) => {
        const next = new Set(set)
        next.delete(id)
        return next
      })
      if (selectedId === id) setSelectedId(remaining[Math.min(index, remaining.length - 1)]?.id ?? null)
    },
    [clips, selectedId],
  )

  const resegment = useCallback(
    async (target: number) => {
      if (!window.confirm(`Re-split the whole video into ~${target}s clips? Your current clip edits will be replaced.`)) return
      try {
        window.clearTimeout(saveTimer.current)
        const updated = await api.resegment(initial.id, target)
        firstRender.current = true // don't autosave what the server just produced
        setProject(updated)
        setClips(sortClips(updated.clips))
        setSelectedId(updated.clips[0]?.id ?? null)
        setChecked(new Set())
        setSaveState('saved')
        toast(`Created ${updated.clips.length} clips`, 'success')
      } catch (err) {
        toast(err instanceof Error ? err.message : 'Could not re-split the video', 'error')
      }
    },
    [initial.id, toast],
  )

  // --- exports -----------------------------------------------------------------
  const exportClip = (clip: Clip) => startExport(clip.id, clip.title || 'Clip', [clip.id])
  const exportMany = (ids: string[]) => {
    if (!ids.length) return
    if (ids.length === 1) {
      const clip = clips.find((c) => c.id === ids[0])
      if (clip) exportClip(clip)
      return
    }
    startExport('batch', `${ids.length} clips (ZIP)`, ids)
  }

  // --- keyboard shortcuts --------------------------------------------------------
  const shortcuts = useRef({ selected, updateClip, splitClip, seek, deleteClip })
  useLayoutEffect(() => {
    shortcuts.current = { selected, updateClip, splitClip, seek, deleteClip }
  })
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const el = e.target as HTMLElement
      if (el.closest('input, textarea, select, [contenteditable]') || e.metaKey || e.ctrlKey || e.altKey) return
      const video = videoRef.current
      const { selected, updateClip, splitClip, seek } = shortcuts.current
      if (!video) return
      const t = video.currentTime
      switch (e.key) {
        case ' ':
        case 'k':
          e.preventDefault()
          if (video.paused) video.play().catch(() => {})
          else video.pause()
          break
        case 'ArrowLeft':
          e.preventDefault()
          seek(t - (e.shiftKey ? 5 : 1))
          break
        case 'ArrowRight':
          e.preventDefault()
          seek(t + (e.shiftKey ? 5 : 1))
          break
        case 'i':
          if (selected) updateClip(selected.id, { start: Math.min(t, selected.end - MIN_CLIP) })
          break
        case 'o':
          if (selected) updateClip(selected.id, { end: Math.max(t, selected.start + MIN_CLIP) })
          break
        case 's':
          splitClip()
          break
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  const totalClipTime = useMemo(() => clips.reduce((sum, c) => sum + c.end - c.start, 0), [clips])
  const media = project.media!

  return (
    <div className="mx-auto max-w-[1500px] px-3 pt-4 pb-24 sm:px-6">
      {/* Top bar */}
      <div className="mb-4 flex flex-wrap items-center gap-3">
        <Button variant="ghost" size="icon" onClick={navigate.home} aria-label="Back to home" icon={<ArrowLeft className="size-4" />} />
        <div className="min-w-0 flex-1">
          <h1 className="truncate text-base font-semibold sm:text-lg" title={project.name}>
            {project.name}
          </h1>
          <p className="text-xs text-muted">
            {formatDuration(media.duration)} · {media.width}×{media.height} · {clips.length} clips ({formatDuration(totalClipTime)})
          </p>
        </div>
        <SaveIndicator state={saveState} onRetry={() => saveNow().catch(() => {})} />
        <Button
          variant="primary"
          icon={<Download className="size-4" />}
          disabled={!clips.length}
          loading={isBusy('batch')}
          onClick={() => exportMany(clips.map((c) => c.id))}
        >
          <span className="hidden sm:inline">Export all</span>
          <span className="sm:hidden">All</span>
        </Button>
      </div>

      {/* Players */}
      <div className="grid items-start gap-4 lg:grid-cols-[minmax(0,1fr)_300px] xl:grid-cols-[minmax(0,1fr)_340px]">
        <SourcePlayer
          ref={videoRef}
          src={api.videoUrl(project.id)}
          media={media}
          cameraPath={cameraPath}
          clip={selected}
        />
        <VerticalPreview videoRef={videoRef} cameraPath={cameraPath} clips={clips} selected={selected} />
      </div>

      <Timeline
        className="mt-4"
        duration={duration}
        clips={clips}
        sceneCuts={project.scene_cuts}
        textRanges={(cameraPath?.text_layouts ?? []).map((l) => [l.start, l.end] as [number, number])}
        selectedId={selectedId}
        videoRef={videoRef}
        onSelect={(id) => setSelectedId(id)}
        onChange={updateClip}
        onSeek={seek}
      />

      <div className="mt-4 grid items-start gap-4 lg:grid-cols-[minmax(0,1fr)_380px]">
        <div className="order-2 lg:order-1">
          <ClipList
            clips={clips}
            selectedId={selectedId}
            checked={checked}
            targetLength={project.target_length}
            onCheck={setChecked}
            onSelect={(clip) => {
              setSelectedId(clip.id)
              seek(clip.start)
            }}
            onPlay={playClip}
            onExport={exportClip}
            onExportSelected={() => exportMany(clips.filter((c) => checked.has(c.id)).map((c) => c.id))}
            onAdd={addClip}
            onResegment={resegment}
            isExporting={isBusy}
          />
        </div>
        <div className="order-1 lg:sticky lg:top-18 lg:order-2">
          <ClipInspector
            clip={selected}
            index={selected ? clips.indexOf(selected) : -1}
            duration={duration}
            videoRef={videoRef}
            playing={!!playRange}
            exporting={selected ? isBusy(selected.id) : false}
            onChange={(patch) => selected && updateClip(selected.id, patch)}
            onPlay={() => selected && playClip(selected)}
            onSplit={splitClip}
            onDelete={() => selected && deleteClip(selected.id)}
            onExport={() => selected && exportClip(selected)}
          />
        </div>
      </div>

      <ExportTray exports={Object.values(exports)} onDismiss={dismiss} onDownload={redownload} />
    </div>
  )
}

function SaveIndicator({ state, onRetry }: { state: SaveState; onRetry: () => void }) {
  if (state === 'error') {
    return (
      <button onClick={onRetry} className="flex items-center gap-1.5 text-xs text-danger hover:underline">
        <CloudOff className="size-3.5" /> <span className="hidden sm:inline">Not saved ·</span> retry
      </button>
    )
  }
  const saving = state === 'saving' || state === 'dirty'
  return (
    <span className="flex items-center gap-1.5 text-xs text-subtle" aria-live="polite">
      {saving ? <Loader2 className="size-3.5 animate-spin" /> : <Check className="size-3.5 text-success" />}
      <span className="hidden sm:inline">{saving ? 'Saving…' : 'All changes saved'}</span>
    </span>
  )
}
