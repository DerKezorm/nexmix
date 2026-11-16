/**
 * Building blocks of the settings, account and about pages: the card, the switch rows, the field, the button and the
 * segments of the design, the same everywhere.
 */
import type { LucideIcon } from 'lucide-react'
import { createContext, useCallback, useContext, useId, useState, type KeyboardEvent, type ReactNode } from 'react'
import { useTranslation } from 'react-i18next'

import { ApiError } from '../../api/client'
import { ConfirmDialog } from '../../components/ConfirmDialog'
import { copyText } from '../../lib/copy'
import { errorText } from '../../lib/errors'

/** Inside a card: a card placed there becomes a part of it (a heading of its own, a line above), as "Security" holds
 * the password, the second factor and the passkeys. */
const InCard = createContext(false)

/** A card with its title and a line of explanation; inside another card, a part of that one. */
export function Card({ title, text, children, id }: { title: string; text?: string; children: ReactNode; id?: string }) {
  const nested = useContext(InCard)
  const titleId = id ? `${id}-title` : undefined
  if (nested)
    return (
      <section id={id} aria-labelledby={titleId} className="card-part">
        <h3 id={titleId}>{title}</h3>
        {text && <p>{text}</p>}
        <div className="space-y-3">{children}</div>
      </section>
    )
  return (
    <section id={id} aria-labelledby={titleId} className="card">
      <h2 id={titleId}>{title}</h2>
      {text && <p>{text}</p>}
      <div className="space-y-3">
        <InCard.Provider value={true}>{children}</InCard.Provider>
      </div>
    </section>
  )
}

/** A heading inside a card, for a second part of it (as "OpenID Connect" under "Sign-in"). */
export function SubHead({ title, text }: { title: string; text?: string }) {
  return (
    <div className="pt-3">
      <h3 className="font-semibold">{title}</h3>
      {text && <p className="text-sm text-muted">{text}</p>}
    </div>
  )
}

/** A switch with its name and a line of explanation; the whole row is the label. */
export function Toggle({ label, hint, checked, onChange, disabled = false }: { label: string; hint?: string; checked: boolean; onChange: (value: boolean) => void; disabled?: boolean }) {
  const hintId = useId()
  return (
    <label className={'toggle ' + (disabled ? 'opacity-60' : 'cursor-pointer')}>
      <span>
        {label}
        {hint && (
          <small id={hintId}>{hint}</small>
        )}
      </span>
      <span className={`sw ${checked ? 'on' : ''}`}>
        <button type="button" role="switch" aria-checked={checked} aria-label={label} aria-describedby={hint ? hintId : undefined} disabled={disabled} onClick={() => onChange(!checked)} />
        <span aria-hidden />
      </span>
    </label>
  )
}

const FIELD = 'input mt-1.5 placeholder:text-muted/70'

/** A field with its name above it and an optional line below. */
export function Input({
  label,
  value,
  onChange,
  type = 'text',
  placeholder,
  hint,
  className = '',
  autoComplete = 'off',
  autoFocus = false,
  min,
  max,
  maxLength,
}: {
  label: string
  value: string
  onChange: (value: string) => void
  type?: string
  placeholder?: string
  hint?: string
  className?: string
  autoComplete?: string
  autoFocus?: boolean
  min?: number
  max?: number
  maxLength?: number
}) {
  const hintId = useId()
  return (
    <div className={'field ' + className}>
      <label className="block">
        <span className="font-semibold">{label}</span>
        <input
          type={type}
          value={value}
          placeholder={placeholder}
          autoComplete={autoComplete}
          autoFocus={autoFocus}
          min={min}
          max={max}
          maxLength={maxLength}
          aria-describedby={hint ? hintId : undefined}
          onChange={(event) => onChange(event.target.value)}
          className={FIELD}
        />
      </label>
      {hint && (
        <span id={hintId} className="mt-1 block text-[12.5px] text-muted">
          {hint}
        </span>
      )}
    </div>
  )
}

