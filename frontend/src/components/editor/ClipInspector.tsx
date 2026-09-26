import { useState, type RefObject } from 'react'
import { Crosshair, Download, Maximize, Minus, Sparkles, MousePointerClick, Play, Plus, Scissors, SquareDashed, Trash2, Timer } from 'lucide-react'
import type { Clip, Framing } from '../../lib/api'
import { formatDuration, formatTime, parseTime } from '../../lib/time'
import { Button } from '../ui/Button'

interface Props {
  clip: Clip | null
  index: number
  duration: number
  videoRef: RefObject<HTMLVideoElement | null>
  playing: boolean
  exporting: boolean
  onChange: (patch: Partial<Clip>) => void
  onPlay: () => void
  onSplit: () => void
  onDelete: () => void
  onExport: () => void
}

const LENGTH_PRESETS = [15, 30, 45, 60]

function TimeField({ label, value, onCommit }: { label: string; value: number; onCommit: (v: number) => void }) {
  const [text, setText] = useState(formatTime(value))
  const [invalid, setInvalid] = useState(false)

  const commit = () => {
    const parsed = parseTime(text)
    if (parsed === null) {
      setInvalid(true)
      return
    }
    setInvalid(false)
    onCommit(parsed)
    setText(formatTime(value)) // parent remounts us via `key` if the value actually changed
  }

  return (
    <input
      aria-label={label}
      value={text}
      onChange={(e) => setText(e.target.value)}
      onBlur={commit}
      onKeyDown={(e) => e.key === 'Enter' && (e.currentTarget as HTMLInputElement).blur()}
      className={`field tabular h-9 flex-1 px-2.5 py-0 text-center font-mono text-[13px] ${invalid ? 'border-danger' : ''}`}
      title="Format m:ss.s, e.g. 1:23.5"
    />
  )
}

function EdgeControl({
  label,
  value,
  onSet,
  onSetToPlayhead,
  shortcut,
}: {
  label: string
  value: number
  onSet: (v: number) => void
  onSetToPlayhead: () => void
  shortcut: string
}) {
  return (
    <div>
      <div className="mb-1.5 flex items-center justify-between">
        <span className="text-xs font-medium text-muted">{label}</span>
        <button onClick={onSetToPlayhead} className="flex items-center gap-1 text-[11px] text-accent hover:underline" title={`Shortcut: ${shortcut}`}>
          <MousePointerClick className="size-3" /> Set to playhead
        </button>
      </div>
      <div className="flex items-center gap-1.5">
        <Button size="icon" variant="secondary" aria-label={`${label} 1 second earlier`} onClick={() => onSet(value - 1)} icon={<Minus className="size-3.5" />} />
        <TimeField key={value} label={label} value={value} onCommit={onSet} />
        <Button size="icon" variant="secondary" aria-label={`${label} 1 second later`} onClick={() => onSet(value + 1)} icon={<Plus className="size-3.5" />} />
      </div>
    </div>
  )
}

