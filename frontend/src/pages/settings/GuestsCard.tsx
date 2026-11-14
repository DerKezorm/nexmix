/**
 * "Guests": what a new invitation for guests starts with (listening, tracks per guest, hours). Each invitation can set
 * it differently in its own dialog. A number is saved when the field is left.
 */
import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'

import type { Server } from './ServerCards'
import { Card, Feedback, Toggle } from './ui'

function NumberRow({ label, value, min, max, onSave }: { label: string; value: number; min: number; max: number; onSave: (value: number) => void }) {
  const [text, setText] = useState(String(value))
  useEffect(() => setText(String(value)), [value])
  const save = () => {
    const wanted = Math.max(min, Math.min(max, Math.round(Number(text) || min)))
    setText(String(wanted))
    if (wanted !== value) onSave(wanted)
  }
  return (
    <label className="toggle">
      <span>{label}</span>
      <input
        className="num"
        type="number"
        inputMode="numeric"
        min={min}
        max={max}
        value={text}
        aria-label={label}
        onChange={(event) => setText(event.target.value)}
        onBlur={save}
        onKeyDown={(event) => {
          if (event.key === 'Enter') event.currentTarget.blur()
        }}
      />
    </label>
  )
}

export function GuestsCard({ server }: { server: Server }) {
  const { t } = useTranslation()
  const s = server.settings
  if (!s) return null
  return (
    <Card id="guests" title={t('server.guests.title')} text={t('server.guests.text')}>
      <div>
        <Toggle label={t('server.guests.preview')} hint={t('server.guests.previewHint')} checked={s.guest_preview ?? true} onChange={(guest_preview) => void server.save({ guest_preview })} />
        <NumberRow label={t('server.guests.max')} value={s.guest_max ?? 10} min={1} max={100} onSave={(guest_max) => void server.save({ guest_max })} />
        <NumberRow label={t('server.guests.hours')} value={s.guest_hours ?? 24} min={1} max={720} onSave={(guest_hours) => void server.save({ guest_hours })} />
      </div>
      <Feedback problem={server.problem} />
    </Card>
  )
}