export function Select<T extends string>({ label, value, options, onChange, className = '' }: { label: string; value: T; options: { value: T; label: string }[]; onChange: (value: T) => void; className?: string }) {
  return (
    <label className={'block ' + className}>
      <span className="font-semibold">{label}</span>
      <select value={value} onChange={(event) => onChange(event.target.value as T)} className="input mt-1.5">
        {options.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
      </select>
    </label>
  )
}

/** The buttons of the settings: one primary per card, the others quiet; `danger` for what cannot be taken back. */
export function Button({
  children,
  onClick,
  busy = false,
  primary = false,
  danger = false,
  ghost = false,
  small = false,
  type = 'button',
  disabled = false,
  label,
  className = '',
}: {
  children: ReactNode
  onClick?: () => void
  busy?: boolean
  primary?: boolean
  danger?: boolean
  /** Quiet: no ground and no frame until pointed at (`.btn.ghost`). */
  ghost?: boolean
  small?: boolean
  type?: 'button' | 'submit'
  disabled?: boolean
  label?: string
  className?: string
}) {
  return (
    <button
      type={type}
      onClick={onClick}
      disabled={busy || disabled}
      aria-label={label}
      className={`btn ${primary ? 'primary' : ''} ${danger ? 'danger' : ''} ${ghost ? 'ghost' : ''} ${small ? 'small' : ''} ${className}`}
    >
      {busy ? '…' : children}
    </button>
  )
}

/** One line under a card: what went wrong, or that it worked. */
export function Feedback({ problem, done = null, values }: { problem: string | null; done?: string | null; values?: Record<string, unknown> }) {
  // Nothing at all while there is nothing to say: an empty line would add its gap to the card.
  if (!problem && !done) return null
  return (
    <div aria-live="polite">
      {problem && (
        <p role="alert" className="alert !mt-0">
          {errorText(problem, values)}
        </p>
      )}
      {done && (
        <p role="status" className="text-sm font-semibold text-accent">
          ✓ {done}
        </p>
      )}
    </div>
  )
}

/** A link or value to copy: the field and the button that copies it. */
export function CopyLink({ value, label }: { value: string; label: string }) {
  const { t } = useTranslation()
  const [copied, setCopied] = useState(false)
  return (
    <div className="linkbox">
      <input readOnly value={value} aria-label={label} onFocus={(event) => event.target.select()} className="input" />
      <button type="button" onClick={() => void copyText(value).then(setCopied)} className="btn">
        {copied ? '✓' : t('common.copy')}
      </button>
    </div>
  )
}

/** Runs a change, remembers whether it worked; the card shows it with `Feedback`. */
// eslint-disable-next-line react-refresh/only-export-components
export function useAction() {
  const [busy, setBusy] = useState(false)
  const [problem, setProblem] = useState<string | null>(null)
  const [done, setDone] = useState<string | null>(null)
  /** What the server said along with the problem (a limit, a number), for the sentence. */
  const [values, setValues] = useState<Record<string, unknown>>({})
  const run = useCallback(async (work: () => Promise<unknown>, success?: string): Promise<boolean> => {
    setBusy(true)
    setProblem(null)
    setValues({})
    setDone(null)
    try {
      await work()
      if (success) setDone(success)
      return true
    } catch (error) {
      setProblem(error instanceof ApiError ? error.code : 'internal_error')
      setValues(error instanceof ApiError ? error.values : {})
      return false
    } finally {
      setBusy(false)
    }
  }, [])
  const clear = useCallback(() => {
    setProblem(null)
    setDone(null)
  }, [])
  return { busy, problem, values, done, run, clear }
}

/** "Really?" with the operator's password where the server asks for it again: the dialog of the whole interface
 * (``components/ConfirmDialog``), here with the words of the settings (its button is the quiet one unless ``danger``). */
export function Confirm({ title, text, confirm, danger = false, password = false, onCancel, onConfirm }: {
  title: string
  text: ReactNode
  confirm: string
  danger?: boolean
  password?: boolean
  onCancel: () => void
  onConfirm: (password: string) => Promise<void>
}) {
  return <ConfirmDialog title={title} text={text} confirm={confirm} danger={danger} password={password} onCancel={onCancel} onConfirm={onConfirm} />
}

/** A choice of a few, as the segments of the design. */
export function Segment<T extends string>({ options, value, onChange, label }: { options: { value: T; label: string }[]; value: T; onChange: (value: T) => void; label?: string }) {
  return (
    <div role="radiogroup" aria-label={label} className="seg">
      {options.map((option) => (
        <button key={option.value} type="button" role="radio" aria-checked={value === option.value} onClick={() => onChange(option.value)} className={value === option.value ? 'on' : undefined}>
          {option.label}
        </button>
      ))}
    </div>
  )
}

/** A tab of a `TabRow`: its value in the address, its name, its sign; `title` says more where the name alone does not
 * (the kind of a media server). */
export type Tab<T extends string> = { value: T; label: string; icon?: LucideIcon; title?: string }

/** A row of round tabs, as in the other nex apps. `under`: a second row below the first, tied to it by a line on the
 * left. Wraps when narrow. The arrows go from tab to tab, Home and End to the first and the last. */
export function TabRow<T extends string>({ tabs, active, onChange, under = false, label }: { tabs: Tab<T>[]; active: T; onChange: (value: T) => void; under?: boolean; label?: string }) {
  const step = (event: KeyboardEvent<HTMLButtonElement>, at: number) => {
    const to = event.key === 'ArrowRight' ? at + 1 : event.key === 'ArrowLeft' ? at - 1 : event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1 : null
    if (to === null) return
    event.preventDefault()
    const next = tabs[(to + tabs.length) % tabs.length]
    onChange(next.value)
    const row = event.currentTarget.parentElement
    window.setTimeout(() => row?.querySelector<HTMLButtonElement>(`[data-tab="${next.value}"]`)?.focus(), 0)
  }
  return (
    <div role="tablist" aria-label={label} className={`flex flex-wrap items-center gap-2 ${under ? 'border-l-2 border-accent/40 pl-4' : ''}`} data-testid={under ? 'sub-tabs' : 'tabs'}>
      {tabs.map(({ value, label: name, icon: Icon, title }, at) => {
        const on = value === active
        return (
          <button
            key={value}
            type="button"
            role="tab"
            aria-selected={on}
            tabIndex={on ? 0 : -1}
            title={title}
            data-tab={value}
            onClick={() => onChange(value)}
            onKeyDown={(event) => step(event, at)}
            className={`inline-flex items-center gap-2 rounded-full border font-semibold transition ${under ? 'px-3.5 py-1.5 text-sm' : 'px-4 py-2 text-sm'} ${
              on ? 'border-accent/50 bg-accent-soft text-accent' : 'border-line bg-sheet text-ink-2 hover:text-ink'
            }`}
          >
            {Icon && <Icon size={16} aria-hidden />}
            <span dir="auto">{name}</span>
            {title && <span className="sr-only">({title})</span>}
          </button>
        )
      })}
    </div>
  )
}

// eslint-disable-next-line react-refresh/only-export-components
export function saveAsFile(name: string, data: Blob) {
  const url = URL.createObjectURL(data)
  const link = document.createElement('a')
  link.href = url
  link.download = name
  document.body.appendChild(link)
  link.click()
  link.remove()
  URL.revokeObjectURL(url)
}
