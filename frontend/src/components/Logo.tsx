// The mark: four lines of a list and a full play button in violet. A white tile in light mode, a dark one in dark mode;
// the colours are `--nmx-logo-*` in index.css (source nexapps/icons-vergleich/neu.py, like public/logo.svg). The second
// half of the wordmark follows the accent.
export function LogoMark({ size = 36, className = '' }: { size?: number; className?: string }) {
  // userSpaceOnUse, so the gradient runs across the whole mark instead of restarting for each line.
  return (
    <svg width={size} height={size} viewBox="0 0 64 64" className={`mark ${className}`.trim()} aria-hidden="true">
      <defs>
        <linearGradient id="nexmix-mark" gradientUnits="userSpaceOnUse" x1="10" y1="10" x2="54" y2="54">
          <stop offset="0" style={{ stopColor: 'var(--nmx-logo-verlauf-1)' }} />
          <stop offset=".5" style={{ stopColor: 'var(--nmx-logo-verlauf-2)' }} />
          <stop offset="1" style={{ stopColor: 'var(--nmx-logo-verlauf-3)' }} />
        </linearGradient>
      </defs>
      <rect x="2" y="2" width="60" height="60" rx="16" style={{ fill: 'var(--nmx-logo-grund)' }} />
      <rect
        x="2.75"
        y="2.75"
        width="58.5"
        height="58.5"
        rx="15.3"
        fill="none"
        strokeWidth="1.5"
        style={{ stroke: 'var(--nmx-logo-rand)', strokeOpacity: 'var(--nmx-logo-rand-deckung)' }}
      />
      <path d="M13 18h27M13 27h21M13 36h14M13 45h10" stroke="url(#nexmix-mark)" strokeWidth="4.2" strokeLinecap="round" />
      <circle cx="43" cy="41" r="11" strokeWidth="2" style={{ fill: 'var(--nmx-logo-knopf)', stroke: 'var(--nmx-logo-grund)' }} />
      <path
        d="M40 35.8v10.4l8.6-5.2Z"
        strokeWidth="1.4"
        strokeLinejoin="round"
        style={{ fill: 'var(--nmx-logo-play)', stroke: 'var(--nmx-logo-play)' }}
      />
    </svg>
  )
}

/** The mark and the name, side by side, as on the sign-in page. `size` is the height of the mark. */
export function Wordmark({ size = 34 }: { size?: number }) {
  return (
    <span className="flex items-center justify-center gap-2.5">
      <LogoMark size={size} />
      <span className="wordmark" style={{ fontSize: Math.round(size * 0.55) }}>
        NEX<span>MIX</span>
      </span>
    </span>
  )
}
