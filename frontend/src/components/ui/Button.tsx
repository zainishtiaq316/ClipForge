import type { ButtonHTMLAttributes, ReactNode } from 'react'
import { Loader2 } from 'lucide-react'

type Variant = 'primary' | 'secondary' | 'ghost' | 'danger'
type Size = 'sm' | 'md' | 'lg' | 'icon'

const variants: Record<Variant, string> = {
  primary: 'bg-accent text-white hover:bg-accent-hover shadow-[0_8px_24px_-10px_rgb(124_92_255/0.8)]',
  secondary: 'bg-surface-3 text-fg hover:bg-line-strong border border-line',
  ghost: 'text-muted hover:text-fg hover:bg-surface-3',
  danger: 'text-danger hover:bg-danger/10',
}

const sizes: Record<Size, string> = {
  sm: 'h-8 px-3 text-xs gap-1.5 rounded-lg',
  md: 'h-10 px-4 text-sm gap-2 rounded-xl',
  lg: 'h-12 px-6 text-base gap-2 rounded-xl',
  icon: 'h-9 w-9 rounded-lg',
}

interface Props extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant
  size?: Size
  loading?: boolean
  icon?: ReactNode
}

export function Button({ variant = 'secondary', size = 'md', loading, icon, children, className = '', disabled, ...rest }: Props) {
  return (
    <button
      type="button"
      className={`inline-flex shrink-0 items-center justify-center font-medium whitespace-nowrap transition-all select-none active:scale-[0.98] disabled:pointer-events-none disabled:opacity-45 ${variants[variant]} ${sizes[size]} ${className}`}
      disabled={disabled || loading}
      {...rest}
    >
      {loading ? <Loader2 className="size-4 animate-spin" /> : icon}
      {children}
    </button>
  )
}
