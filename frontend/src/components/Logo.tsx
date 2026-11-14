// The mark: a white tile with four lines of a list and a play circle, drawn in violet. It stays the same in every
// theme; the second half of the wordmark follows the accent.
const VIOLET = '#5b2ee6'

export function LogoMark({ size = 36, className = '' }: { size?: number; className?: string }) {
  return (
    <svg width={size} height={size} viewBox="0 0 64 64" className={`mark ${className}`.trim()} aria-hidden="true">
      <rect x="2" y="2" width="60" height="60" rx="16" fill="#ffffff" />
      <rect x="2" y="2" width="60" height="60" rx="16" fill="none" stroke={VIOLET} strokeWidth="2.5" />
      <g stroke={VIOLET} strokeWidth="3.2" strokeLinecap="round">
        <path d="M13 19h26M13 28h20M13 37h14M13 46h10" />
      </g>
      <circle cx="44" cy="42" r="10.5" fill="none" stroke={VIOLET} strokeWidth="2.8" />
      <path d="M41.2 37.2v9.6l8-4.8z" fill={VIOLET} />
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
