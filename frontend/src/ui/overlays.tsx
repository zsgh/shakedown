import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react'
import type { ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { Button, IconButton, cn } from './kit'
import { Icon } from './icons'

/* ── Drawer ─────────────────────────────────────────────── */

export function Drawer({
  open,
  onClose,
  title,
  subtitle,
  actions,
  children,
  width = 620,
}: {
  open: boolean
  onClose: () => void
  title: ReactNode
  subtitle?: ReactNode
  actions?: ReactNode
  children: ReactNode
  width?: number
}) {
  useEffect(() => {
    if (!open) return
    function onKey(event: KeyboardEvent) {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, onClose])

  if (!open) return null
  return createPortal(
    <div className="overlay" onMouseDown={onClose}>
      <aside
        className="drawer"
        style={{ width }}
        onMouseDown={(event) => event.stopPropagation()}
        role="dialog"
        aria-modal="true"
      >
        <header className="drawer__head">
          <div className="drawer__heading">
            <h2>{title}</h2>
            {subtitle && <p>{subtitle}</p>}
          </div>
          <div className="drawer__actions">
            {actions}
            <IconButton icon="close" label="关闭" onClick={onClose} />
          </div>
        </header>
        <div className="drawer__body">{children}</div>
      </aside>
    </div>,
    document.body,
  )
}

/* ── Modal ──────────────────────────────────────────────── */

export function Modal({
  open,
  onClose,
  title,
  subtitle,
  children,
  footer,
  size = 'md',
}: {
  open: boolean
  onClose: () => void
  title: ReactNode
  subtitle?: ReactNode
  children: ReactNode
  footer?: ReactNode
  size?: 'sm' | 'md' | 'lg'
}) {
  useEffect(() => {
    if (!open) return
    function onKey(event: KeyboardEvent) {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, onClose])

  if (!open) return null
  return createPortal(
    <div className="overlay overlay--center" onMouseDown={onClose}>
      <div
        className={cn('modal', `modal--${size}`)}
        onMouseDown={(event) => event.stopPropagation()}
        role="dialog"
        aria-modal="true"
      >
        <header className="modal__head">
          <div>
            <h2>{title}</h2>
            {subtitle && <p>{subtitle}</p>}
          </div>
          <IconButton icon="close" label="关闭" onClick={onClose} />
        </header>
        <div className="modal__body">{children}</div>
        {footer && <footer className="modal__foot">{footer}</footer>}
      </div>
    </div>,
    document.body,
  )
}

/* ── Toast ──────────────────────────────────────────────── */

type ToastTone = 'success' | 'danger' | 'info' | 'warning'

interface ToastItem {
  id: number
  title: string
  description?: string
  tone: ToastTone
}

interface ToastApi {
  push: (title: string, options?: { description?: string; tone?: ToastTone }) => void
  success: (title: string, description?: string) => void
  error: (title: string, description?: string) => void
  info: (title: string, description?: string) => void
}

const ToastContext = createContext<ToastApi | null>(null)

export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<ToastItem[]>([])
  const seq = useRef(0)

  const remove = useCallback((id: number) => {
    setItems((prev) => prev.filter((item) => item.id !== id))
  }, [])

  const push = useCallback(
    (title: string, options?: { description?: string; tone?: ToastTone }) => {
      const id = ++seq.current
      setItems((prev) => [...prev.slice(-3), { id, title, description: options?.description, tone: options?.tone ?? 'info' }])
      window.setTimeout(() => remove(id), 4200)
    },
    [remove],
  )

  const api = useMemo<ToastApi>(
    () => ({
      push,
      success: (title, description) => push(title, { description, tone: 'success' }),
      error: (title, description) => push(title, { description, tone: 'danger' }),
      info: (title, description) => push(title, { description, tone: 'info' }),
    }),
    [push],
  )

  return (
    <ToastContext.Provider value={api}>
      {children}
      {createPortal(
        <div className="toasts" aria-live="polite">
          {items.map((item) => (
            <div key={item.id} className={cn('toast', `toast--${item.tone}`)}>
              <span className="toast__icon">
                <Icon
                  name={item.tone === 'success' ? 'check' : item.tone === 'danger' ? 'alert' : 'info'}
                  size={16}
                />
              </span>
              <div className="toast__body">
                <strong>{item.title}</strong>
                {item.description && <span>{item.description}</span>}
              </div>
              <button className="toast__close" onClick={() => remove(item.id)} aria-label="关闭">
                <Icon name="close" size={13} />
              </button>
            </div>
          ))}
        </div>,
        document.body,
      )}
    </ToastContext.Provider>
  )
}

export function useToast(): ToastApi {
  const context = useContext(ToastContext)
  if (!context) throw new Error('useToast 必须在 ToastProvider 内使用')
  return context
}

/* ── Confirm ────────────────────────────────────────────── */

export function ConfirmDialog({
  open,
  title,
  description,
  confirmLabel = '确认',
  danger,
  busy,
  onCancel,
  onConfirm,
}: {
  open: boolean
  title: string
  description?: ReactNode
  confirmLabel?: string
  danger?: boolean
  busy?: boolean
  onCancel: () => void
  onConfirm: () => void
}) {
  return (
    <Modal
      open={open}
      onClose={onCancel}
      title={title}
      size="sm"
      footer={
        <>
          <Button onClick={onCancel}>取消</Button>
          <Button variant={danger ? 'danger' : 'primary'} loading={busy} onClick={onConfirm}>
            {confirmLabel}
          </Button>
        </>
      }
    >
      <p className="confirm-text">{description}</p>
    </Modal>
  )
}
