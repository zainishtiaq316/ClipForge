import { useEffect, useMemo, useRef, useState, type KeyboardEvent, type PointerEvent, type RefObject } from 'react'
import { Magnet, ZoomIn, ZoomOut } from 'lucide-react'
import type { Clip } from '../../lib/api'
import { clamp, formatDuration, formatTime } from '../../lib/time'

interface Props {
  duration: number
  clips: Clip[]
  sceneCuts: number[]
  selectedId: string | null
  videoRef: RefObject<HTMLVideoElement | null>
  onSelect: (id: string) => void
  onChange: (id: string, patch: Partial<Clip>) => void
  onSeek: (t: number) => void
  className?: string
}

const RULER_H = 26
const TRACK_H = 64
const SNAP_PX = 8
const TICK_STEPS = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 1200]
const ZOOMS = [1, 1.5, 2, 3, 4, 6, 8, 12, 16, 24, 32]

type Drag = { kind: 'scrub' } | { kind: 'start' | 'end'; id: string; snaps: number[] }

export function Timeline({ duration, clips, sceneCuts, selectedId, videoRef, onSelect, onChange, onSeek, className = '' }: Props) {
  const scrollerRef = useRef<HTMLDivElement>(null)
  const innerRef = useRef<HTMLDivElement>(null)
  const playheadRef = useRef<HTMLDivElement>(null)
  const dragRef = useRef<Drag | null>(null)
  const [viewWidth, setViewWidth] = useState(800)
  const [zoomIndex, setZoomIndex] = useState(0)
  const [snapping, setSnapping] = useState(true)
  const [dragLabel, setDragLabel] = useState<{ x: number; text: string } | null>(null)

  const zoom = ZOOMS[zoomIndex]
  const width = Math.max(viewWidth, viewWidth * zoom)
  const pps = duration > 0 ? width / duration : 1

  useEffect(() => {
    const el = scrollerRef.current
    if (!el) return
    const ro = new ResizeObserver(() => setViewWidth(el.clientWidth))
    ro.observe(el)
    return () => ro.disconnect()
  }, [])

  // Playhead follows the video every frame; auto-scroll while playing when zoomed in.
  useEffect(() => {
    let raf = 0
    const tick = () => {
      const video = videoRef.current
      const scroller = scrollerRef.current
      if (video && playheadRef.current) {
        const x = video.currentTime * pps
        playheadRef.current.style.transform = `translateX(${x}px)`
        if (scroller && !video.paused && !dragRef.current) {
          const { scrollLeft, clientWidth } = scroller
          if (x > scrollLeft + clientWidth - 40 || x < scrollLeft) scroller.scrollLeft = x - 60
        }
      }
      raf = requestAnimationFrame(tick)
    }
    raf = requestAnimationFrame(tick)
    return () => cancelAnimationFrame(raf)
  }, [pps, videoRef])

  const ticks = useMemo(() => {
    const step = TICK_STEPS.find((s) => s * pps >= 80) ?? 1800
    const out: number[] = []
    for (let t = 0; t <= duration; t += step) out.push(t)
    return { step, out }
  }, [duration, pps])

  const timeAt = (clientX: number) => {
    const rect = innerRef.current!.getBoundingClientRect()
    return clamp((clientX - rect.left) / pps, 0, duration)
  }

  const setZoom = (next: number) => {
    const scroller = scrollerRef.current
    const video = videoRef.current
    const index = clamp(next, 0, ZOOMS.length - 1)
    setZoomIndex(index)
    // Keep the playhead in view after zooming.
    if (scroller && video) {
      const nextPps = Math.max(viewWidth, viewWidth * ZOOMS[index]) / duration
      requestAnimationFrame(() => (scroller.scrollLeft = video.currentTime * nextPps - scroller.clientWidth / 2))
    }
  }

  // --- pointer interactions -------------------------------------------------------
  const beginScrub = (e: PointerEvent) => {
    if (e.button !== 0) return
    dragRef.current = { kind: 'scrub' }
    innerRef.current?.setPointerCapture(e.pointerId)
    onSeek(timeAt(e.clientX))
  }

  const beginHandle = (e: PointerEvent, clip: Clip, kind: 'start' | 'end') => {
    e.stopPropagation()
    if (e.button !== 0) return
    const others = clips.filter((c) => c.id !== clip.id).flatMap((c) => [c.start, c.end])
    const playhead = videoRef.current?.currentTime ?? 0
    dragRef.current = { kind, id: clip.id, snaps: [0, duration, playhead, ...sceneCuts, ...others] }
    videoRef.current?.pause()
    innerRef.current?.setPointerCapture(e.pointerId)
  }

  const onPointerMove = (e: PointerEvent) => {
    const drag = dragRef.current
    if (!drag) return
    let t = timeAt(e.clientX)
    if (drag.kind === 'scrub') {
      onSeek(t)
      return
    }
    if (snapping && !e.altKey) {
      const nearest = drag.snaps.reduce((best, s) => (Math.abs(s - t) < Math.abs(best - t) ? s : best), Infinity)
      if (Math.abs(nearest - t) * pps <= SNAP_PX) t = nearest
    }
    onChange(drag.id, drag.kind === 'start' ? { start: t } : { end: t })
    onSeek(t) // show the frame at the new edge
    setDragLabel({ x: t * pps, text: formatTime(t) })
  }

  const endDrag = () => {
    dragRef.current = null
    setDragLabel(null)
  }

  const onHandleKey = (e: KeyboardEvent, clip: Clip, kind: 'start' | 'end') => {
    const step = e.shiftKey ? 1 : 0.1
    const delta = e.key === 'ArrowLeft' ? -step : e.key === 'ArrowRight' ? step : 0
    if (!delta) return
    e.preventDefault()
    e.stopPropagation()
    const value = (kind === 'start' ? clip.start : clip.end) + delta
    onChange(clip.id, { [kind]: value })
    onSeek(value)
  }

  const selected = clips.find((c) => c.id === selectedId)

  return (
    <div className={`card overflow-hidden ${className}`}>
      <div className="flex flex-wrap items-center gap-2 border-b border-line px-3 py-2 sm:px-4">
        <h2 className="text-sm font-semibold">Timeline</h2>
        <p className="hidden text-xs text-subtle md:block">
          Click to seek · drag a clip's edges to trim or extend · <kbd className="font-mono">Alt</kbd> disables snapping
        </p>
        <div className="flex-1" />
        <button
          onClick={() => setSnapping((s) => !s)}
          className={`flex h-8 items-center gap-1.5 rounded-lg px-2 text-xs transition-colors ${snapping ? 'text-accent' : 'text-subtle hover:text-fg'}`}
          aria-pressed={snapping}
          title="Snap edges to scene cuts, other clips and the playhead"
        >
          <Magnet className="size-3.5" /> <span className="hidden sm:inline">Snap</span>
        </button>
        <div className="flex items-center gap-1">
          <button
            className="grid size-8 place-items-center rounded-lg text-muted hover:bg-surface-3 hover:text-fg disabled:opacity-40"
            onClick={() => setZoom(zoomIndex - 1)}
            disabled={zoomIndex === 0}
            aria-label="Zoom out"
          >
            <ZoomOut className="size-4" />
          </button>
          <input
            type="range"
            min={0}
            max={ZOOMS.length - 1}
            value={zoomIndex}
            onChange={(e) => setZoom(Number(e.target.value))}
            className="w-20 accent-accent sm:w-28"
            aria-label="Timeline zoom"
          />
          <button
            className="grid size-8 place-items-center rounded-lg text-muted hover:bg-surface-3 hover:text-fg disabled:opacity-40"
            onClick={() => setZoom(zoomIndex + 1)}
            disabled={zoomIndex === ZOOMS.length - 1}
            aria-label="Zoom in"
          >
            <ZoomIn className="size-4" />
          </button>
        </div>
      </div>

      <div ref={scrollerRef} className="scrollbar-thin overflow-x-auto overflow-y-hidden">
        <div
          ref={innerRef}
          className="relative cursor-text touch-none select-none"
          style={{ width, height: RULER_H + TRACK_H + 18 }}
          onPointerDown={beginScrub}
          onPointerMove={onPointerMove}
          onPointerUp={endDrag}
          onPointerCancel={endDrag}
        >
          {/* Ruler */}
          <div className="absolute inset-x-0 top-0 border-b border-line bg-surface-2/60" style={{ height: RULER_H }}>
            {ticks.out.map((t) => (
              <div key={t} className="absolute top-0 h-full border-l border-line-strong/70" style={{ left: t * pps }}>
                <span className="tabular absolute top-1 left-1.5 font-mono text-[10px] text-subtle">
                  {formatTime(t, 0)}
                </span>
              </div>
            ))}
          </div>

          {/* Track */}
          <div className="absolute inset-x-0" style={{ top: RULER_H + 9, height: TRACK_H }}>
            {sceneCuts.map((t) => (
              <div
                key={t}
                className="absolute inset-y-0 w-px bg-warning/35"
                style={{ left: t * pps }}
                title={`Scene change at ${formatTime(t)}`}
              />
            ))}

            {clips.map((clip, i) => {
              const isSelected = clip.id === selectedId
              const left = clip.start * pps
              const w = Math.max(2, (clip.end - clip.start) * pps)
              return (
                <div
                  key={clip.id}
                  className={`absolute inset-y-0 cursor-pointer overflow-hidden rounded-lg border transition-colors ${
                    isSelected
                      ? 'z-10 border-accent bg-accent/25'
                      : i % 2
                        ? 'border-line-strong bg-surface-3/90 hover:border-muted'
                        : 'border-line-strong bg-[#262b39]/90 hover:border-muted'
                  }`}
                  style={{ left, width: w }}
                  onPointerDown={(e) => {
                    if (e.button !== 0) return
                    e.stopPropagation()
                    onSelect(clip.id)
                    onSeek(timeAt(e.clientX))
                  }}
                  title={`${clip.title} · ${formatTime(clip.start)} – ${formatTime(clip.end)}`}
                >
                  {w > 54 && (
                    <div className="pointer-events-none px-3 py-2">
                      <p className="truncate text-xs font-medium">{clip.title || `Clip ${i + 1}`}</p>
                      <p className="tabular text-[11px] text-muted">{formatDuration(clip.end - clip.start)}</p>
                    </div>
                  )}
                </div>
              )
            })}

            {/* Trim handles for the selected clip */}
            {selected &&
              (['start', 'end'] as const).map((kind) => {
                const t = kind === 'start' ? selected.start : selected.end
                return (
                  <div
                    key={kind}
                    role="slider"
                    tabIndex={0}
                    aria-label={`Clip ${kind}`}
                    aria-valuemin={0}
                    aria-valuemax={duration}
                    aria-valuenow={t}
                    aria-valuetext={formatTime(t)}
                    className="group absolute inset-y-[-4px] z-20 flex w-4 cursor-ew-resize touch-none items-center justify-center"
                    style={{ left: t * pps - 8 }}
                    onPointerDown={(e) => beginHandle(e, selected, kind)}
                    onKeyDown={(e) => onHandleKey(e, selected, kind)}
                  >
                    <div className="h-full w-2.5 rounded-md bg-accent shadow-lg ring-2 ring-bg transition-transform group-hover:scale-x-125 group-focus-visible:ring-fg">
                      <div className="mx-auto mt-[26px] h-3 w-0.5 rounded bg-white/80" />
                    </div>
                  </div>
                )
              })}
          </div>

          {/* Playhead */}
          <div ref={playheadRef} className="pointer-events-none absolute inset-y-0 left-0 z-30 w-0">
            <div className="absolute inset-y-0 -left-px w-0.5 bg-fg" />
            <div className="absolute top-0 -left-[5px] size-2.5 rotate-45 rounded-[2px] bg-fg" />
          </div>

          {dragLabel && (
            <div
              className="tabular pointer-events-none absolute z-40 -translate-x-1/2 rounded bg-fg px-1.5 py-0.5 font-mono text-[10px] font-semibold text-bg"
              style={{ left: dragLabel.x, top: RULER_H + TRACK_H + 4 }}
            >
              {dragLabel.text}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
