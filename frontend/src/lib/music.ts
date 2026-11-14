/** How the playlists say lengths, years and moods. */
import i18n from 'i18next'

/** 3:07 */
export function clock(seconds: number): string {
  const whole = Math.max(0, Math.round(seconds))
  return `${Math.floor(whole / 60)}:${String(whole % 60).padStart(2, '0')}`
}

/** "1 Std. 12 Min." or "48 Min." */
export function length(seconds: number): string {
  const hours = Math.floor(seconds / 3600)
  const minutes = Math.round((seconds % 3600) / 60)
  return hours ? i18n.t('music.hoursMinutes', { hours, minutes }) : i18n.t('music.minutes', { minutes })
}

/** A mood by its English name from Plex, in the language of the page. */
export function moodName(name: string): string {
  if (/[.:]/.test(name)) return name
  return i18n.t(`moods.${name}`, { defaultValue: name })
}

/** "90er", "00er"; a decade outside 1950 to 2049 with its century ("1920er"). */
export function decadeName(decade: number): string {
  const short = decade >= 1950 && decade < 2050 ? String(decade).slice(2) : String(decade)
  return i18n.t('music.decade', { decade: short })
}

/** "Artist · Album · 1984", the parts there are. */
export function trackLine(artist: string, album: string, year: number | null): string {
  return [artist, album, year ? String(year) : ''].filter((part) => part).join(' · ')
}
