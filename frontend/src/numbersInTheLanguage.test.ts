/**
 * Numbers and dates are written in the language the person chose, never in the one of the machine: a bare
 * `toLocaleString()` or `toLocaleDateString()` takes the browser's (or, in the tests, the system's) and wrote "52,731"
 * in a German page on a machine set to English. Every call names the language (`i18n.language`).
 */
type Fs = {
  readFileSync(path: string, encoding: 'utf-8'): string
  readdirSync(path: string, options: { recursive: true }): string[]
}

const fs = (await import(/* @vite-ignore */ ['node', 'fs'].join(':'))) as unknown as Fs

const BARE = /\.toLocale(?:Date|Time)?String\(\s*\)/

describe('numbers and dates in the chosen language', () => {
  it('never takes the language of the machine', () => {
    const found: string[] = []
    for (const name of fs.readdirSync('src', { recursive: true }).map((item) => item.replaceAll(String.fromCharCode(92), '/'))) {
      if (!/\.(ts|tsx)$/.test(name) || /\.test\.tsx?$/.test(name)) continue
      fs.readFileSync(`src/${name}`, 'utf-8').split('\n').forEach((line, index) => {
        if (BARE.test(line)) found.push(`src/${name}:${index + 1}`)
      })
    }
    expect(found).toEqual([])
  })

  it('would see a bare call', () => {
    expect(BARE.test('count: total.toLocaleString() })')).toBe(true)
    expect(BARE.test('count: total.toLocaleString(i18n.language) })')).toBe(false)
  })
})