export function ClipInspector({ clip, index, duration, videoRef, playing, exporting, onChange, onPlay, onSplit, onDelete, onExport }: Props) {
  if (!clip) {
    return (
      <div className="card grid place-items-center px-6 py-14 text-center">
        <SquareDashed className="size-8 text-subtle" />
        <p className="mt-3 text-sm font-medium">No clip selected</p>
        <p className="mt-1 text-xs text-muted">Pick a clip on the timeline or in the list to edit it.</p>
      </div>
    )
  }

  const length = clip.end - clip.start
  const playhead = () => videoRef.current?.currentTime ?? 0
  const setLength = (seconds: number) => {
    // Extend the end first; if the video ends, grow backwards from the start.
    const end = Math.min(duration, clip.start + seconds)
    const start = Math.max(0, end - seconds)
    onChange({ start, end })
  }

  return (
    <div className="card p-4 sm:p-5">
      <div className="flex items-center gap-2">
        <span className="grid size-7 shrink-0 place-items-center rounded-lg bg-accent text-xs font-bold text-white">{index + 1}</span>
        <input
          aria-label="Clip title"
          value={clip.title}
          maxLength={80}
          placeholder={`Clip ${index + 1}`}
          onChange={(e) => onChange({ title: e.target.value })}
          className="min-w-0 flex-1 rounded-lg border border-transparent bg-transparent px-2 py-1 text-base font-semibold outline-none hover:border-line focus:border-accent"
        />
      </div>

      <div className="mt-5 grid gap-4 sm:grid-cols-2 lg:grid-cols-1">
        <EdgeControl
          label="Start"
          value={clip.start}
          shortcut="I"
          onSet={(v) => onChange({ start: Math.min(v, clip.end - 1) })}
          onSetToPlayhead={() => onChange({ start: Math.min(playhead(), clip.end - 1) })}
        />
        <EdgeControl
          label="End"
          value={clip.end}
          shortcut="O"
          onSet={(v) => onChange({ end: Math.max(v, clip.start + 1) })}
          onSetToPlayhead={() => onChange({ end: Math.max(playhead(), clip.start + 1) })}
        />
      </div>

      <div className="mt-5 rounded-xl border border-line bg-surface-2 p-3.5">
        <div className="flex items-center justify-between">
          <span className="flex items-center gap-1.5 text-xs font-medium text-muted">
            <Timer className="size-3.5" /> Clip length
          </span>
          <span className="tabular text-lg font-semibold">{formatDuration(length)}</span>
        </div>
        <div className="mt-3 grid grid-cols-2 gap-2">
          <Button size="sm" onClick={() => onChange({ end: clip.end - 5 })} disabled={length <= 1.5} icon={<Minus className="size-3.5" />}>
            Shorten 5s
          </Button>
          <Button size="sm" onClick={() => setLength(length + 5)} disabled={length >= duration} icon={<Plus className="size-3.5" />}>
            Extend 5s
          </Button>
        </div>
        <div className="mt-2 grid grid-cols-4 gap-1.5">
          {LENGTH_PRESETS.map((s) => (
            <button
              key={s}
              onClick={() => setLength(s)}
              disabled={s > duration}
              className={`h-7 rounded-md border text-xs font-medium transition-colors disabled:opacity-40 ${
                Math.abs(length - s) < 0.05 ? 'border-accent bg-accent-soft text-fg' : 'border-line text-muted hover:text-fg'
              }`}
            >
              {s}s
            </button>
          ))}
        </div>
      </div>

      <div className="mt-5">
        <span className="mb-1.5 block text-xs font-medium text-muted">Framing</span>
        <div className="grid grid-cols-4 gap-1 rounded-xl bg-surface-2 p-1">
          {(
            [
              ['auto', 'Smart', Sparkles],
              ['track', 'Track', Crosshair],
              ['center', 'Center', SquareDashed],
              ['fit', 'Fit', Maximize],
            ] as [Framing, string, typeof Crosshair][]
          ).map(([value, label, Icon]) => (
            <button
              key={value}
              onClick={() => onChange({ framing: value })}
              aria-pressed={clip.framing === value}
              className={`flex h-9 items-center justify-center gap-1.5 rounded-lg text-xs font-medium transition-colors ${
                clip.framing === value ? 'bg-surface-3 text-fg shadow-sm' : 'text-muted hover:text-fg'
              }`}
            >
              <Icon className="size-3.5" /> {label}
            </button>
          ))}
        </div>
        <p className="mt-1.5 text-[11px] text-subtle">
          {clip.framing === 'auto'
            ? 'Follows the speaker, and shows the whole frame whenever on-screen text would be cut.'
            : clip.framing === 'track'
              ? 'Always a 9:16 crop that follows the speaker (or main moving element), even over text.'
              : clip.framing === 'center'
              ? 'A fixed crop from the middle of the frame.'
              : 'Shows the whole frame over a blurred background. Good for slides, titles and wide group shots.'}
        </p>
      </div>

      <div className="mt-5 grid grid-cols-3 gap-2">
        <Button size="sm" onClick={onPlay} icon={<Play className="size-3.5" />} disabled={playing}>
          {playing ? 'Playing' : 'Play'}
        </Button>
        <Button size="sm" onClick={onSplit} icon={<Scissors className="size-3.5" />} title="Split at playhead (S)">
          Split
        </Button>
        <Button size="sm" variant="danger" onClick={onDelete} icon={<Trash2 className="size-3.5" />}>
          Delete
        </Button>
      </div>
      <Button variant="primary" className="mt-3 w-full" onClick={onExport} loading={exporting} icon={<Download className="size-4" />}>
        {exporting ? 'Exporting…' : 'Export this clip'}
      </Button>

      <p className="mt-4 text-[11px] leading-relaxed text-subtle">
        Shortcuts: <kbd className="font-mono text-muted">Space</kbd> play · <kbd className="font-mono text-muted">←/→</kbd> seek ·{' '}
        <kbd className="font-mono text-muted">I</kbd>/<kbd className="font-mono text-muted">O</kbd> set start/end ·{' '}
        <kbd className="font-mono text-muted">S</kbd> split
      </p>
    </div>
  )
}
