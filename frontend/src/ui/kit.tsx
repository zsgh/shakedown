import { useState } from 'react'
import type { ButtonHTMLAttributes, ReactNode } from 'react'
import { Icon, type IconName } from './icons'

export function cn(...values: Array<string | false | null | undefined>): string {
  return values.filter(Boolean).join(' ')
}

/* ── Button ─────────────────────────────────────────────── */

type ButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: 'primary' | 'secondary' | 'ghost' | 'danger' | 'dark'
  size?: 'sm' | 'md' | 'lg'
  icon?: IconName
  loading?: boolean
}

export function Button({
  variant = 'secondary',
  size = 'md',
  icon,
  loading,
  className,
  children,
  disabled,
  ...rest
}: ButtonProps) {
  return (
    <button
      className={cn('btn', `btn--${variant}`, `btn--${size}`, !children && 'btn--icon', className)}
      disabled={disabled || loading}
      {...rest}
    >
      {loading ? <Icon name="loader" size={15} className="spin" /> : icon ? <Icon name={icon} size={15} /> : null}
      {children}
    </button>
  )
}

export function IconButton({
  icon,
  label,
  className,
  ...rest
}: ButtonHTMLAttributes<HTMLButtonElement> & { icon: IconName; label: string }) {
  return (
    <button className={cn('icon-btn', className)} aria-label={label} title={label} {...rest}>
      <Icon name={icon} size={16} />
    </button>
  )
}

/* ── Card ───────────────────────────────────────────────── */

export function Card({
  title,
  subtitle,
  actions,
  children,
  className,
  flush,
}: {
  title?: ReactNode
  subtitle?: ReactNode
  actions?: ReactNode
  children: ReactNode
  className?: string
  flush?: boolean
}) {
  return (
    <section className={cn('card', className)}>
      {(title || actions) && (
        <header className="card__head">
          <div>
            {title && <h2 className="card__title">{title}</h2>}
            {subtitle && <p className="card__sub">{subtitle}</p>}
          </div>
          {actions && <div className="card__actions">{actions}</div>}
        </header>
      )}
      <div className={cn('card__body', flush && 'card__body--flush')}>{children}</div>
    </section>
  )
}

/* ── Status ─────────────────────────────────────────────── */

export function Pill({
  tone = 'muted',
  dot,
  children,
  className,
}: {
  tone?: string
  dot?: boolean
  children: ReactNode
  className?: string
}) {
  return (
    <span className={cn('pill', `tone-${tone}`, className)}>
      {dot && <span className="pill__dot" />}
      {children}
    </span>
  )
}

export function Status({
  value,
  map,
  dot = true,
}: {
  value?: string | null
  map: Record<string, { label: string; tone: string }>
  dot?: boolean
}) {
  const item = (value && map[value]) || { label: value || '未知', tone: 'muted' }
  return (
    <Pill tone={item.tone} dot={dot}>
      {item.label}
    </Pill>
  )
}

/* ── Metric ─────────────────────────────────────────────── */

export function Metric({
  label,
  value,
  hint,
  tone,
  icon,
}: {
  label: string
  value: ReactNode
  hint?: ReactNode
  tone?: string
  icon?: IconName
}) {
  return (
    <div className="metric">
      <div className="metric__top">
        <span className="metric__label">{label}</span>
        {icon && <Icon name={icon} size={15} className="metric__icon" />}
      </div>
      <div className={cn('metric__value', tone && `text-${tone}`)}>{value}</div>
      {hint && <div className="metric__hint">{hint}</div>}
    </div>
  )
}

/* ── Progress ───────────────────────────────────────────── */

export function Progress({ value, tone = 'brand' }: { value: number; tone?: string }) {
  const width = Math.max(0, Math.min(100, value * 100))
  return (
    <div className="progress" role="progressbar" aria-valuenow={width} aria-valuemin={0} aria-valuemax={100}>
      <span className={cn('progress__bar', `tone-${tone}`)} style={{ width: `${width}%` }} />
    </div>
  )
}

/* ── Segmented / Tabs ───────────────────────────────────── */

