import { useState } from 'react'
import { Download, Film, Play, Plus, RefreshCw } from 'lucide-react'
import type { Clip } from '../../lib/api'
import { formatDuration, formatTime } from '../../lib/time'
import { Button } from '../ui/Button'

interface Props {
  clips: Clip[]
  selectedId: string | null
  checked: Set<string>
  targetLength: number
  onCheck: (next: Set<string>) => void
  onSelect: (clip: Clip) => void
  onPlay: (clip: Clip) => void
  onExport: (clip: Clip) => void
  onExportSelected: () => void
  onAdd: () => void
  onResegment: (target: number) => void
  isExporting: (key: string) => boolean
}

const RESEGMENT_OPTIONS = [15, 30, 45, 60, 90]

export function ClipList(props: Props) {
  const { clips, selectedId, checked, onCheck, onSelect, onPlay, onExport, onExportSelected, onAdd, onResegment, isExporting } = props
  const [target, setTarget] = useState(RESEGMENT_OPTIONS.includes(props.targetLength) ? props.targetLength : 30)
  const allChecked = clips.length > 0 && clips.every((c) => checked.has(c.id))

  const toggle = (id: string) => {
    const next = new Set(checked)
    if (next.has(id)) next.delete(id)
    else next.add(id)
    onCheck(next)
  }

  return (
    <div className="card overflow-hidden">
      <div className="flex flex-wrap items-center gap-2 border-b border-line px-3 py-2.5 sm:px-4">
        <label className="flex cursor-pointer items-center gap-2 text-sm font-semibold select-none">
          <input
            type="checkbox"
            className="size-4 accent-accent"
            checked={allChecked}
            onChange={() => onCheck(allChecked ? new Set() : new Set(clips.map((c) => c.id)))}
            aria-label="Select all clips"
          />
          Clips <span className="font-normal text-subtle">({clips.length})</span>
        </label>
        <div className="flex-1" />
        {checked.size > 0 && (
          <Button size="sm" variant="primary" icon={<Download className="size-3.5" />} onClick={onExportSelected} loading={isExporting('batch')}>
            Export {checked.size} selected
          </Button>
        )}
        <Button size="sm" icon={<Plus className="size-3.5" />} onClick={onAdd} title="Add a clip starting at the playhead">
          Add clip
        </Button>
      </div>

      {clips.length === 0 ? (
        <div className="grid place-items-center px-6 py-12 text-center">
          <Film className="size-8 text-subtle" />
          <p className="mt-3 text-sm text-muted">No clips yet. Add one at the playhead or re-split the video below.</p>
        </div>
      ) : (
        <ul className="scrollbar-thin max-h-[540px] divide-y divide-line overflow-y-auto">
          {clips.map((clip, i) => {
            const active = clip.id === selectedId
            const busy = isExporting(clip.id)
            return (
              <li
                key={clip.id}
                className={`group flex items-center gap-3 px-3 py-2.5 transition-colors sm:px-4 ${active ? 'bg-accent-soft' : 'hover:bg-surface-2'}`}
              >
                <input
                  type="checkbox"
                  className="size-4 shrink-0 accent-accent"
                  checked={checked.has(clip.id)}
                  onChange={() => toggle(clip.id)}
                  aria-label={`Select ${clip.title}`}
                />
                <button className="flex min-w-0 flex-1 items-center gap-3 text-left" onClick={() => onSelect(clip)}>
                  <span
                    className={`grid size-8 shrink-0 place-items-center rounded-lg text-xs font-bold ${
                      active ? 'bg-accent text-white' : 'bg-surface-3 text-muted'
                    }`}
                  >
                    {i + 1}
                  </span>
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-sm font-medium">{clip.title || `Clip ${i + 1}`}</span>
                    <span className="tabular block font-mono text-[11px] text-subtle">
                      {formatTime(clip.start)} → {formatTime(clip.end)}
                    </span>
                  </span>
                  <span className="tabular hidden rounded-md bg-surface-3 px-2 py-0.5 text-xs text-muted sm:inline">
                    {formatDuration(clip.end - clip.start)}
                  </span>
                  {clip.framing !== 'auto' && (
                    <span className="hidden rounded-md border border-line px-1.5 py-0.5 text-[10px] text-subtle md:inline">{clip.framing}</span>
                  )}
                </button>
                <Button size="icon" variant="ghost" aria-label={`Play ${clip.title}`} onClick={() => onPlay(clip)} icon={<Play className="size-4" />} />
                <Button
                  size="icon"
                  variant="ghost"
                  aria-label={`Export ${clip.title}`}
                  title="Export this clip as MP4"
                  loading={busy}
                  onClick={() => onExport(clip)}
                  icon={<Download className="size-4" />}
                />
              </li>
            )
          })}
        </ul>
      )}

      <div className="flex flex-wrap items-center gap-2 border-t border-line bg-surface-2/50 px-3 py-2.5 text-xs sm:px-4">
        <RefreshCw className="size-3.5 text-subtle" />
        <span className="text-muted">Re-split the whole video into clips of about</span>
        <select value={target} onChange={(e) => setTarget(Number(e.target.value))} className="field h-8 w-auto px-2 py-0 text-xs" aria-label="Target clip length">
          {RESEGMENT_OPTIONS.map((s) => (
            <option key={s} value={s}>
              {s} seconds
            </option>
          ))}
        </select>
        <Button size="sm" variant="ghost" onClick={() => onResegment(target)}>
          Re-split
        </Button>
      </div>
    </div>
  )
}
