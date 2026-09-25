interface Props {
  value: number // 0..1
  indeterminate?: boolean
  tone?: 'accent' | 'success' | 'danger'
  className?: string
}

const tones = { accent: 'bg-accent', success: 'bg-success', danger: 'bg-danger' }

export function ProgressBar({ value, indeterminate, tone = 'accent', className = '' }: Props) {
  const pct = Math.round(Math.min(1, Math.max(0, value)) * 100)
  return (
    <div
      className={`relative h-2 overflow-hidden rounded-full bg-surface-3 ${className}`}
      role="progressbar"
      aria-valuenow={indeterminate ? undefined : pct}
      aria-valuemin={0}
      aria-valuemax={100}
    >
      <div
        className={`h-full rounded-full transition-[width] duration-500 ease-out ${tones[tone]}`}
        style={{ width: indeterminate ? '100%' : `${Math.max(pct, 2)}%`, opacity: indeterminate ? 0.35 : 1 }}
      />
      <div className="progress-shimmer absolute inset-0" />
    </div>
  )
}