export function Segmented<T extends string>({
  options,
  value,
  onChange,
}: {
  options: Array<{ value: T; label: ReactNode; count?: number }>
  value: T
  onChange: (value: T) => void
}) {
  return (
    <div className="segmented" role="tablist">
      {options.map((option) => (
        <button
          key={option.value}
          role="tab"
          aria-selected={value === option.value}
          className={cn('segmented__item', value === option.value && 'is-active')}
          onClick={() => onChange(option.value)}
        >
          {option.label}
          {option.count !== undefined && <span className="segmented__count">{option.count}</span>}
        </button>
      ))}
    </div>
  )
}

/* ── Form ───────────────────────────────────────────────── */

export function Field({
  label,
  hint,
  children,
  inline,
}: {
  label: string
  hint?: ReactNode
  children: ReactNode
  inline?: boolean
}) {
  return (
    <label className={cn('field', inline && 'field--inline')}>
      <span className="field__label">{label}</span>
      <span className="field__control">{children}</span>
      {hint && <span className="field__hint">{hint}</span>}
    </label>
  )
}

/* ── Feedback ───────────────────────────────────────────── */

export function EmptyState({
  icon = 'compass',
  title,
  description,
  action,
}: {
  icon?: IconName
  title: string
  description?: ReactNode
  action?: ReactNode
}) {
  return (
    <div className="empty-state">
      <span className="empty-state__icon">
        <Icon name={icon} size={22} />
      </span>
      <h3>{title}</h3>
      {description && <p>{description}</p>}
      {action && <div className="empty-state__action">{action}</div>}
    </div>
  )
}

export function Skeleton({ lines = 3 }: { lines?: number }) {
  return (
    <div className="skeleton">
      {Array.from({ length: lines }).map((_, index) => (
        <span key={index} style={{ width: `${92 - index * 13}%` }} />
      ))}
    </div>
  )
}

export function ErrorBox({ children }: { children: ReactNode }) {
  return (
    <div className="notice notice--danger">
      <Icon name="alert" size={16} />
      <span>{children}</span>
    </div>
  )
}

export function InfoBox({ children, tone = 'info' }: { children: ReactNode; tone?: 'info' | 'success' | 'warning' | 'danger' }) {
  return (
    <div className={cn('notice', `notice--${tone}`)}>
      <Icon name={tone === 'success' ? 'check' : tone === 'danger' ? 'alert' : 'info'} size={16} />
      <span>{children}</span>
    </div>
  )
}

/* ── Copy ───────────────────────────────────────────────── */

export function CopyButton({ text, label = '复制', size = 'sm' }: { text?: string | null; label?: string; size?: 'sm' | 'md' }) {
  const [copied, setCopied] = useState(false)
  if (!text) return null
  return (
    <Button
      size={size}
      variant="ghost"
      icon={copied ? 'check' : 'copy'}
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(text)
          setCopied(true)
          window.setTimeout(() => setCopied(false), 1600)
        } catch {
          /* 剪贴板不可用时静默失败 */
        }
      }}
    >
      {copied ? '已复制' : label}
    </Button>
  )
}

/* ── Page header ────────────────────────────────────────── */

export function PageHead({
  title,
  subtitle,
  actions,
  eyebrow,
}: {
  title: ReactNode
  subtitle?: ReactNode
  actions?: ReactNode
  eyebrow?: ReactNode
}) {
  return (
    <div className="page-head">
      <div className="page-head__text">
        {eyebrow && <div className="page-head__eyebrow">{eyebrow}</div>}
        <h1>{title}</h1>
        {subtitle && <p>{subtitle}</p>}
      </div>
      {actions && <div className="page-head__actions">{actions}</div>}
    </div>
  )
}

/* ── Key-value ──────────────────────────────────────────── */

export function KeyValue({ items }: { items: Array<{ label: ReactNode; value: ReactNode }> }) {
  return (
    <dl className="kv">
      {items.map((item, index) => (
        <div key={index} className="kv__row">
          <dt>{item.label}</dt>
          <dd>{item.value}</dd>
        </div>
      ))}
    </dl>
  )
}
