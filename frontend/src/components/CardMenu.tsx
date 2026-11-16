/**
 * The menu "…" on a card: a button that opens a list of actions (`role="menu"`). The keyboard goes through it with the
 * arrows, Home and End; Escape closes it and puts the focus back on the button, Tab closes it and goes on. A click
 * beside it closes it too. An action closes the menu first and gives the focus back to the button, so that a dialog it
 * opens returns there when it closes.
 */
import type { LucideIcon } from 'lucide-react'
import { MoreHorizontal } from 'lucide-react'
import { useEffect, useId, useRef, useState, type KeyboardEvent } from 'react'

export type MenuAction = { key: string; label: string; icon: LucideIcon; run: () => void; danger?: boolean }

export function CardMenu({ label, actions }: { label: string; actions: MenuAction[] }) {
  const [open, setOpen] = useState(false)
  const box = useRef<HTMLSpanElement>(null)
  const button = useRef<HTMLButtonElement>(null)
  const list = useRef<HTMLSpanElement>(null)
  // Where the focus goes once the list is there: the first item, or the last (arrow up on the button).
  const start = useRef<'first' | 'last'>('first')
  const menuId = useId()

  const items = () => Array.from(list.current?.querySelectorAll<HTMLButtonElement>('[role="menuitem"]') ?? [])
  const close = (focusBack: boolean) => {
    setOpen(false)
    if (focusBack) button.current?.focus()
  }

  useEffect(() => {
    if (!open) return
    const all = items()
    ;(start.current === 'last' ? all[all.length - 1] : all[0])?.focus()
    const away = (event: MouseEvent) => {
      if (!box.current?.contains(event.target as Node)) setOpen(false)
    }
    const escape = (event: globalThis.KeyboardEvent) => {
      if (event.key !== 'Escape') return
      event.preventDefault()
      setOpen(false)
      button.current?.focus()
    }
    document.addEventListener('mousedown', away)
    document.addEventListener('keydown', escape)
    return () => {
      document.removeEventListener('mousedown', away)
      document.removeEventListener('keydown', escape)
    }
  }, [open])

  const onListKey = (event: KeyboardEvent) => {
    const all = items()
    const at = all.indexOf(document.activeElement as HTMLButtonElement)
    const go = (index: number) => {
      event.preventDefault()
      all[(index + all.length) % all.length]?.focus()
    }
    if (event.key === 'ArrowDown') go(at + 1)
    else if (event.key === 'ArrowUp') go(at - 1)
    else if (event.key === 'Home') go(0)
    else if (event.key === 'End') go(all.length - 1)
    else if (event.key === 'Tab') close(false)
  }

  return (
    <span className={`pmenu${open ? ' open' : ''}`} ref={box}>
      <button
        ref={button}
        type="button"
        className="more"
        aria-expanded={open}
        aria-haspopup="menu"
        aria-controls={open ? menuId : undefined}
        aria-label={label}
        onClick={() => {
          start.current = 'first'
          setOpen(!open)
        }}
        onKeyDown={(event) => {
          if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
            event.preventDefault()
            start.current = event.key === 'ArrowUp' ? 'last' : 'first'
            setOpen(true)
          }
        }}
        data-testid="card-menu"
      >
        <MoreHorizontal size={16} aria-hidden />
      </button>
      {open && (
        <span ref={list} id={menuId} role="menu" aria-label={label} className="menu card rise" onKeyDown={onListKey}>
          {actions.map(({ key, label: name, icon: Icon, run, danger }) => (
            <button
              key={key}
              type="button"
              role="menuitem"
              tabIndex={-1}
              className={danger ? 'danger' : undefined}
              onClick={() => {
                close(true)
                run()
              }}
              data-testid={`card-${key}`}
            >
              <Icon size={15} aria-hidden />
              {name}
            </button>
          ))}
        </span>
      )}
    </span>
  )
}
