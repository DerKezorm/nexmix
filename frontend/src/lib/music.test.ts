/** Lengths, decades and moods as the playlists say them, and every mood of the groups in German. */
import { changeLanguage } from '../i18n'
import de from '../i18n/de.json'
import { clock, decadeName, length, moodName } from './music'
import { MOOD_GROUPS } from './moodGroups'

describe('saying music', () => {
  beforeEach(async () => {
    await changeLanguage('de', false)
  })

  it('says lengths and decades', () => {
    expect(clock(187)).toBe('3:07')
    expect(length(4380)).toBe('1 Std. 13 Min.')
    expect(length(200)).toBe('3 Min.')
    expect(decadeName(1990)).toBe('90er')
    expect(decadeName(2000)).toBe('00er')
    expect(decadeName(1920)).toBe('1920er')
  })

  it('says moods in German, and a mood it does not know as it is', () => {
    expect(moodName('Calm/Peaceful')).toBe('Ruhig')
    expect(moodName('Laid-Back/Mellow')).toBe('Lässig')
    expect(moodName('Some New Mood')).toBe('Some New Mood')
  })

  it('has every mood of the groups, and some three hundred in all', () => {
    const moods = (de as { moods: Record<string, string> }).moods
    for (const [, names] of MOOD_GROUPS) for (const name of names) expect(moods[name], name).toBeTruthy()
    expect(Object.keys(moods).length).toBeGreaterThan(290)
  })

  it('says it in English too', async () => {
    await changeLanguage('en', false)
    expect(length(4380)).toBe('1 h 13 min')
    expect(decadeName(1990)).toBe('90s')
    expect(moodName('Calm/Peaceful')).toBe('Calm/Peaceful')
  })
})
